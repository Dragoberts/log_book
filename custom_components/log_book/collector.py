"""Captures Home Assistant events internally (no token) and writes them to the
Log Book database. This replaces the standalone server's token-based importer."""
import json
import logging

from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.const import EVENT_STATE_CHANGED

_LOGGER = logging.getLogger(__name__)

# State changes we don't want to log (noise)
_SKIP_STATES = {"unknown"}


def build_person_map(hass: HomeAssistant) -> dict:
    """Map HA user_id -> person friendly name (so we can attribute actions to a user)."""
    pmap = {}
    try:
        for st in hass.states.async_all("person"):
            uid = st.attributes.get("user_id")
            if uid:
                pmap[uid] = st.attributes.get("friendly_name") or st.entity_id.split(".")[1]
    except Exception:  # pragma: no cover
        pass
    return pmap


class LogCollector:
    """Subscribes to the event bus and persists meaningful events."""

    def __init__(self, hass: HomeAssistant, db):
        self.hass = hass
        self.db = db
        self.person_map = {}
        self._unsub = None

    @callback
    def async_start(self):
        self.person_map = build_person_map(self.hass)
        self._unsub = self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._handle_state_changed)
        _LOGGER.info("Log Book collector started (token-free, internal event bus)")

    @callback
    def async_stop(self):
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _handle_state_changed(self, event: Event):
        try:
            entity_id = event.data.get("entity_id")
            new_state = event.data.get("new_state")
            if not entity_id or new_state is None:
                return
            state = new_state.state
            if state in _SKIP_STATES:
                return

            domain = entity_id.split(".")[0]
            attrs = new_state.attributes or {}
            friendly = attrs.get("friendly_name") or entity_id
            ctx = event.context

            # Refresh the person map lazily when a person changes
            if domain == "person":
                self.person_map = build_person_map(self.hass)

            user_name = self.person_map.get(getattr(ctx, "user_id", None))

            if domain == "automation":
                event_type = "automation_triggered"
            elif user_name:
                event_type = "user_action"
            else:
                event_type = "state_change"

            device = friendly
            message = f"{friendly}: {state}"

            metadata = {
                "context_id": getattr(ctx, "id", None),
                "context_parent_id": getattr(ctx, "parent_id", None),
                "context_user_id": getattr(ctx, "user_id", None),
                "context_user_name": user_name,
                "state": state,
            }

            # Write off the event loop
            self.hass.async_add_executor_job(
                self.db.add_log, event_type, message, user_name, device, entity_id, None, metadata
            )
        except Exception as err:  # pragma: no cover - never break the event loop
            _LOGGER.debug("Log Book collector error: %s", err)
