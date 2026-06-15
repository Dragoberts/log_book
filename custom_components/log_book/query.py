"""Read-side queries for Log Book (runs in executor; pure SQLite, no HA token)."""
import json
import sqlite3

_CREATE_LOGS = """
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    user TEXT,
    device TEXT,
    entity TEXT,
    automation TEXT,
    metadata TEXT
)
"""

def _open(db_path):
    """Open DB and ensure the logs table exists (defensive: init may have been skipped)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_LOGS)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON logs(timestamp DESC)")
    conn.commit()
    return conn


def _row_to_log(row):
    return {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "event_type": row["event_type"],
        "message": row["message"],
        "user": row["user"],
        "device": row["device"],
        "entity": row["entity"],
        "automation": row["automation"],
        "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
    }


def get_logs(db_path, *, limit=50, offset=0, user=None, device=None, entity=None,
             automation=None, event_type=None, exclude_event_type=None,
             date_from=None, date_to=None, q=None, chain_only=None, hidden=None):
    limit = min(int(limit or 50), 1000000)
    offset = int(offset or 0)
    conn = _open(db_path)
    cur = conn.cursor()

    query = "SELECT * FROM logs WHERE 1=1"
    params = []
    if event_type:
        query += " AND event_type = ?"; params.append(event_type)
    if exclude_event_type:
        query += " AND event_type != ?"; params.append(exclude_event_type)
    if user:
        query += " AND user = ?"; params.append(user)
    if device:
        query += " AND device = ?"; params.append(device)
    if entity:
        query += " AND entity = ?"; params.append(entity)
    if automation:
        query += " AND automation = ?"; params.append(automation)
    if q:
        like = f"%{q}%"
        query += (" AND (message LIKE ? OR entity LIKE ? OR device LIKE ?"
                  " OR COALESCE(user,'') LIKE ? OR COALESCE(automation,'') LIKE ?)")
        params.extend([like, like, like, like, like])
    if date_from:
        query += " AND substr(timestamp, 1, 10) >= ?"; params.append(date_from)
    if date_to:
        query += " AND substr(timestamp, 1, 10) <= ?"; params.append(date_to)
    if chain_only:
        query += (" AND (json_extract(metadata, '$.context_entity_id') LIKE 'automation.%'"
                  " OR entity LIKE 'automation.%' OR event_type = 'automation_failed')")
    if hidden:
        ph = ",".join("?" for _ in hidden)
        query += f" AND COALESCE(entity,'') NOT IN ({ph}) AND COALESCE(message,'') NOT IN ({ph})"
        params.extend(hidden); params.extend(hidden)

    query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    cur.execute(query, params)
    logs = [_row_to_log(r) for r in cur.fetchall()]
    conn.close()
    return {"logs": logs, "limit": limit, "offset": offset}


def get_filters(db_path):
    conn = _open(db_path)
    cur = conn.cursor()

    def distinct(col):
        cur.execute(f"SELECT DISTINCT {col} FROM logs WHERE {col} IS NOT NULL ORDER BY {col}")
        return [r[0] for r in cur.fetchall()]

    result = {
        "users": distinct("user"),
        "devices": distinct("device"),
        "entities": distinct("entity"),
        "automations": distinct("automation"),
        "event_types": distinct("event_type"),
    }
    conn.close()
    return result


def get_count(db_path, since_id=0):
    conn = _open(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM logs WHERE id > ?", (int(since_id or 0),))
    new = cur.fetchone()[0] or 0
    cur.execute("SELECT MAX(id) FROM logs")
    max_id = cur.fetchone()[0] or 0
    conn.close()
    return {"new": new, "max_id": max_id}


# ---------------- Process topology (via HA context parent_id) ----------------

def _node_from_log(log):
    ent = log["entity"] or ""
    if ent.startswith("automation."):
        ntype = "automation"
    elif ent.startswith("person."):
        ntype = "user"
    else:
        ntype = "entity"
    meta = log["metadata"]
    return {
        "type": ntype,
        "entity": ent or None,
        "name": log["device"] or log["user"] or ent or log["message"],
        "user": meta.get("context_user_name") or log["user"],
        "log_id": log["id"],
        "state": meta.get("state"),
        "message": log["message"],
        "source": None,
    }


def get_chain(db_path, log_id):
    conn = _open(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM logs WHERE id = ?", (int(log_id),))
    row = cur.fetchone()
    if not row:
        conn.close()
        return {"error": "not found"}
    target = _row_to_log(row)
    meta = target["metadata"]

    # Walk UP the cause chain using context.parent_id -> parent's context.id
    causes = []
    visited = set()
    cur_log = target
    for _ in range(6):
        m = cur_log["metadata"]
        pid = m.get("context_parent_id")
        cid = m.get("context_id")
        if not pid or pid == cid or pid in visited:
            # No further parent. If this event was done by a user, show the user as root.
            un = m.get("context_user_name")
            if un and (not causes or causes[0].get("type") != "user"):
                causes.insert(0, {"type": "user", "name": un, "user": un})
            break
        visited.add(pid)
        cur.execute(
            "SELECT * FROM logs WHERE json_extract(metadata,'$.context_id') = ? AND id != ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (pid, cur_log["id"]),
        )
        prow = cur.fetchone()
        if not prow:
            un = m.get("context_user_name")
            if un:
                causes.insert(0, {"type": "user", "name": un, "user": un})
            break
        parent = _row_to_log(prow)
        causes.insert(0, _node_from_log(parent))
        cur_log = parent

    # Effects: all events that share the target's context id (same "run")
    effects = []
    cid = meta.get("context_id")
    if cid:
        cur.execute(
            "SELECT * FROM logs WHERE json_extract(metadata,'$.context_id') = ? "
            "ORDER BY timestamp ASC LIMIT 100",
            (cid,),
        )
        effects = [_row_to_log(r) for r in cur.fetchall()]
    if not effects:
        effects = [target]

    automation = None
    for c in causes:
        if c.get("type") == "automation":
            automation = {"entity": c.get("entity"), "name": c.get("name"), "source": c.get("source")}
            break

    note = None
    nearby = []
    if not causes and not meta.get("context_parent_id"):
        note = ("Home Assistant hat für dieses Ereignis keine Ursache protokolliert "
                "(z.B. ein gepollter Sensor-Messwert, ein Gerät das offline geht, oder eine "
                "externe Änderung).")
        cur.execute(
            "SELECT * FROM logs WHERE id BETWEEN ? AND ? AND id != ? ORDER BY id DESC LIMIT 20",
            (target["id"] - 25, target["id"] + 25, target["id"]),
        )
        for r in cur.fetchall():
            l = _row_to_log(r)
            nearby.append({"id": l["id"], "timestamp": l["timestamp"], "message": l["message"],
                           "event_type": l["event_type"], "entity": l["entity"]})

    conn.close()
    return {"target": target, "causes": causes, "automation": automation,
            "effects": effects, "note": note, "nearby": nearby}


# ---------------- Device detail stats ----------------

def get_entity_stats(db_path, entity):
    conn = _open(db_path)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) c, MIN(timestamp) mn, MAX(timestamp) mx FROM logs WHERE entity = ?", (entity,))
    r = cur.fetchone()
    total, first_ts, last_ts = r["c"], r["mn"], r["mx"]

    cur.execute("SELECT device FROM logs WHERE entity = ? AND device IS NOT NULL LIMIT 1", (entity,))
    dr = cur.fetchone()
    device = dr["device"] if dr else entity

    cur.execute("SELECT event_type, COUNT(*) c FROM logs WHERE entity = ? GROUP BY event_type ORDER BY c DESC", (entity,))
    by_type = [{"event_type": x["event_type"], "count": x["c"]} for x in cur.fetchall()]

    cur.execute("SELECT user, COUNT(*) c FROM logs WHERE entity = ? AND user IS NOT NULL GROUP BY user ORDER BY c DESC LIMIT 5", (entity,))
    top_users = [{"user": x["user"], "count": x["c"]} for x in cur.fetchall()]

    # Top causes: parent events (joined via context parent_id -> context id)
    cur.execute(
        "SELECT p.entity ent, COUNT(*) c FROM logs ch "
        "JOIN logs p ON json_extract(p.metadata,'$.context_id') = json_extract(ch.metadata,'$.context_parent_id') "
        "WHERE ch.entity = ? AND p.entity IS NOT NULL AND p.entity != ch.entity "
        "GROUP BY p.entity ORDER BY c DESC LIMIT 5",
        (entity,),
    )
    top_causes = [{"cause": x["ent"], "count": x["c"]} for x in cur.fetchall()]

    conn.close()
    return {"entity": entity, "device": device, "total": total or 0,
            "first": first_ts, "last": last_ts,
            "by_type": by_type, "top_users": top_users, "top_causes": top_causes}


# ---------------- Pattern detection ----------------

def get_patterns(db_path, entity):
    conn = _open(db_path)
    cur = conn.cursor()
    cur.execute("SELECT timestamp FROM logs WHERE entity = ? ORDER BY timestamp ASC", (entity,))
    times = []
    from datetime import datetime
    for r in cur.fetchall():
        try:
            times.append(datetime.fromisoformat(r["timestamp"]))
        except Exception:
            pass
    n = len(times)
    if n < 3:
        conn.close()
        return {"findings": ["Zu wenige Ereignisse für eine Musteranalyse."], "count": n}

    findings = []
    deltas = [(times[i] - times[i - 1]).total_seconds() for i in range(1, n)]
    avg = sum(deltas) / len(deltas)
    std = (sum((d - avg) ** 2 for d in deltas) / len(deltas)) ** 0.5

    def human(sec):
        if sec < 90: return f"{sec:.0f} Sek."
        if sec < 5400: return f"{sec/60:.0f} Min."
        if sec < 129600: return f"{sec/3600:.1f} Std."
        return f"{sec/86400:.1f} Tage"

    findings.append(f"Ø Abstand zwischen Ereignissen: {human(avg)} ({n} Ereignisse).")
    if avg > 0 and std / avg < 0.25:
        findings.append(f"⏱️ Sehr regelmäßig – ca. alle {human(avg)} (geringe Streuung). Deutet auf Zeitplan/Polling hin.")

    hours = {}
    for t in times:
        hours[t.hour] = hours.get(t.hour, 0) + 1
    th, tc = max(hours.items(), key=lambda x: x[1])
    if tc / n > 0.3:
        findings.append(f"🕒 Häufung um {th:02d}:00 Uhr ({tc} von {n}).")

    preceders = {}
    for t in times[-100:]:
        from datetime import timedelta
        lo = (t - timedelta(seconds=10)).isoformat()
        cur.execute(
            "SELECT entity FROM logs WHERE entity != ? AND entity IS NOT NULL "
            "AND timestamp BETWEEN ? AND ? ORDER BY timestamp DESC LIMIT 1",
            (entity, lo, t.isoformat()),
        )
        pr = cur.fetchone()
        if pr and pr["entity"]:
            preceders[pr["entity"]] = preceders.get(pr["entity"], 0) + 1
    if preceders:
        pe, pc = max(preceders.items(), key=lambda x: x[1])
        if pc >= 3:
            findings.append(f"🔗 Folgt oft kurz auf <b>{pe}</b> ({pc}×) – möglicher Zusammenhang.")

    conn.close()
    return {"findings": findings, "count": n, "avg_interval_seconds": avg}
