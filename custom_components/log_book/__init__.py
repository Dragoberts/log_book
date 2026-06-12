"""Log Book - native Home Assistant integration (token-free).

Provides a sidebar panel that visualises HA events and (Phase 2) the process
topology. All data is read internally from Home Assistant - no access token,
no separate server.
"""
import logging
from pathlib import Path

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType

from .database import LogDatabase
from .collector import LogCollector
from .views import ALL_VIEWS

_LOGGER = logging.getLogger(__name__)

DOMAIN = "log_book"
SERVICE_LOG_EVENT = "log_event"

PANEL_URL_PATH = "log-book"            # sidebar route -> /log-book
FRONTEND_URL = "/log_book_frontend"    # static asset mount

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)


async def _register_static(hass: HomeAssistant, url_path: str, dir_path: str) -> None:
    """Register a static path, compatible with old and new HA cores."""
    try:
        from homeassistant.components.http import StaticPathConfig
        await hass.http.async_register_static_paths(
            [StaticPathConfig(url_path, dir_path, False)]
        )
        return
    except (ImportError, AttributeError):
        pass
    try:
        # Deprecated on newer cores but works on older ones
        hass.http.register_static_path(url_path, dir_path, False)
    except Exception as err:  # pragma: no cover
        _LOGGER.warning("Log Book: static path registration failed: %s", err)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Log Book integration."""
    integration_dir = Path(__file__).parent
    www_dir = integration_dir / "www"
    db_path = hass.config.path("log_book.db")

    # 1) Database
    database = LogDatabase(db_path)
    await hass.async_add_executor_job(database.init_db)

    hass.data[DOMAIN] = {"database": database, "db_path": db_path}

    # 2) Serve the frontend assets
    await _register_static(hass, FRONTEND_URL, str(www_dir))

    # 3) Register the sidebar panel (iframe pointing at our static app)
    try:
        from homeassistant.components import frontend
        frontend.async_register_built_in_panel(
            hass,
            "iframe",
            sidebar_title="Log Book",
            sidebar_icon="mdi:notebook-outline",
            frontend_url_path=PANEL_URL_PATH,
            config={"url": f"{FRONTEND_URL}/index.html"},
            require_admin=False,
        )
    except ValueError:
        # Panel already registered (e.g. on reload) - ignore
        pass
    except Exception as err:  # pragma: no cover
        _LOGGER.warning("Log Book: panel registration failed: %s", err)

    # 4) HTTP API views
    for view_cls in ALL_VIEWS:
        hass.http.register_view(view_cls(db_path))

    # 5) Internal event collector (token-free)
    collector = LogCollector(hass, database)
    collector.async_start()
    hass.data[DOMAIN]["collector"] = collector

    # 5b) One-time historical backfill from the recorder (after HA has started,
    #     so the recorder is ready). Populates users + topology for past events.
    async def _do_backfill(_event=None):
        try:
            from .backfill import async_backfill
            await async_backfill(hass, database, collector, days=3)
        except Exception as err:  # pragma: no cover
            _LOGGER.warning("Log Book: backfill error: %s", err)

    try:
        from homeassistant.helpers.start import async_at_started
        async_at_started(hass, _do_backfill)
    except Exception:
        hass.async_create_task(_do_backfill())

    # 6) Manual logging service (optional)
    async def handle_log_event(call: ServiceCall) -> None:
        await hass.async_add_executor_job(
            database.add_log,
            call.data.get("event_type"),
            call.data.get("message"),
            call.data.get("user"),
            call.data.get("device"),
            call.data.get("entity"),
            call.data.get("automation"),
            call.data.get("metadata", {}),
        )

    hass.services.async_register(
        DOMAIN, SERVICE_LOG_EVENT, handle_log_event,
        schema=vol.Schema({
            vol.Required("event_type"): cv.string,
            vol.Required("message"): cv.string,
            vol.Optional("user"): cv.string,
            vol.Optional("device"): cv.string,
            vol.Optional("entity"): cv.string,
            vol.Optional("automation"): cv.string,
            vol.Optional("metadata"): dict,
        }),
    )

    _LOGGER.info("Log Book set up - panel at /%s (token-free)", PANEL_URL_PATH)
    return True
