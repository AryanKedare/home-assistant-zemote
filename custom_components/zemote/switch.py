"""Switch platform for Zemote integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZemoteHub
from .const import DOMAIN, SIGNAL_STATE_UPDATED


async def async_setup_entry(hass, entry, async_add_entities):
    hub: ZemoteHub = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for d in hub.devices:
        if d.get("platform") != "switch": continue
        entities.append(ZemoteSur(hub, d) if (d.get("surBrand") or d.get("surCodeset")) else ZemoteSwitch(hub, d))
    async_add_entities(entities)


def _device_info(device, serial):
    return DeviceInfo(identifiers={(DOMAIN, serial)}, name=device.get("hubName", serial),
                      manufacturer="Contera IoT", model="Zemote Hub",
                      suggested_area=device.get("roomName") or None)


class ZemoteSwitch(SwitchEntity):
    def __init__(self, hub, device):
        self._hub = hub; self._device = device
        self._serial = device["serialNumber"]; self._channel = device["channelKey"]
        self._attr_name = device["name"]; self._attr_unique_id = device["applianceId"]
        self._state = False

    @property
    def device_info(self): return _device_info(self._device, self._serial)
    @property
    def is_on(self): return self._state

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(
            self.hass, f"{SIGNAL_STATE_UPDATED}_{self._serial}", self._handle_state_update))
        val = self._hub.get_channel_state(self._serial, self._channel)
        if val is not None:
            self._state = int(val) != 0
            self.async_write_ha_state()

    @callback
    def _handle_state_update(self, reported):
        raw = reported.get(self._channel)
        if raw is not None:
            self._state = int(raw) != 0
            self.async_write_ha_state()

    def turn_on(self, **kwargs): self._state = True; self._hub.set_channel(self._serial, self._channel, 1); self.schedule_update_ha_state()
    def turn_off(self, **kwargs): self._state = False; self._hub.set_channel(self._serial, self._channel, 0); self.schedule_update_ha_state()


class ZemoteSur(SwitchEntity):
    def __init__(self, hub, device):
        self._hub = hub; self._device = device
        self._serial = device["serialNumber"]; self._channel = device["channelKey"]
        self._attr_name = device["name"]; self._attr_unique_id = device["applianceId"]
        self._state = False

    @property
    def device_info(self): return _device_info(self._device, self._serial)
    @property
    def is_on(self): return self._state

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(
            self.hass, f"{SIGNAL_STATE_UPDATED}_{self._serial}", self._handle_state_update))

    @callback
    def _handle_state_update(self, reported):
        raw = reported.get(self._channel)
        if raw is not None:
            self._state = int(raw) != 0
            self.async_write_ha_state()

    def turn_on(self, **kwargs):
        self._state = True
        self._hub.publish(self._serial, {self._channel: 1, "brand": self._device.get("surBrand", ""), "codeset": self._device.get("surCodeset", "")})
        self.schedule_update_ha_state()

    def turn_off(self, **kwargs): self._state = False; self._hub.set_channel(self._serial, self._channel, 0); self.schedule_update_ha_state()
