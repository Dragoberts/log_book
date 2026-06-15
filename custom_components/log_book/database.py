"""Database management for Log Book."""
import json
import sqlite3
from datetime import datetime
from typing import Any, List, Optional


class LogDatabase:
    """SQLite database for Log Book."""

    def __init__(self, db_path: str):
        """Initialize database."""
        self.db_path = db_path

    def init_db(self) -> None:
        """Initialize database tables."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute(
            """
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
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_timestamp ON logs(timestamp DESC)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_event_type ON logs(event_type)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user ON logs(user)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_device ON logs(device)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entity ON logs(entity)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_automation ON logs(automation)
            """
        )

        conn.commit()
        conn.close()

    def add_log(
        self,
        event_type: str,
        message: str,
        user: Optional[str] = None,
        device: Optional[str] = None,
        entity: Optional[str] = None,
        automation: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> int:
        """Add a log entry."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        metadata_json = json.dumps(metadata or {})

        cursor.execute(
            """
            INSERT INTO logs (event_type, message, user, device, entity, automation, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_type, message, user, device, entity, automation, metadata_json),
        )

        conn.commit()
        log_id = cursor.lastrowid
        conn.close()

        return log_id

    def add_logs_batch(self, rows: list) -> None:
        """Insert many rows in a single transaction.

        Each row: (timestamp, event_type, message, user, device, entity,
                   automation, metadata_json)
        """
        if not rows:
            return
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS logs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,"
                "event_type TEXT NOT NULL, message TEXT NOT NULL,"
                "user TEXT, device TEXT, entity TEXT, automation TEXT, metadata TEXT)"
            )
            conn.executemany(
                "INSERT INTO logs (timestamp, event_type, message, user, device, entity, automation, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

    def get_logs(
        self,
        limit: int = 100,
        offset: int = 0,
        event_type: Optional[str] = None,
        user: Optional[str] = None,
        device: Optional[str] = None,
        entity: Optional[str] = None,
        automation: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[dict]:
        """Get logs with filters."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = "SELECT * FROM logs WHERE 1=1"
        params = []

        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)

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

        if start_date:
            query += " AND timestamp >= ?"
            params.append(start_date.isoformat())

        if end_date:
            query += " AND timestamp <= ?"
            params.append(end_date.isoformat())

        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()

        return [
            {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "event_type": row["event_type"],
                "message": row["message"],
                "user": row["user"],
                "device": row["device"],
                "entity": row["entity"],
                "automation": row["automation"],
                "metadata": json.loads(row["metadata"]),
            }
            for row in rows
        ]

    def get_distinct_values(self, column: str) -> List[str]:
        """Get distinct values for a column."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute(f"SELECT DISTINCT {column} FROM logs WHERE {column} IS NOT NULL")
        values = [row[0] for row in cursor.fetchall()]
        conn.close()

        return sorted(values)

    def clear_old_logs(self, days: int = 30) -> int:
        """Delete logs older than specified days."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute(
            """
            DELETE FROM logs
            WHERE timestamp < datetime('now', '-' || ? || ' days')
            """,
            (days,),
        )

        conn.commit()
        deleted = cursor.rowcount
        conn.close()

        return deleted
