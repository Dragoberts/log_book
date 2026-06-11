#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Log Book Server with integrated API
"""

from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import json
import sqlite3
import sys
import io
import threading
import time
from datetime import datetime, timedelta

try:
    import requests
except ImportError:
    requests = None

# Fix encoding for Windows
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

DB_PATH = Path.home() / '.homeassistant' / 'log_book.db'

# --- Live importer config (loaded from env vars or config.json - never hardcoded) ---
from config import load_config
_CFG = load_config()
HA_URL = _CFG["ha_url"]
HA_WS_URL = _CFG["ha_ws_url"]
HA_TOKEN = _CFG["ha_token"]
HA_HEADERS = _CFG["headers"]
LIVE_POLL_SECONDS = 10  # how often the server pulls new HA logbook entries
TRACE_POLL_SECONDS = 15  # how often we check automation traces for blocked runs
TRACE_RETENTION_DAYS = 60  # how long archived traces are kept

try:
    import websocket as _wsclient  # websocket-client
except ImportError:
    _wsclient = None


def ha_ws(commands):
    """Open a WebSocket to HA, authenticate, run command dicts (without 'id'),
    return the list of matching result messages (or None on failure)."""
    if _wsclient is None:
        return None
    ws = None
    try:
        ws = _wsclient.create_connection(HA_WS_URL, timeout=20)
        ws.recv()  # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": HA_TOKEN}))
        if json.loads(ws.recv()).get("type") != "auth_ok":
            return None
        results = []
        mid = 1
        for cmd in commands:
            payload = dict(cmd)
            payload["id"] = mid
            ws.send(json.dumps(payload))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == mid and msg.get("type") == "result":
                    results.append(msg)
                    break
            mid += 1
        return results
    except Exception as e:
        print(f"[ws] Fehler: {e}")
        return None
    finally:
        if ws:
            try: ws.close()
            except Exception: pass


def get_automation_map():
    """entity_id -> {uid, name} for all automation entities."""
    try:
        r = requests.get(f"{HA_URL}/api/states", headers=HA_HEADERS, timeout=20)
        r.raise_for_status()
        m = {}
        for s in r.json():
            if s["entity_id"].startswith("automation."):
                a = s.get("attributes", {})
                m[s["entity_id"]] = {"uid": a.get("id"), "name": a.get("friendly_name", s["entity_id"])}
        return m
    except Exception:
        return {}


def _build_person_map():
    """Map HA user_id -> person friendly name (real users)."""
    try:
        r = requests.get(f"{HA_URL}/api/states", headers=HA_HEADERS, timeout=30)
        r.raise_for_status()
        pmap = {}
        for s in r.json():
            if s["entity_id"].startswith("person."):
                attrs = s.get("attributes", {})
                uid = attrs.get("user_id")
                if uid:
                    pmap[uid] = attrs.get("friendly_name", s["entity_id"].split(".")[1])
        return pmap
    except Exception as e:
        print(f"[live] Personen-Map Fehler: {e}")
        return {}


def _normalize_ts(when):
    """HA ISO timestamp (tz-aware) -> local naive ISO, matching the stored format."""
    try:
        dt = datetime.fromisoformat(when)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.isoformat()
    except Exception:
        return when


def _classify(entry, person_map):
    """Same categorization as the import script: real users / automations / devices."""
    entity_id = entry.get("entity_id")
    name = entry.get("name", entity_id or "Unbekannt")
    state = entry.get("state")
    raw_message = entry.get("message")
    domain = entity_id.split(".")[0] if entity_id else None

    # person.* -> user presence, not a device
    if domain == "person":
        msg = f"{name} {raw_message}" if raw_message else f"{name}: {state}"
        return "user_action", name, None, entity_id, None, msg

    device = name
    entity = entity_id
    automation = None
    ctx_domain = entry.get("context_domain")
    ctx_name = entry.get("context_name")
    if ctx_domain == "automation" and ctx_name:
        automation = ctx_name

    user = None
    ctx_user_id = entry.get("context_user_id")
    if ctx_user_id and ctx_user_id in person_map and ctx_domain != "automation":
        user = person_map[ctx_user_id]

    if automation or entry.get("context_event_type") == "automation_triggered" or domain == "automation":
        event_type = "automation_triggered"
    elif user:
        event_type = "user_action"
    else:
        event_type = "state_change"

    if raw_message:
        message = f"{name} {raw_message}"
    elif state is not None:
        message = f"{name}: {state}"
    else:
        message = name

    return event_type, user, device, entity, automation, message


def _build_metadata(entry, person_map):
    """Extract HA context fields needed to reconstruct the process chain."""
    ctx_user_id = entry.get("context_user_id")
    return {
        "context_id": entry.get("context_id"),
        "context_event_type": entry.get("context_event_type"),
        "context_domain": entry.get("context_domain"),
        "context_name": entry.get("context_name"),
        "context_entity_id": entry.get("context_entity_id"),
        "context_entity_id_name": entry.get("context_entity_id_name"),
        "context_source": entry.get("context_source"),
        "context_message": entry.get("context_message"),
        "context_user_id": ctx_user_id,
        "context_user_name": person_map.get(ctx_user_id) if ctx_user_id else None,
        "state": entry.get("state"),
    }


def _get_last_seen():
    """Latest timestamp already in the DB (as datetime), or 1h ago if empty."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute("SELECT MAX(timestamp) FROM logs")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return datetime.fromisoformat(row[0])
    except Exception:
        pass
    return datetime.now() - timedelta(hours=1)


def live_importer():
    """Background thread: poll HA logbook and insert new entries into the DB."""
    if requests is None:
        print("[live] 'requests' nicht installiert - Live-Import deaktiviert.")
        return

    person_map = _build_person_map()
    last_seen = _get_last_seen()
    print(f"[live] Live-Import aktiv (alle {LIVE_POLL_SECONDS}s). Start ab {last_seen.isoformat()}")
    person_refresh = 0

    while True:
        try:
            # Refresh person map occasionally (new users / restarts)
            person_refresh += 1
            if person_refresh >= 30:
                person_map = _build_person_map() or person_map
                person_refresh = 0

            # Fetch with a small overlap so boundary events are not missed
            start = (last_seen - timedelta(minutes=2)).replace(microsecond=0).isoformat()
            url = f"{HA_URL}/api/logbook/{start}"
            r = requests.get(url, headers=HA_HEADERS, timeout=60)
            r.raise_for_status()
            entries = r.json()

            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            new_count = 0
            new_max = last_seen
            for entry in entries:
                ts_norm = _normalize_ts(entry.get("when"))
                try:
                    ts_dt = datetime.fromisoformat(ts_norm)
                except Exception:
                    continue
                # Only strictly newer than what we've already stored
                if ts_dt <= last_seen:
                    continue
                event_type, user, device, entity, automation, message = _classify(entry, person_map)
                metadata = _build_metadata(entry, person_map)
                cur.execute(
                    """INSERT INTO logs (timestamp, event_type, message, user, device, entity, automation, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ts_norm, event_type, message, user, device, entity, automation, json.dumps(metadata))
                )
                new_count += 1
                if ts_dt > new_max:
                    new_max = ts_dt
            conn.commit()
            conn.close()

            if new_count:
                last_seen = new_max
                print(f"[live] +{new_count} neue Logs (neuester: {last_seen.isoformat()})")
        except Exception as e:
            print(f"[live] Fehler: {e}")

        time.sleep(LIVE_POLL_SECONDS)


def _init_traces_table():
    """Create the long-term trace archive table (kept for TRACE_RETENTION_DAYS)."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS traces (
                run_id TEXT PRIMARY KEY,
                automation_entity TEXT,
                automation_name TEXT,
                timestamp TEXT,
                script_execution TEXT,
                conditions TEXT,
                choices TEXT,
                trigger TEXT
            )"""
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_traces_entity_ts ON traces(automation_entity, timestamp)")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[trace] Tabelle-Init Fehler: {e}")


def _load_archived_runs():
    """run_ids already archived (to avoid re-fetching)."""
    s = set()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute("SELECT run_id FROM traces")
        for row in cur.fetchall():
            s.add(row[0])
        conn.close()
    except Exception:
        pass
    return s


def _parse_trace(full):
    """Extract condition results, choose-branch choices and trigger from a full trace."""
    choices, conditions = {}, {}
    for path, entries in (full.get("trace") or {}).items():
        for e in entries:
            r = e.get("result")
            if isinstance(r, dict):
                if "choice" in r:
                    choices[path] = r["choice"]
                if isinstance(r.get("result"), bool):
                    conditions[path] = r["result"]
    trig = full.get("trigger")
    trig_desc = trig.get("description") if isinstance(trig, dict) else trig
    return {
        "script_execution": full.get("script_execution"),
        "conditions": conditions,
        "choices": choices,
        "trigger": trig_desc,
    }


def _cleanup_old_traces():
    try:
        cutoff = (datetime.now() - timedelta(days=TRACE_RETENTION_DAYS)).isoformat()
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute("DELETE FROM traces WHERE timestamp < ?", (cutoff,))
        n = cur.rowcount
        conn.commit()
        conn.close()
        if n:
            print(f"[trace] {n} alte Traces (>{TRACE_RETENTION_DAYS} Tage) entfernt")
    except Exception:
        pass


def _archive_and_log(amap, new_runs, seen):
    """Fetch full traces for new runs, archive them, and log condition-blocked runs."""
    cmds, meta = [], []
    for ent, tr in new_runs:
        uid = amap[ent]["uid"]
        cmds.append({"type": "trace/get", "domain": "automation", "item_id": uid, "run_id": tr["run_id"]})
        meta.append((ent, tr))
    details = ha_ws(cmds) or [None] * len(meta)

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    archived = failed_logged = 0
    for (ent, tr), det in zip(meta, details):
        rid = tr["run_id"]
        seen.add(rid)
        name = amap[ent]["name"]
        ts = (tr.get("timestamp") or {}).get("start")
        ts_norm = _normalize_ts(ts) if ts else datetime.now().isoformat()

        parsed = {"script_execution": tr.get("script_execution"), "conditions": {}, "choices": {}, "trigger": None}
        if det and det.get("success"):
            parsed = _parse_trace(det.get("result", {}))
            if not parsed["script_execution"]:
                parsed["script_execution"] = tr.get("script_execution")

        try:
            cur.execute(
                """INSERT OR REPLACE INTO traces
                   (run_id, automation_entity, automation_name, timestamp, script_execution, conditions, choices, trigger)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (rid, ent, name, ts_norm, parsed["script_execution"],
                 json.dumps(parsed["conditions"]), json.dumps(parsed["choices"]), parsed["trigger"])
            )
            archived += 1
        except Exception:
            pass

        if parsed["script_execution"] == "failed_conditions":
            cur.execute("SELECT 1 FROM logs WHERE event_type='automation_failed' AND json_extract(metadata,'$.run_id')=? LIMIT 1", (rid,))
            if not cur.fetchone():
                failed_cond_path = None
                for path, val in parsed["conditions"].items():
                    if "condition" in path and val is False:
                        failed_cond_path = path
                        break
                message = f"Automatisierung '{name}' ausgelöst, aber Bedingung nicht erfüllt – nicht ausgeführt"
                metadata = {
                    "run_id": rid, "script_execution": "failed_conditions",
                    "context_source": parsed["trigger"], "context_entity_id": ent,
                    "context_name": name, "context_event_type": "automation_triggered",
                    "failed_condition_path": failed_cond_path, "trace_conditions": parsed["conditions"],
                }
                cur.execute(
                    """INSERT INTO logs (timestamp, event_type, message, user, device, entity, automation, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ts_norm, "automation_failed", message, None, name, ent, name, json.dumps(metadata))
                )
                failed_logged += 1
    conn.commit()
    conn.close()
    if archived:
        extra = f", {failed_logged} fehlgeschlagene geloggt" if failed_logged else ""
        print(f"[trace] +{archived} Traces archiviert{extra}")


def trace_poller():
    """Background thread: archive EVERY automation run's trace (last 60 days) so
    condition results stay available even after HA purges its last-5 history."""
    if _wsclient is None or requests is None:
        print("[trace] deaktiviert (websocket-client fehlt)")
        return

    _init_traces_table()
    seen = _load_archived_runs()
    amap = get_automation_map()
    print(f"[trace] Archiv-Poller aktiv (alle {TRACE_POLL_SECONDS}s, {len(amap)} Automatisierungen, Aufbewahrung {TRACE_RETENTION_DAYS} Tage)")
    refresh = cleanup = 0

    while True:
        try:
            refresh += 1
            if refresh >= 40:
                amap = get_automation_map() or amap
                refresh = 0
            cleanup += 1
            if cleanup >= 240:  # ~hourly at 15s interval
                _cleanup_old_traces()
                cleanup = 0

            ents = [e for e, info in amap.items() if info.get("uid")]
            cmds = [{"type": "trace/list", "domain": "automation", "item_id": amap[e]["uid"]} for e in ents]
            results = ha_ws(cmds)
            if results:
                new_runs = []
                for ent, res in zip(ents, results):
                    if not res.get("success"):
                        continue
                    for tr in res.get("result", []):
                        rid = tr.get("run_id")
                        if rid and rid not in seen:
                            new_runs.append((ent, tr))
                if new_runs:
                    _archive_and_log(amap, new_runs, seen)
        except Exception as e:
            print(f"[trace] Fehler: {e}")

        time.sleep(TRACE_POLL_SECONDS)

class LogBookHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(www_dir), **kwargs)

    def do_GET(self):
        try:
            # Handle API endpoints FIRST
            if '/api/log_book/' in self.path:
                if self.path.startswith('/api/log_book/logs'):
                    self.get_logs(parse_qs(urlparse(self.path).query))
                elif self.path.startswith('/api/log_book/filters'):
                    self.get_filters()
                elif self.path.startswith('/api/log_book/chain'):
                    self.get_chain(parse_qs(urlparse(self.path).query))
                elif self.path.startswith('/api/log_book/automation'):
                    self.get_automation(parse_qs(urlparse(self.path).query))
                elif self.path.startswith('/api/log_book/trace'):
                    self.get_trace(parse_qs(urlparse(self.path).query))
                elif self.path.startswith('/api/log_book/patterns'):
                    self.get_patterns(parse_qs(urlparse(self.path).query))
                elif self.path.startswith('/api/log_book/entity_stats'):
                    self.get_entity_stats(parse_qs(urlparse(self.path).query))
                elif self.path.startswith('/api/log_book/count'):
                    self.get_count(parse_qs(urlparse(self.path).query))
                else:
                    self.send_error(404)
                return

            # Serve static files
            super().do_GET()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # Client disconnected mid-response (e.g. page reload) - ignore safely
            pass

    def handle_api_get(self):
        parsed_path = urlparse(self.path)
        query_params = parse_qs(parsed_path.query)

        if parsed_path.path == '/api/log_book/logs':
            self.get_logs(query_params)
        elif parsed_path.path == '/api/log_book/filters':
            self.get_filters()
        else:
            self.send_error(404)

    def get_logs(self, query_params):
        try:
            limit = int(query_params.get('limit', ['100'])[0])
            offset = int(query_params.get('offset', ['0'])[0])
            limit = min(limit, 1000000)  # allow "Alle"

            user = query_params.get('user', [None])[0] if query_params.get('user') else None
            device = query_params.get('device', [None])[0] if query_params.get('device') else None
            entity = query_params.get('entity', [None])[0] if query_params.get('entity') else None
            automation = query_params.get('automation', [None])[0] if query_params.get('automation') else None
            event_type = query_params.get('event_type', [None])[0] if query_params.get('event_type') else None
            exclude_event_type = query_params.get('exclude_event_type', [None])[0] if query_params.get('exclude_event_type') else None
            date_from = query_params.get('date_from', [None])[0] if query_params.get('date_from') else None
            date_to = query_params.get('date_to', [None])[0] if query_params.get('date_to') else None
            chain_only = query_params.get('chain_only', [None])[0] if query_params.get('chain_only') else None
            q = query_params.get('q', [None])[0] if query_params.get('q') else None
            # Hidden keys: each is an entity_id or a message. Exclude matching rows everywhere.
            hidden = query_params.get('hidden', [])

            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            query = "SELECT * FROM logs WHERE 1=1"
            params = []

            if event_type:
                query += " AND event_type = ?"
                params.append(event_type)
            if exclude_event_type:
                query += " AND event_type != ?"
                params.append(exclude_event_type)
            if user:
                query += " AND user = ?"
                params.append(user)
            if device:
                query += " AND device = ?"
                params.append(device)
            if entity:
                query += " AND entity = ?"
                params.append(entity)
            if automation:
                query += " AND automation = ?"
                params.append(automation)
            # DB timestamps use ISO "T" separator (e.g. 2026-06-09T08:37:38.170140).
            # Compare on the date prefix so the separator/microseconds don't matter.
            if date_from:
                query += " AND substr(timestamp, 1, 10) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND substr(timestamp, 1, 10) <= ?"
                params.append(date_to)
            if q:
                like = f"%{q}%"
                query += " AND (message LIKE ? OR entity LIKE ? OR device LIKE ? OR COALESCE(user,'') LIKE ? OR COALESCE(automation,'') LIKE ?)"
                params.extend([like, like, like, like, like])
            if chain_only:
                # Only logs that form a real chain: automation-driven effects (context is an
                # automation), the automation entities themselves, or blocked automations.
                # (A non-automation context like media_player/user is just a single event.)
                query += (" AND (json_extract(metadata, '$.context_entity_id') LIKE 'automation.%'"
                          " OR entity LIKE 'automation.%'"
                          " OR event_type = 'automation_failed')")
            if hidden:
                placeholders = ",".join("?" for _ in hidden)
                # A hidden key may be an entity_id or a message - exclude either match
                query += f" AND COALESCE(entity, '') NOT IN ({placeholders})"
                query += f" AND COALESCE(message, '') NOT IN ({placeholders})"
                params.extend(hidden)
                params.extend(hidden)

            query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            cursor.execute(query, params)
            rows = cursor.fetchall()

            logs = [
                {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "event_type": row["event_type"],
                    "message": row["message"],
                    "user": row["user"],
                    "device": row["device"],
                    "entity": row["entity"],
                    "automation": row["automation"],
                    "metadata": json.loads(row["metadata"]) if row["metadata"] else {}
                }
                for row in rows
            ]

            conn.close()

            self.send_json({"logs": logs, "limit": limit, "offset": offset})
        except Exception as e:
            self.send_json({"error": str(e), "logs": []})

    def get_filters(self):
        try:
            conn = sqlite3.connect(str(DB_PATH))
            cursor = conn.cursor()

            # Get distinct values
            cursor.execute("SELECT DISTINCT user FROM logs WHERE user IS NOT NULL ORDER BY user")
            users = [row[0] for row in cursor.fetchall()]

            cursor.execute("SELECT DISTINCT device FROM logs WHERE device IS NOT NULL ORDER BY device")
            devices = [row[0] for row in cursor.fetchall()]

            cursor.execute("SELECT DISTINCT entity FROM logs WHERE entity IS NOT NULL ORDER BY entity")
            entities = [row[0] for row in cursor.fetchall()]

            cursor.execute("SELECT DISTINCT automation FROM logs WHERE automation IS NOT NULL ORDER BY automation")
            automations = [row[0] for row in cursor.fetchall()]

            cursor.execute("SELECT DISTINCT event_type FROM logs ORDER BY event_type")
            event_types = [row[0] for row in cursor.fetchall()]

            conn.close()

            self.send_json({
                "users": users,
                "devices": devices,
                "entities": entities,
                "automations": automations,
                "event_types": event_types
            })
        except Exception as e:
            self.send_error(500, str(e))

    def _row_to_log(self, row):
        return {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "event_type": row["event_type"],
            "message": row["message"],
            "user": row["user"],
            "device": row["device"],
            "entity": row["entity"],
            "automation": row["automation"],
            "metadata": json.loads(row["metadata"]) if row["metadata"] else {}
        }

    def get_chain(self, query_params):
        """Reconstruct WHY a log happened by following the HA context chain.

        Every state change carries a 'context' pointing at what caused it - which
        may be an automation, another entity (device/media_player/script), a user,
        or nothing at all. We walk that cause chain upward and gather co-effects.
        """
        try:
            log_id = int(query_params.get('id', ['0'])[0])
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("SELECT * FROM logs WHERE id = ?", (log_id,))
            row = cursor.fetchone()
            if not row:
                conn.close()
                self.send_json({"error": "not found"})
                return

            target = self._row_to_log(row)

            def ts_of(log):
                try:
                    return datetime.fromisoformat(log["timestamp"])
                except Exception:
                    return datetime.now()

            def find_log_for_entity(entity, ts):
                """Most recent log for `entity` around time `ts` (the cause's own event)."""
                lo = (ts - timedelta(seconds=180)).isoformat()
                hi = (ts + timedelta(seconds=15)).isoformat()
                cursor.execute(
                    """SELECT * FROM logs WHERE entity = ? AND timestamp BETWEEN ? AND ?
                       ORDER BY timestamp DESC LIMIT 1""",
                    (entity, lo, hi)
                )
                r = cursor.fetchone()
                return self._row_to_log(r) if r else None

            # ---- Special case: the clicked log IS an automation firing ----
            # Show it automation-centric: trigger -> automation (expandable) -> its effects.
            if target["entity"] and str(target["entity"]).startswith("automation."):
                auto_entity = target["entity"]
                auto_name = (target.get("device")
                             or target["metadata"].get("context_entity_id_name")
                             or auto_entity)
                source = target["metadata"].get("context_source")
                if not source and target["message"] and "triggered by" in target["message"]:
                    source = target["message"].split("triggered by", 1)[1].strip()
                ran = target["event_type"] != "automation_failed"

                tts = ts_of(target)
                lo = (tts - timedelta(seconds=30)).isoformat()
                hi = (tts + timedelta(seconds=30)).isoformat()
                cursor.execute(
                    """SELECT * FROM logs
                       WHERE json_extract(metadata, '$.context_entity_id') = ?
                         AND timestamp BETWEEN ? AND ?
                       ORDER BY timestamp ASC LIMIT 100""",
                    (auto_entity, lo, hi)
                )
                eff = [self._row_to_log(r) for r in cursor.fetchall()]

                causes = []
                trig = self._find_trigger(cursor, source, tts)
                if trig and trig.get("entity") != auto_entity:
                    # Walk UP from the trigger entity (the helper): who/what changed IT?
                    if trig.get("log_id"):
                        cursor.execute("SELECT * FROM logs WHERE id = ?", (trig["log_id"],))
                        trow = cursor.fetchone()
                        if trow:
                            causes.extend(self._walk_up(cursor, self._row_to_log(trow), find_log_for_entity, ts_of))
                    # the trigger entity itself (the helper / sensor / device)
                    causes.append({
                        "type": "entity",
                        "entity": trig.get("entity"), "name": trig.get("name"),
                        "user": trig.get("user"), "log_id": trig.get("log_id"),
                        "message": trig.get("message"),
                    })
                causes.append({"type": "automation", "entity": auto_entity, "name": auto_name, "source": source})

                conn.close()
                self.send_json({
                    "target": target,
                    "causes": causes,
                    "automation": {"entity": auto_entity, "name": auto_name, "source": source, "ran": ran},
                    "effects": eff if eff else [],
                    "note": None,
                    "nearby": [],
                })
                return

            # ---- Walk the cause chain upward (root-first) ----
            causes = []
            automation = None
            visited = set()
            cur = target
            for _ in range(6):
                meta = cur["metadata"]
                cei = meta.get("context_entity_id")
                cuser = meta.get("context_user_name")
                csource = meta.get("context_source")
                cur_ts = ts_of(cur)

                if cei and cei != cur["entity"]:
                    is_auto = str(cei).startswith("automation.")
                    cause_log = None if is_auto else find_log_for_entity(cei, cur_ts)
                    node = {
                        "type": "automation" if is_auto else "entity",
                        "entity": cei,
                        "name": (meta.get("context_name")
                                 or (cause_log["device"] if cause_log else None)
                                 or cei),
                        "user": cuser,
                        "source": csource,
                        "log_id": cause_log["id"] if cause_log else None,
                        "state": (cause_log["metadata"].get("state") if cause_log else None),
                        "message": cause_log["message"] if cause_log else None,
                    }
                    causes.insert(0, node)
                    if is_auto:
                        automation = {"entity": cei, "name": meta.get("context_name") or cei, "source": csource}
                        trig = self._find_trigger(cursor, csource, cur_ts)
                        if trig and trig.get("entity") != cei:
                            # the trigger entity (helper) node
                            causes.insert(0, {
                                "type": "entity",
                                "entity": trig.get("entity"), "name": trig.get("name"),
                                "user": trig.get("user"), "log_id": trig.get("log_id"),
                                "message": trig.get("message"),
                            })
                            # walk UP from the trigger: who/what changed the helper?
                            if trig.get("log_id"):
                                cursor.execute("SELECT * FROM logs WHERE id = ?", (trig["log_id"],))
                                trow = cursor.fetchone()
                                if trow:
                                    upper = self._walk_up(cursor, self._row_to_log(trow), find_log_for_entity, ts_of)
                                    for n in reversed(upper):
                                        causes.insert(0, n)
                        break
                    if cei in visited or not cause_log:
                        break
                    visited.add(cei)
                    cur = cause_log
                    continue
                elif cuser:
                    causes.insert(0, {"type": "user", "name": cuser, "user": cuser})
                    break
                elif csource:
                    causes.insert(0, {"type": "state", "name": csource, "source": csource})
                    break
                else:
                    break

            # ---- Co-effects: other logs caused by the same direct context ----
            effects = []
            tcei = target["metadata"].get("context_entity_id")
            if tcei:
                tts = ts_of(target)
                lo = (tts - timedelta(seconds=30)).isoformat()
                hi = (tts + timedelta(seconds=30)).isoformat()
                cursor.execute(
                    """SELECT * FROM logs
                       WHERE json_extract(metadata, '$.context_entity_id') = ?
                         AND timestamp BETWEEN ? AND ?
                       ORDER BY timestamp ASC LIMIT 100""",
                    (tcei, lo, hi)
                )
                effects = [self._row_to_log(r) for r in cursor.fetchall()]
            if not effects:
                effects = [target]

            note = None
            nearby = []
            if not causes and not tcei:
                note = ("Home Assistant hat für dieses Ereignis keine Ursache protokolliert. "
                        "Das passiert z.B. wenn ein Gerät offline geht (unavailable), beim Neustart, "
                        "oder wenn eine externe Integration/App/Fernbedienung die Änderung direkt vornimmt "
                        "(ohne dass HA den Auslöser kennt).")
                # Investigative aid: what else happened around the same time?
                tts = ts_of(target)
                lo = (tts - timedelta(seconds=45)).isoformat()
                hi = (tts + timedelta(seconds=45)).isoformat()
                cursor.execute(
                    """SELECT * FROM logs WHERE timestamp BETWEEN ? AND ? AND id != ?
                       ORDER BY timestamp ASC LIMIT 80""",
                    (lo, hi, target["id"])
                )
                items = [self._row_to_log(r) for r in cursor.fetchall()]
                items.sort(key=lambda l: abs((datetime.fromisoformat(l["timestamp"]) - tts).total_seconds()))
                for l in items[:20]:
                    nearby.append({
                        "id": l["id"], "timestamp": l["timestamp"], "message": l["message"],
                        "event_type": l["event_type"], "entity": l["entity"],
                    })

            conn.close()
            self.send_json({
                "target": target,
                "causes": causes,
                "automation": automation,
                "effects": effects,
                "note": note,
                "nearby": nearby,
            })
        except Exception as e:
            self.send_json({"error": str(e)})

    def _walk_up(self, cursor, log, find_log_for_entity, ts_of, max_depth=6):
        """Return the cause chain ABOVE `log` (root first), following the HA context
        (who/what changed each entity) - user, another entity, a state source, etc."""
        out = []
        cur = log
        seen = set()
        for _ in range(max_depth):
            m = cur["metadata"]
            cei = m.get("context_entity_id")
            cuser = m.get("context_user_name")
            csrc = m.get("context_source")
            cts = ts_of(cur)
            if cei and cei != cur["entity"]:
                is_auto = str(cei).startswith("automation.")
                clog = None if is_auto else find_log_for_entity(cei, cts)
                out.insert(0, {
                    "type": "automation" if is_auto else "entity",
                    "entity": cei,
                    "name": m.get("context_name") or (clog["device"] if clog else None) or cei,
                    "user": cuser, "source": csrc,
                    "log_id": clog["id"] if clog else None,
                    "state": clog["metadata"].get("state") if clog else None,
                    "message": clog["message"] if clog else None,
                })
                if is_auto or cei in seen or not clog:
                    break
                seen.add(cei)
                cur = clog
                continue
            elif cuser:
                out.insert(0, {"type": "user", "name": cuser, "user": cuser})
                break
            elif csrc:
                out.insert(0, {"type": "state", "name": csrc, "source": csrc})
                break
            else:
                break
        return out

    def _find_trigger(self, cursor, source, ts):
        """Given a context_source like 'state of input_boolean.x', find the log that
        actually changed that entity just before ts (to learn who/what did it)."""
        if not source or " of " not in source:
            return None
        cand = source.split(" of ")[-1].strip()
        # cand should look like an entity_id
        if "." not in cand:
            return None
        lo = (ts - timedelta(seconds=30)).isoformat()
        hi = (ts + timedelta(seconds=5)).isoformat()
        cursor.execute(
            """SELECT * FROM logs
               WHERE entity = ? AND timestamp BETWEEN ? AND ?
               ORDER BY timestamp DESC LIMIT 1""",
            (cand, lo, hi)
        )
        r = cursor.fetchone()
        if r:
            log = self._row_to_log(r)
            return {
                "entity": log["entity"],
                "name": log.get("device") or log["entity"],
                "user": log["metadata"].get("context_user_name"),
                "log_id": log["id"],
                "message": log["message"],
            }
        # No log found, but we still know the trigger entity name
        return {"entity": cand, "name": cand, "user": None, "log_id": None, "message": None}

    def get_automation(self, query_params):
        """Fetch the real automation config (triggers/conditions/actions) from HA."""
        try:
            entity = query_params.get('entity', [None])[0]
            if not entity or requests is None:
                self.send_json({"error": "no entity or requests unavailable"})
                return

            # 1) Get the automation's unique id + attributes from its state
            unique_id = None
            friendly = entity
            current_state = None
            last_triggered = None
            try:
                r = requests.get(f"{HA_URL}/api/states/{entity}", headers=HA_HEADERS, timeout=15)
                if r.status_code == 200:
                    st = r.json()
                    attrs = st.get("attributes", {})
                    unique_id = attrs.get("id")
                    friendly = attrs.get("friendly_name", entity)
                    current_state = st.get("state")
                    last_triggered = attrs.get("last_triggered")
            except Exception:
                pass

            # 2) Fetch the real config (needs admin token + config integration)
            config = None
            config_error = None
            if unique_id:
                try:
                    r = requests.get(
                        f"{HA_URL}/api/config/automation/config/{unique_id}",
                        headers=HA_HEADERS, timeout=15
                    )
                    if r.status_code == 200:
                        config = r.json()
                    else:
                        config_error = f"HTTP {r.status_code}"
                except Exception as e:
                    config_error = str(e)
            else:
                config_error = "keine unique id"

            # Normalize trigger/condition/action to lists
            def as_list(v):
                if v is None:
                    return []
                return v if isinstance(v, list) else [v]

            result = {
                "entity": entity,
                "friendly_name": friendly,
                "state": current_state,
                "last_triggered": last_triggered,
                "config_available": config is not None,
                "config_error": config_error,
            }
            if config:
                result["alias"] = config.get("alias", friendly)
                result["mode"] = config.get("mode", "single")
                result["triggers"] = as_list(config.get("trigger") or config.get("triggers"))
                result["conditions"] = as_list(config.get("condition") or config.get("conditions"))
                result["actions"] = as_list(config.get("action") or config.get("actions"))
            self.send_json(result)
        except Exception as e:
            self.send_json({"error": str(e)})

    def get_trace(self, query_params):
        """Return the REAL trace results for an automation run closest to a given time:
        which conditions were true/false and which choose branch executed."""
        try:
            entity = query_params.get('entity', [None])[0]
            time_s = query_params.get('time', [None])[0]
            if not entity:
                self.send_json({"available": False, "reason": "no entity"})
                return

            # 0) Try the long-term ARCHIVE first (works even for old runs HA purged)
            try:
                conn = sqlite3.connect(str(DB_PATH))
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                row = None
                if time_s:
                    tt = datetime.fromisoformat(time_s)
                    lo = (tt - timedelta(minutes=10)).isoformat()
                    hi = (tt + timedelta(minutes=10)).isoformat()
                    cur.execute("SELECT * FROM traces WHERE automation_entity=? AND timestamp BETWEEN ? AND ?", (entity, lo, hi))
                    cands = cur.fetchall()
                    best, bestd = None, 1e18
                    for c in cands:
                        try:
                            d = abs((datetime.fromisoformat(c["timestamp"]) - tt).total_seconds())
                            if d < bestd:
                                bestd, best = d, c
                        except Exception:
                            pass
                    if best and bestd <= 120:
                        row = best
                else:
                    cur.execute("SELECT * FROM traces WHERE automation_entity=? ORDER BY timestamp DESC LIMIT 1", (entity,))
                    row = cur.fetchone()
                conn.close()
                if row:
                    self.send_json({
                        "available": True, "source": "archive", "run_id": row["run_id"],
                        "script_execution": row["script_execution"],
                        "choices": json.loads(row["choices"] or "{}"),
                        "conditions": json.loads(row["conditions"] or "{}"),
                        "trigger": row["trigger"],
                        "timestamp": {"start": row["timestamp"]},
                    })
                    return
            except Exception:
                pass

            if _wsclient is None:
                self.send_json({"available": False, "reason": "not archived and no websocket"})
                return

            amap = get_automation_map()
            info = amap.get(entity)
            if not info or not info.get("uid"):
                self.send_json({"available": False, "reason": "unknown automation"})
                return
            uid = info["uid"]

            res = ha_ws([{"type": "trace/list", "domain": "automation", "item_id": uid}])
            if not res or not res[0].get("success"):
                self.send_json({"available": False, "reason": "trace/list failed"})
                return
            traces = res[0].get("result", [])
            if not traces:
                self.send_json({"available": False, "reason": "no traces"})
                return

            # Pick the trace closest to the requested time.
            # HA keeps only the last ~5 traces per automation, so an older run's trace
            # is gone. If the closest trace is far from the requested time, it belongs to
            # a DIFFERENT run - don't report it (it would be misleading).
            target_tr = traces[-1]
            if time_s:
                try:
                    tt = datetime.fromisoformat(time_s)
                    def dist(tr):
                        try:
                            st = datetime.fromisoformat(tr["timestamp"]["start"])
                            if st.tzinfo is not None:
                                st = st.astimezone().replace(tzinfo=None)
                            return abs((st - tt).total_seconds())
                        except Exception:
                            return 1e18
                    target_tr = min(traces, key=dist)
                    if dist(target_tr) > 120:  # >2 min away = different run, original purged
                        self.send_json({"available": False, "reason": "trace_purged"})
                        return
                except Exception:
                    pass

            run_id = target_tr.get("run_id")
            full = ha_ws([{"type": "trace/get", "domain": "automation", "item_id": uid, "run_id": run_id}])
            if not full or not full[0].get("success"):
                self.send_json({"available": False, "reason": "trace/get failed"})
                return
            tr = full[0].get("result", {})
            steps = tr.get("trace", {})

            choices = {}      # e.g. "action/1" -> 0  (which choose branch ran)
            conditions = {}   # e.g. "action/1/choose/0/conditions/0" -> true/false
            for path, entries in steps.items():
                for e in entries:
                    r = e.get("result")
                    if isinstance(r, dict):
                        if "choice" in r:
                            choices[path] = r["choice"]
                        if isinstance(r.get("result"), bool):
                            conditions[path] = r["result"]

            trig = tr.get("trigger")
            trig_desc = trig.get("description") if isinstance(trig, dict) else trig

            self.send_json({
                "available": True,
                "run_id": run_id,
                "script_execution": tr.get("script_execution") or target_tr.get("script_execution"),
                "choices": choices,
                "conditions": conditions,
                "trigger": trig_desc,
                "timestamp": target_tr.get("timestamp"),
            })
        except Exception as e:
            self.send_json({"available": False, "reason": str(e)})

    def get_count(self, query_params):
        """Number of logs newer than a given id - powers the live indicator."""
        try:
            since_id = int(query_params.get('since_id', ['0'])[0])
            conn = sqlite3.connect(str(DB_PATH))
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*), MAX(id) FROM logs WHERE id > ?", (since_id,))
            row = cur.fetchone()
            cur.execute("SELECT MAX(id) FROM logs")
            max_id = cur.fetchone()[0] or 0
            conn.close()
            self.send_json({"new": row[0] or 0, "max_id": max_id})
        except Exception as e:
            self.send_json({"new": 0, "error": str(e)})

    def get_entity_stats(self, query_params):
        """Aggregated stats for one entity - powers the device detail page."""
        try:
            entity = query_params.get('entity', [None])[0]
            if not entity:
                self.send_json({"error": "no entity"})
                return
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM logs WHERE entity = ?", (entity,))
            total, first_ts, last_ts = cur.fetchone()

            cur.execute("SELECT device FROM logs WHERE entity = ? AND device IS NOT NULL LIMIT 1", (entity,))
            drow = cur.fetchone()
            device = drow[0] if drow else entity

            cur.execute("SELECT event_type, COUNT(*) c FROM logs WHERE entity = ? GROUP BY event_type ORDER BY c DESC", (entity,))
            by_type = [{"event_type": r[0], "count": r[1]} for r in cur.fetchall()]

            cur.execute("SELECT user, COUNT(*) c FROM logs WHERE entity = ? AND user IS NOT NULL GROUP BY user ORDER BY c DESC LIMIT 5", (entity,))
            top_users = [{"user": r[0], "count": r[1]} for r in cur.fetchall()]

            cur.execute("""SELECT json_extract(metadata,'$.context_entity_id') cei, COUNT(*) c
                           FROM logs WHERE entity = ? AND cei IS NOT NULL GROUP BY cei ORDER BY c DESC LIMIT 5""", (entity,))
            top_causes = [{"cause": r[0], "count": r[1]} for r in cur.fetchall()]

            conn.close()
            self.send_json({
                "entity": entity, "device": device, "total": total or 0,
                "first": first_ts, "last": last_ts,
                "by_type": by_type, "top_users": top_users, "top_causes": top_causes,
            })
        except Exception as e:
            self.send_json({"error": str(e)})

    def get_patterns(self, query_params):
        """Detect temporal patterns for one entity (helps answer 'why does this keep happening')."""
        try:
            entity = query_params.get('entity', [None])[0]
            if not entity:
                self.send_json({"findings": []})
                return
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT timestamp, message, metadata FROM logs WHERE entity = ? ORDER BY timestamp ASC", (entity,))
            rows = cur.fetchall()
            conn.close()

            findings = []
            times = []
            for r in rows:
                try:
                    times.append(datetime.fromisoformat(r["timestamp"]))
                except Exception:
                    pass
            n = len(times)
            if n < 3:
                self.send_json({"findings": ["Zu wenige Ereignisse für eine Musteranalyse."], "count": n})
                return

            # 1) Average interval + regularity
            deltas = [(times[i] - times[i - 1]).total_seconds() for i in range(1, n)]
            avg = sum(deltas) / len(deltas)
            var = sum((d - avg) ** 2 for d in deltas) / len(deltas)
            std = var ** 0.5
            def human(sec):
                if sec < 90: return f"{sec:.0f} Sek."
                if sec < 5400: return f"{sec/60:.0f} Min."
                if sec < 129600: return f"{sec/3600:.1f} Std."
                return f"{sec/86400:.1f} Tage"
            findings.append(f"Ø Abstand zwischen Ereignissen: {human(avg)} ({n} Ereignisse).")
            if avg > 0 and std / avg < 0.25:
                findings.append(f"⏱️ Sehr regelmäßig – tritt ca. alle {human(avg)} auf (geringe Streuung). Deutet auf einen Zeitplan/Timer hin.")

            # 2) Most common hour of day
            hours = {}
            for t in times:
                hours[t.hour] = hours.get(t.hour, 0) + 1
            top_hour, top_hour_c = max(hours.items(), key=lambda x: x[1])
            if top_hour_c / n > 0.3:
                findings.append(f"🕒 Häufung um {top_hour:02d}:00 Uhr ({top_hour_c} von {n} Ereignissen).")

            # 3) Most common preceding entity (co-occurrence within 10s before)
            cur2conn = sqlite3.connect(str(DB_PATH))
            cur2conn.row_factory = sqlite3.Row
            c2 = cur2conn.cursor()
            preceders = {}
            for t in times[-100:]:  # cap work
                lo = (t - timedelta(seconds=10)).isoformat()
                hi = t.isoformat()
                c2.execute("""SELECT entity FROM logs WHERE entity != ? AND entity IS NOT NULL
                              AND timestamp BETWEEN ? AND ? ORDER BY timestamp DESC LIMIT 1""", (entity, lo, hi))
                pr = c2.fetchone()
                if pr and pr[0]:
                    preceders[pr[0]] = preceders.get(pr[0], 0) + 1
            cur2conn.close()
            if preceders:
                pe, pc = max(preceders.items(), key=lambda x: x[1])
                if pc >= 3:
                    findings.append(f"🔗 Folgt oft kurz auf <b>{pe}</b> ({pc}×) – möglicher Zusammenhang.")

            self.send_json({"findings": findings, "count": n, "avg_interval_seconds": avg})
        except Exception as e:
            self.send_json({"findings": [], "error": str(e)})

    def send_json(self, data):
        json_data = json.dumps(data).encode('utf-8')
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', len(json_data))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(json_data)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # Client disconnected mid-response - ignore safely
            pass

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        # Never cache - always serve the freshest frontend (no hard-reload needed)
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def send_head(self):
        # SimpleHTTPRequestHandler uses If-Modified-Since -> 304; disable that so
        # the browser always gets the current file content.
        if 'If-Modified-Since' in self.headers:
            del self.headers['If-Modified-Since']
        if 'If-None-Match' in self.headers:
            del self.headers['If-None-Match']
        return super().send_head()

    def log_message(self, format, *args):
        pass


if __name__ == '__main__':
    www_dir = Path(__file__).parent / 'custom_components' / 'log_book' / 'www'

    if not www_dir.exists():
        print("Verzeichnis nicht gefunden: {www_dir}")
        sys.exit(1)

    port = 8080
    if len(sys.argv) > 1:
        port = int(sys.argv[1])

    server_address = ('0.0.0.0', port)
    # ThreadingHTTPServer: one bad/aborted connection can't take down the server
    httpd = ThreadingHTTPServer(server_address, LogBookHandler)
    httpd.daemon_threads = True

    # Ensure the trace archive table exists before serving requests
    _init_traces_table()

    # Start live importer in the background (pulls new HA logbook entries)
    importer = threading.Thread(target=live_importer, daemon=True)
    importer.start()

    # Start trace poller (logs automations blocked by conditions)
    tracer = threading.Thread(target=trace_poller, daemon=True)
    tracer.start()

    print("=" * 60)
    print("Log Book Server laeuft!")
    print("=" * 60)
    print(f"Oeffne: http://localhost:{port}")
    print(f"Oder im Netzwerk: http://<dieser-host>:{port}")
    print(f"\nAPI Port: {port}")
    print("Logs aus Datenbank: OK")
    print(f"Live-Import: alle {LIVE_POLL_SECONDS}s aus HA-Logbook")
    print("\nDruecke Ctrl+C zum Stoppen...")
    print("=" * 60)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n\nServer beendet")
        sys.exit(0)
