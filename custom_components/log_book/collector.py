"""Captures Home Assistant events internally (no token) and writes them to the
Log Book database in batches. Replaces the standalone server's token importer."""
import json
import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.helpers.event import async_track_time_interval

_LOGGER = logging.getLogger(__name__)

_FLUSH_INTERVAL = timedelta(seconds=2)
_SKIP_STATES = {"unknown"}


def build_person_map(hass: HomeAssistant) -> dict:
    """Map HA user_id -> person friendly name."""
    pmap = {}
    try:
        for st in hass.states.async_all("person"):
            uid = st.attributes.get("user_id")
            if uid:
                pmap[uid] = st.attributes.get("friendly_name") or st.entity_id.split(".")[1]
    except Exception:  # pragma: no cover
        pass
    return pmap


def _local_iso(dt):
    """A timezone-aware UTC datetime -> local naive ISO (frontend-friendly)."""
    try:
        return dt.astimezone().replace(tzinfo=None).isoformat()
    except Exception:
        return None


class LogCollector:
    """Subscribes to the event bus, filters noise like the HA logbook, batches writes."""

    def __init__(self, hass: HomeAssistant, db):
        self.hass = hass
        self.db = db
        self.person_map = {}
        self._unsub = None
        self._unsub_timer = None
        self._queue = []

    @callback
    def async_start(self):
        self.person_map = build_person_map(self.hass)
        self._unsub = self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._handle)
        self._unsub_timer = async_track_time_interval(self.hass, self._flush, _FLUSH_INTERVAL)
        _LOGGER.info("Log Book collector started (token-free, batched every 2s)")

    @callback
    def async_stop(self):
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _handle(self, event: Event):
        try:
            row = self._build_row(event)
            if row:
                self._queue.append(row)
        except Exception as err:  # pragma: no cover - never break the loop
            _LOGGER.debug("collector build error: %s", err)

    def _build_row(self, event: Event):
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if not entity_id or new_state is None:
            return None

        state = new_state.state
        if state in _SKIP_STATES:
            return None
        # Ignore attribute-only changes (state value unchanged)
        if old_state is not None and old_state.state == state:
            return None

        domain = entity_id.split(".")[0]
        attrs = new_state.attributes or {}

        # Skip continuously-changing sensor measurements (HA logbook excludes these too)
        if domain == "sensor" and (attrs.get("unit_of_measurement") or attrs.get("state_class")):
            return None

        friendly = attrs.get("friendly_name") or entity_id
        ctx = event.context

        if domain == "person":
            self.person_map = build_person_map(self.hass)

        uid = getattr(ctx, "user_id", None)
        user_ctx = self.person_map.get(uid)
        # Lazy refresh: person entities may not have been loaded at setup time
        if uid and not user_ctx:
            self.person_map = build_person_map(self.hass)
            user_ctx = self.person_map.get(uid)

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

        message = f"{friendly}: {state}"
        metadata = {
            "context_id": getattr(ctx, "id", None),
            "context_parent_id": getattr(ctx, "parent_id", None),
            "context_user_id": getattr(ctx, "user_id", None),
            "context_user_name": user_ctx or (friendly if domain == "person" else None),
            "state": state,
        }
        timestamp = _local_iso(event.time_fired)
        return (timestamp, event_type, message, user, device, entity_id, automation, json.dumps(metadata))

    async def _flush(self, now=None):
        if not self._queue:
            return
        rows = self._queue
        self._queue = []
        try:
            await self.hass.async_add_executor_job(self.db.add_logs_batch, rows)
        except Exception as err:  # pragma: no cover
            _LOGGER.warning("Log Book flush failed (%d rows): %s", len(rows), err)
