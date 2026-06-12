"""One-time historical import from the Home Assistant recorder (token-free).

The recorder stores past state changes WITH their context (id / parent_id /
user_id), so we can backfill the log list, the user filter AND the process
topology without any access token.
"""
import json
import logging
import sqlite3
from datetime import timedelta

_LOGGER = logging.getLogger(__name__)


def _count_rows(db_path):
    try:
        conn = sqlite3.connect(db_path)
        n = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        conn.close()
        return n
    except Exception:
        return 0


def _build_rows(states_dict, person_map):
    rows = []
    for entity_id, states in (states_dict or {}).items():
        domain = entity_id.split(".")[0]
        for st in states:
            try:
                state = getattr(st, "state", None)
                if state is None or state == "unknown":
                    continue
                attrs = getattr(st, "attributes", {}) or {}
                if domain == "sensor" and (attrs.get("unit_of_measurement") or attrs.get("state_class")):
                    continue
                friendly = attrs.get("friendly_name") or entity_id
                ctx = getattr(st, "context", None)
                uid = getattr(ctx, "user_id", None) if ctx else None
                user_ctx = person_map.get(uid)

                user = device = automation = None
                if domain == "person":
                    event_type = "user_action"
                    user = friendly
                elif domain == "automation":
                    event_type = "automation_triggered"
                    automation = friendly
                    device = friendly
                else:
                    device = friendly
                    if user_ctx:
                        event_type = "user_action"
                        user = user_ctx
                    else:
                        event_type = "state_change"

                lc = getattr(st, "last_changed", None)
                ts = lc.astimezone().replace(tzinfo=None).isoformat() if lc else None
                metadata = {
                    "context_id": getattr(ctx, "id", None) if ctx else None,
                    "context_parent_id": getattr(ctx, "parent_id", None) if ctx else None,
                    "context_user_id": uid,
                    "context_user_name": user_ctx or (friendly if domain == "person" else None),
                    "state": state,
                }
                rows.append((ts, event_type, f"{friendly}: {state}", user, device,
                             entity_id, automation, json.dumps(metadata)))
            except Exception:
                continue
    rows.sort(key=lambda r: r[0] or "")
    return rows


def _clear_logs(db_path):
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM logs")
        conn.commit()
        conn.close()
    except Exception as err:
        _LOGGER.warning("Log Book: clear failed: %s", err)


async def async_backfill(hass, db, collector, days=3, force=False):
    """Import the last `days` of recorder history. Skips if DB already has rows
    unless force=True (manual reimport)."""
    existing = await hass.async_add_executor_job(_count_rows, db.db_path)
    if existing and not force:
        _LOGGER.warning("Log Book backfill: DB already has %d rows - skipping "
                        "(call service log_book.reimport to force).", existing)
        return
    if force and existing:
        await hass.async_add_executor_job(_clear_logs, db.db_path)

    try:
        from homeassistant.components.recorder import history, get_instance
        import homeassistant.util.dt as dt_util
    except Exception as err:
        _LOGGER.warning("Log Book backfill: recorder unavailable: %s", err)
        return

    # Refresh the person map now that HA is started (persons are loaded)
    try:
        from .collector import build_person_map
        collector.person_map = build_person_map(hass)
    except Exception:
        pass

    start = dt_util.utcnow() - timedelta(days=days)
    end = dt_util.utcnow()
    entity_ids = list(hass.states.async_entity_ids())  # modern recorder needs explicit ids
    _LOGGER.warning("Log Book backfill: importing %d days for %d entities…", days, len(entity_ids))

    def _fetch():
        return history.get_significant_states(
            hass, start, end, entity_ids,
            include_start_time_state=False,
            significant_changes_only=True,
            minimal_response=False,
            no_attributes=False,
        )

    try:
        states_dict = await get_instance(hass).async_add_executor_job(_fetch)
    except Exception as err:
        _LOGGER.warning("Log Book backfill: query failed: %s", err)
        return

    person_map = collector.person_map or {}
    rows = await hass.async_add_executor_job(_build_rows, states_dict, person_map)
    # how many rows actually carry context (for chains)?
    with_ctx = sum(1 for r in rows if '"context_parent_id": null' not in r[7])
    if rows:
        await hass.async_add_executor_job(db.add_logs_batch, rows)
    _LOGGER.warning("Log Book backfill: done - %d events imported (%d with context for chains).",
                    len(rows), with_ctx)
