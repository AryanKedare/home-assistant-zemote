"""Cover platform for Zemote integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZemoteHub
from .const import DOMAIN, SIGNAL_STATE_UPDATED

CMD_OPEN = 2; CMD_CLOSE = 1; CMD_STOP = 3


async def async_setup_entry(hass, entry, async_add_entities):
    hub: ZemoteHub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ZemoteCover(hub, d) for d in hub.devices if d.get("platform") == "cover"])


class ZemoteCover(CoverEntity):
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP

    def __init__(self, hub, device):
        self._hub = hub; self._device = device
        self._serial = device["serialNumber"]; self._channel = device["channelKey"]
        self._attr_name = device["name"]; self._attr_unique_id = device["applianceId"]
        self._last_cmd = 0

    @property
    def device_info(self):
        return DeviceInfo(identifiers={(DOMAIN, self._serial)}, name=self._device.get("hubName", self._serial),
                          manufacturer="Contera IoT", model="Zemote Hub",
                          suggested_area=self._device.get("roomName") or None)

    @property
    def is_closed(self):
        if self._last_cmd == CMD_CLOSE: return True
        if self._last_cmd == CMD_OPEN: return False
        return None

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(
            self.hass, f"{SIGNAL_STATE_UPDATED}_{self._serial}", self._handle_state_update))
        val = self._hub.get_channel_state(self._serial, self._channel)
        if val is not None:
            self._last_cmd = int(val)
            self.async_write_ha_state()

    @callback
    def _handle_state_update(self, reported):
        raw = reported.get(self._channel)
        if raw is not None:
            self._last_cmd = int(raw)
            self.async_write_ha_state()

    def open_cover(self, **kwargs): self._last_cmd = CMD_OPEN; self._hub.set_channel(self._serial, self._channel, CMD_OPEN); self.schedule_update_ha_state()
    def close_cover(self, **kwargs): self._last_cmd = CMD_CLOSE; self._hub.set_channel(self._serial, self._channel, CMD_CLOSE); self.schedule_update_ha_state()
    def stop_cover(self, **kwargs): self._last_cmd = CMD_STOP; self._hub.set_channel(self._serial, self._channel, CMD_STOP); self.schedule_update_ha_state()
