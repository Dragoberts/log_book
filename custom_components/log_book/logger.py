"""Log service for Log Book."""
from datetime import datetime
from typing import Any, Optional

from .database import LogDatabase


class LogService:
    """Service for logging events."""

    def __init__(self, database: LogDatabase):
        """Initialize log service."""
        self.database = database

    def log_event(
        self,
        event_type: str,
        message: str,
        user: Optional[str] = None,
        device: Optional[str] = None,
        entity: Optional[str] = None,
        automation: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> int:
        """Log an event."""
        return self.database.add_log(
            event_type=event_type,
            message=message,
            user=user,
            device=device,
            entity=entity,
            automation=automation,
            metadata=metadata,
        )

    def log_device_action(
        self,
        device_id: str,
        device_name: str,
        action: str,
        user: Optional[str] = None,
        automation: Optional[str] = None,
    ) -> int:
        """Log a device action (turn on/off, lock/unlock, etc)."""
        if user and automation:
            message = f"{device_name} wurde {action} durch {user} und {automation}"
        elif user:
            message = f"{device_name} wurde {action} durch {user}"
        elif automation:
            message = f"{device_name} wurde {action} durch {automation}"
        else:
            message = f"{device_name} wurde {action}"

        return self.log_event(
            event_type="device_action",
            message=message,
            device=device_id,
            user=user,
            automation=automation,
        )

    def log_automation_trigger(
        self,
        automation_id: str,
        automation_name: str,
        action: str,
        affected_devices: Optional[list] = None,
    ) -> int:
        """Log an automation trigger."""
        message = f"Automatisierung '{automation_name}' hat {action} ausgelöst"
        if affected_devices:
            device_list = ", ".join(affected_devices)
            message += f" ({device_list})"

        return self.log_event(
            event_type="automation_trigger",
            message=message,
            automation=automation_id,
            metadata={"affected_devices": affected_devices or []},
        )

    def get_logs(
        self,
        limit: int = 100,
        offset: int = 0,
        event_type: Optional[str] = None,
        user: Optional[str] = None,
        device: Optional[str] = None,
        automation: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> list:
        """Get logs with filters."""
        return self.database.get_logs(
            limit=limit,
            offset=offset,
            event_type=event_type,
            user=user,
            device=device,
            automation=automation,
            start_date=start_date,
            end_date=end_date,
        )

    def get_distinct_users(self) -> list:
        """Get all distinct users."""
        return self.database.get_distinct_values("user")

    def get_distinct_devices(self) -> list:
        """Get all distinct devices."""
        return self.database.get_distinct_values("device")

    def get_distinct_automations(self) -> list:
        """Get all distinct automations."""
        return self.database.get_distinct_values("automation")

    def get_distinct_event_types(self) -> list:
        """Get all distinct event types."""
        return self.database.get_distinct_values("event_type")

    def get_distinct_entities(self) -> list:
        """Get all distinct entities."""
        return self.database.get_distinct_values("entity")
