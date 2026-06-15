"""One-time historical import from the Home Assistant recorder (token-free).

The REST/history API does NOT expose event context, but the recorder DATABASE
stores context_id / parent_id / user_id per state. We read those directly so the
process topology (cause -> effect chains) and the user filter work for the past.
Falls back to the history API (no context) if the recorder query is unavailable.
"""
import json
import logging
import sqlite3
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)


def _count_rows(db_path):
    try:
        conn = sqlite3.connect(db_path)
        n = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def _clear_logs(db_path):
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM logs")
        conn.commit()
        conn.close()
    except Exception as err:
        _LOGGER.warning("Log Book: clear failed: %s", err)


def _bin_to_id(b):
    if not b:
        return None
    try:
        from homeassistant.util.ulid import bytes_to_ulid
        return bytes_to_ulid(b)
    except Exception:
        try:
            return b.hex()
        except Exception:
            return None


def _bin_to_hex(b):
    try:
        return b.hex() if b else None
    except Exception:
        return None


def _fetch_recorder(hass, cutoff_ts, limit=80000):
    """Read raw state rows WITH context from the recorder DB (runs in recorder executor)."""
    from homeassistant.components.recorder.db_schema import States, StatesMeta
    from homeassistant.components.recorder.util import session_scope

    try:
        _scope = session_scope(hass=hass, read_only=True)
    except TypeError:
        _scope = session_scope(hass=hass)

    out = []
    with _scope as session:
        q = (
            session.query(
                StatesMeta.entity_id,
                States.state,
                States.last_updated_ts,
                States.context_id_bin,
                States.context_parent_id_bin,
                States.context_user_id_bin,
            )
            .join(StatesMeta, States.metadata_id == StatesMeta.metadata_id)
            .filter(States.last_updated_ts >= cutoff_ts)
            .order_by(States.last_updated_ts.asc())
            .limit(limit)
        )
        for r in q.all():
            out.append((r[0], r[1], r[2], r[3], r[4], r[5]))
    return out


def _build_rows_from_recorder(raw, friendly_map, skip_set, person_map):
    rows = []
    for entity_id, state, ts, cid_bin, pid_bin, uid_bin in raw:
        try:
            if state is None or state in ("unknown", "unavailable"):
                continue
            if entity_id in skip_set:
                continue
            domain = entity_id.split(".")[0]
            friendly = friendly_map.get(entity_id, entity_id)
            uid = _bin_to_hex(uid_bin)
            user_ctx = person_map.get(uid)

            user = device = automation = None
            if domain == "person":
                event_type = "user_action"; user = friendly
            elif domain == "automation":
                event_type = "automation_triggered"; automation = friendly; device = friendly
            else:
                device = friendly
                if user_ctx:
                    event_type = "user_action"; user = user_ctx
                else:
                    event_type = "state_change"

            try:
                tstr = datetime.fromtimestamp(ts).isoformat() if ts else None
            except Exception:
                tstr = None
            metadata = {
                "context_id": _bin_to_id(cid_bin),
                "context_parent_id": _bin_to_id(pid_bin),
                "context_user_id": uid,
                "context_user_name": user_ctx or (friendly if domain == "person" else None),
                "state": state,
            }
            rows.append((tstr, event_type, f"{friendly}: {state}", user, device,
                         entity_id, automation, json.dumps(metadata)))
        except Exception:
            continue
    return rows


async def async_backfill(hass, db, collector, days=3, force=False):
    """Import the last `days` of recorder history (with context). Skips if the DB
    already has rows unless force=True."""
    existing = await hass.async_add_executor_job(_count_rows, db.db_path)
    if existing and not force:
        _LOGGER.warning("Log Book backfill: DB already has %d rows - skipping "
                        "(use service log_book.reimport to force).", existing)
        return
    if force and existing:
        await hass.async_add_executor_job(_clear_logs, db.db_path)

    # Refresh person map (HA started -> persons loaded)
    try:
        from .collector import build_person_map
        collector.person_map = build_person_map(hass)
    except Exception:
        pass
    person_map = collector.person_map or {}

    # Build friendly-name map + sensor-measurement skip set from current states
    friendly_map, skip_set = {}, set()
    for st in hass.states.async_all():
        friendly_map[st.entity_id] = st.attributes.get("friendly_name") or st.entity_id
        if st.entity_id.split(".")[0] == "sensor" and (
            st.attributes.get("unit_of_measurement") or st.attributes.get("state_class")
        ):
            skip_set.add(st.entity_id)

    cutoff_ts = (datetime.now() - timedelta(days=days)).timestamp()

    rows = None
    # Primary: recorder DB (has context for chains)
    try:
        from homeassistant.components.recorder import get_instance
        _LOGGER.warning("Log Book backfill: reading recorder DB (%d days)…", days)
        raw = await get_instance(hass).async_add_executor_job(_fetch_recorder, hass, cutoff_ts)
        rows = await hass.async_add_executor_job(
            _build_rows_from_recorder, raw, friendly_map, skip_set, person_map
        )
        _LOGGER.warning("Log Book backfill: recorder DB returned %d states -> %d log rows",
                        len(raw), len(rows))
    except Exception as err:
        _LOGGER.warning("Log Book backfill: recorder DB query failed (%s) - trying history API", err)

    # Fallback: history API (no context, but populates the log list + users)
    if not rows:
        try:
            from homeassistant.components.recorder import history, get_instance
            import homeassistant.util.dt as dt_util
            start = dt_util.utcnow() - timedelta(days=days)
            entity_ids = list(hass.states.async_entity_ids())

            def _fetch_hist():
                return history.get_significant_states(
                    hass, start, dt_util.utcnow(), entity_ids,
                    include_start_time_state=False, significant_changes_only=True,
                    minimal_response=False, no_attributes=False,
                )

            states_dict = await get_instance(hass).async_add_executor_job(_fetch_hist)
            rows = []
            for entity_id, states in (states_dict or {}).items():
                if entity_id in skip_set:
                    continue
                domain = entity_id.split(".")[0]
                for s in states:
                    state = getattr(s, "state", None)
                    if state in (None, "unknown", "unavailable"):
                        continue
                    friendly = friendly_map.get(entity_id, entity_id)
                    user = device = automation = None
                    if domain == "person":
                        event_type = "user_action"; user = friendly
                    elif domain == "automation":
                        event_type = "automation_triggered"; automation = friendly; device = friendly
                    else:
                        event_type = "state_change"; device = friendly
                    lc = getattr(s, "last_changed", None)
                    tstr = lc.astimezone().replace(tzinfo=None).isoformat() if lc else None
                    meta = {"context_id": None, "context_parent_id": None,
                            "context_user_name": (friendly if domain == "person" else None),
                            "state": state}
                    rows.append((tstr, event_type, f"{friendly}: {state}", user, device,
                                 entity_id, automation, json.dumps(meta)))
            rows.sort(key=lambda r: r[0] or "")
            _LOGGER.warning("Log Book backfill: history fallback -> %d rows (no chain context)", len(rows))
        except Exception as err:
            _LOGGER.warning("Log Book backfill: history fallback failed: %s", err)
            rows = []

    if rows:
        with_ctx = sum(1 for r in rows if '"context_parent_id": null' not in r[7] and '"context_parent_id":null' not in r[7])
        await hass.async_add_executor_job(db.add_logs_batch, rows)
        _LOGGER.warning("Log Book backfill: DONE - %d events imported (%d with cause-context).",
                        len(rows), with_ctx)
    else:
        _LOGGER.warning("Log Book backfill: no rows imported.")
