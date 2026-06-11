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
        data = await hass.async_add_executor_job(
            lambda: query.get_logs(self.db_path, **kwargs)
        )
        return self.json(data)


class FiltersView(_Base):
    url = "/api/log_book/filters"
    name = "api:log_book:filters"

    async def get(self, request):
        hass = request.app["hass"]
        data = await hass.async_add_executor_job(query.get_filters, self.db_path)
        return self.json(data)


class CountView(_Base):
    url = "/api/log_book/count"
    name = "api:log_book:count"

    async def get(self, request):
        hass = request.app["hass"]
        since = request.query.get("since_id", 0)
        data = await hass.async_add_executor_job(query.get_count, self.db_path, since)
        return self.json(data)


# --- Phase 2 stubs (return safe defaults so the UI degrades gracefully) ---

class ChainView(_Base):
    url = "/api/log_book/chain"
    name = "api:log_book:chain"

    async def get(self, request):
        log_id = request.query.get("id")
        # Phase 1: chain reconstruction (via context.parent_id) follows in Phase 2
        return self.json({
            "target": {"id": log_id, "message": "", "timestamp": "", "metadata": {}},
            "causes": [], "automation": None, "effects": [],
            "note": "Die Prozess-Topologie wird in Phase 2 ergänzt.", "nearby": [],
        })


class TraceView(_Base):
    url = "/api/log_book/trace"
    name = "api:log_book:trace"

    async def get(self, request):
        return self.json({"available": False, "reason": "phase2"})


class PatternsView(_Base):
    url = "/api/log_book/patterns"
    name = "api:log_book:patterns"

    async def get(self, request):
        return self.json({"findings": [], "count": 0})


class EntityStatsView(_Base):
    url = "/api/log_book/entity_stats"
    name = "api:log_book:entity_stats"

    async def get(self, request):
        hass = request.app["hass"]
        entity = request.query.get("entity")
        if not entity:
            return self.json({"error": "no entity"})
        res = await hass.async_add_executor_job(
            lambda: query.get_logs(self.db_path, entity=entity, limit=30)
        )
        return self.json({
            "entity": entity, "device": entity, "total": len(res["logs"]),
            "first": None, "last": None, "by_type": [], "top_users": [], "top_causes": [],
            "_recent": res["logs"],
        })


ALL_VIEWS = [LogsView, FiltersView, CountView, ChainView, TraceView, PatternsView, EntityStatsView]
