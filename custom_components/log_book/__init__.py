"""Log Book integration for Home Assistant."""
import logging
from pathlib import Path

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType

from .database import LogDatabase
from .logger import LogService

_LOGGER = logging.getLogger(__name__)

DOMAIN = "log_book"
SERVICE_LOG_EVENT = "log_event"
SERVICE_GET_LOGS = "get_logs"

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema({})},
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Log Book integration."""

    # Initialize database
    db_path = Path(hass.config.path("log_book.db"))
    database = LogDatabase(str(db_path))
    await hass.async_add_executor_job(database.init_db)

    # Store database in hass.data
    hass.data[DOMAIN] = {
        "database": database,
        "service": LogService(database),
    }

    # Register services
    async def handle_log_event(call: ServiceCall) -> None:
        """Handle log event service call."""
        service = hass.data[DOMAIN]["service"]
        await hass.async_add_executor_job(
            service.log_event,
            call.data.get("event_type"),
            call.data.get("message"),
            call.data.get("user"),
            call.data.get("device"),
            call.data.get("entity"),
            call.data.get("automation"),
            call.data.get("metadata", {}),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_LOG_EVENT,
        handle_log_event,
        schema=vol.Schema(
            {
                vol.Optional("user"): cv.string,
                vol.Optional("device"): cv.string,
                vol.Optional("entity"): cv.string,
                vol.Optional("automation"): cv.string,
                vol.Required("event_type"): cv.string,
                vol.Required("message"): cv.string,
                vol.Optional("metadata"): dict,
            }
        ),
    )

    _LOGGER.info("Log Book integration set up successfully")
    return True
