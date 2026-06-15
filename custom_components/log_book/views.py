"""HTTP API views for the Log Book panel (served inside Home Assistant, no token)."""
import logging

from homeassistant.components.http import HomeAssistantView

from . import query

_LOGGER = logging.getLogger(__name__)


class _Base(HomeAssistantView):
    requires_auth = False  # local log data, same-origin panel

    def __init__(self, db_path):
        self.db_path = db_path


class LogsView(_Base):
    url = "/api/log_book/logs"
    name = "api:log_book:logs"

    async def get(self, request):
        hass = request.app["hass"]
        qs = request.query
        kwargs = dict(
            limit=qs.get("limit", 50),
            offset=qs.get("offset", 0),
            user=qs.get("user"),
            device=qs.get("device"),
            entity=qs.get("entity"),
            automation=qs.get("automation"),
            event_type=qs.get("event_type"),
            exclude_event_type=qs.get("exclude_event_type"),
            date_from=qs.get("date_from"),
            date_to=qs.get("date_to"),
            q=qs.get("q"),
            chain_only=qs.get("chain_only"),
            hidden=qs.getall("hidden", []),
        )
        try:
            data = await hass.async_add_executor_job(
                lambda: query.get_logs(self.db_path, **kwargs)
            )
        except Exception as err:
            _LOGGER.error("LogsView error: %s", err)
            data = {"logs": [], "limit": kwargs["limit"], "offset": kwargs["offset"]}
        return self.json(data)


class FiltersView(_Base):
    url = "/api/log_book/filters"
    name = "api:log_book:filters"

    async def get(self, request):
        hass = request.app["hass"]
        try:
            data = await hass.async_add_executor_job(query.get_filters, self.db_path)
        except Exception as err:
            _LOGGER.error("FiltersView error: %s", err)
            data = {"users": [], "devices": [], "entities": [], "automations": [], "event_types": []}
        return self.json(data)


class CountView(_Base):
    url = "/api/log_book/count"
    name = "api:log_book:count"

    async def get(self, request):
        hass = request.app["hass"]
        since = request.query.get("since_id", 0)
        try:
            data = await hass.async_add_executor_job(query.get_count, self.db_path, since)
        except Exception as err:
            _LOGGER.error("CountView error: %s", err)
            data = {"new": 0, "max_id": 0}
        return self.json(data)


class ChainView(_Base):
    url = "/api/log_book/chain"
    name = "api:log_book:chain"

    async def get(self, request):
        hass = request.app["hass"]
        log_id = request.query.get("id")
        try:
            data = await hass.async_add_executor_job(query.get_chain, self.db_path, log_id)
        except Exception as err:
            _LOGGER.error("ChainView error: %s", err)
            data = {"error": str(err)}
        return self.json(data)


class EntityStatsView(_Base):
    url = "/api/log_book/entity_stats"
    name = "api:log_book:entity_stats"

    async def get(self, request):
        hass = request.app["hass"]
        entity = request.query.get("entity")
        if not entity:
            return self.json({"error": "no entity"})
        try:
            data = await hass.async_add_executor_job(query.get_entity_stats, self.db_path, entity)
        except Exception as err:
            _LOGGER.error("EntityStatsView error: %s", err)
            data = {"entity": entity, "total": 0, "by_type": [], "top_users": [], "top_causes": []}
        return self.json(data)


class PatternsView(_Base):
    url = "/api/log_book/patterns"
    name = "api:log_book:patterns"

    async def get(self, request):
        hass = request.app["hass"]
        entity = request.query.get("entity")
        if not entity:
            return self.json({"findings": [], "count": 0})
        try:
            data = await hass.async_add_executor_job(query.get_patterns, self.db_path, entity)
        except Exception as err:
            _LOGGER.error("PatternsView error: %s", err)
            data = {"findings": [], "count": 0}
        return self.json(data)


class TraceView(_Base):
    """Automation traces require deeper HA-internal access - deferred. Degrades gracefully."""
    url = "/api/log_book/trace"
    name = "api:log_book:trace"

    async def get(self, request):
        return self.json({"available": False, "reason": "not yet implemented in native mode"})


class AutomationView(_Base):
    """Automation config view - deferred. Returns a 'not available' shape so the UI degrades."""
    url = "/api/log_book/automation"
    name = "api:log_book:automation"

    async def get(self, request):
        entity = request.query.get("entity")
        return self.json({
            "entity": entity, "friendly_name": entity, "config_available": False,
            "config_error": "Konfiguration im nativen Modus noch nicht verfügbar",
        })


ALL_VIEWS = [LogsView, FiltersView, CountView, ChainView, EntityStatsView,
             PatternsView, TraceView, AutomationView]
