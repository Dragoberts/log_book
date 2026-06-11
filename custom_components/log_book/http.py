"""HTTP endpoint for Log Book frontend."""
import json
from aiohttp import web
from homeassistant.core import HomeAssistant

from . import DOMAIN


async def setup_http_endpoints(hass: HomeAssistant) -> None:
    """Set up HTTP endpoints for Log Book."""

    async def handle_get_logs(request: web.Request) -> web.Response:
        """Handle GET /api/log_book/logs request."""
        data = hass.data[DOMAIN]
        service = data["service"]

        params = request.query
        limit = min(int(params.get("limit", 100)), 1000)
        offset = int(params.get("offset", 0))
        event_type = params.get("event_type")
        user = params.get("user")
        device = params.get("device")
        automation = params.get("automation")

        logs = service.get_logs(
            limit=limit,
            offset=offset,
            event_type=event_type,
            user=user,
            device=device,
            automation=automation,
        )

        return web.json_response({"logs": logs, "limit": limit, "offset": offset})

    async def handle_get_filters(request: web.Request) -> web.Response:
        """Handle GET /api/log_book/filters request."""
        data = hass.data[DOMAIN]
        service = data["service"]

        return web.json_response(
            {
                "users": service.get_distinct_users(),
                "devices": service.get_distinct_devices(),
                "entities": service.get_distinct_entities(),
                "automations": service.get_distinct_automations(),
                "event_types": service.get_distinct_event_types(),
            }
        )

    hass.http.register_view(LogsView(hass))
    hass.http.register_view(FiltersView(hass))


class LogsView(web.View):
    """View for getting logs."""

    def __init__(self, hass: HomeAssistant):
        """Initialize view."""
        self.hass = hass

    async def get(self) -> web.Response:
        """GET request handler."""
        data = self.hass.data[DOMAIN]
        service = data["service"]

        params = self.request.query
        limit = min(int(params.get("limit", 100)), 1000)
        offset = int(params.get("offset", 0))
        event_type = params.get("event_type")
        user = params.get("user")
        device = params.get("device")
        entity = params.get("entity")
        automation = params.get("automation")

        logs = service.get_logs(
            limit=limit,
            offset=offset,
            event_type=event_type,
            user=user,
            device=device,
            entity=entity,
            automation=automation,
        )

        return web.json_response({"logs": logs, "limit": limit, "offset": offset})

    @property
    def url(self) -> str:
        return "/api/log_book/logs"


class FiltersView(web.View):
    """View for getting available filters."""

    def __init__(self, hass: HomeAssistant):
        """Initialize view."""
        self.hass = hass

    async def get(self) -> web.Response:
        """GET request handler."""
        data = self.hass.data[DOMAIN]
        service = data["service"]

        return web.json_response(
            {
                "users": service.get_distinct_users(),
                "devices": service.get_distinct_devices(),
                "entities": service.get_distinct_entities(),
                "automations": service.get_distinct_automations(),
                "event_types": service.get_distinct_event_types(),
            }
        )

    @property
    def url(self) -> str:
        return "/api/log_book/filters"
