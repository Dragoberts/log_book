"""Read-side queries for Log Book (runs in executor; pure SQLite, no HA token)."""
import json
import sqlite3


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
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
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
    conn = sqlite3.connect(db_path)
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
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM logs WHERE id > ?", (int(since_id or 0),))
    new = cur.fetchone()[0] or 0
    cur.execute("SELECT MAX(id) FROM logs")
    max_id = cur.fetchone()[0] or 0
    conn.close()
    return {"new": new, "max_id": max_id}
