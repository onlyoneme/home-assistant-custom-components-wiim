"""
Support for WiiM Mini devices.

For more details about this platform, please refer to the documentation at
https://github.com/onlyoneme/home-assistant-custom-components-wiim
"""
import logging
import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.helpers import config_validation as cv

DOMAIN = 'wiim_custom'

SERVICE_CMD = 'command'
SERVICE_PLAY_URL = 'play_url'


ATTR_CMD = 'command'
ATTR_NOTIF = 'notify'
ATTR_URL = 'url'


CMND_SERVICE_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): cv.comp_entity_ids,
    vol.Required(ATTR_CMD): cv.string,
    vol.Optional(ATTR_NOTIF, default=True): cv.boolean
})

PLAY_URL_SERVICE_SCHEMA = vol.Schema({
    vol.Required(ATTR_ENTITY_ID): cv.entity_id,
    vol.Required(ATTR_URL): cv.string
})

_LOGGER = logging.getLogger(__name__)

def setup(hass, config):
    """Handle service configuration."""

    async def async_service_handle(service):
        """Handle services."""
        _LOGGER.debug("DOMAIN: %s, entities: %s", DOMAIN, str(hass.data[DOMAIN].entities))
        _LOGGER.debug("Service_handle from id: %s", service.data.get(ATTR_ENTITY_ID))
        entity_ids = service.data.get(ATTR_ENTITY_ID)
        entities = hass.data[DOMAIN].entities

        if entity_ids:
            if entity_ids == 'all':
                entity_ids = [e.entity_id for e in entities]
            entities = [e for e in entities if e.entity_id in entity_ids]

        if service.service == SERVICE_CMD:
            command = service.data.get(ATTR_CMD)
            notify = service.data.get(ATTR_NOTIF)
            for device in entities:
                if device.entity_id in entity_ids:
                    _LOGGER.debug("**COMMAND** entity: %s; command: %s", device.entity_id, command)
                    await device.async_execute_command(command, notify)
        elif service.service == SERVICE_PLAY_URL:
            url = service.data.get(ATTR_URL)
            for device in entities:
                if device.entity_id in entity_ids:
                    _LOGGER.debug("**PLAY URL** entity: %s; url: %s", device.entity_id, url)
                    await device.call_wiim_httpapi("setPlayerCmd:play:{0}".format(url), None)


    hass.services.async_register(
        DOMAIN, SERVICE_CMD, async_service_handle, schema=CMND_SERVICE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_PLAY_URL, async_service_handle, schema=PLAY_URL_SERVICE_SCHEMA)

    return True
