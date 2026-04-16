"""Fan platform for Zemote integration."""
from __future__ import annotations

import math
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import percentage_to_ranged_value, ranged_value_to_percentage

from . import ZemoteHub
from .const import DOMAIN, SIGNAL_STATE_UPDATED

SPEED_RANGE = (1, 5)
SPEED_COUNT = 3
SPEED_MAP   = {1: 1, 2: 3, 3: 5}
INVERSE_MAP = {v: k for k, v in SPEED_MAP.items()}


async def async_setup_entry(hass, entry, async_add_entities):
    hub: ZemoteHub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ZemoteFan(hub, d) for d in hub.devices if d.get("platform") == "fan"])


class ZemoteFan(FanEntity):
    _attr_supported_features = FanEntityFeature.SET_SPEED
    _attr_speed_count        = SPEED_COUNT

    def __init__(self, hub, device):
        self._hub = hub; self._device = device
        self._serial = device["serialNumber"]; self._channel = device["channelKey"]
        self._attr_name = device["name"]; self._attr_unique_id = device["applianceId"]
        self._shadow_value = 0

    @property
    def device_info(self):
        return DeviceInfo(identifiers={(DOMAIN, self._serial)}, name=self._device.get("hubName", self._serial),
                          manufacturer="Contera IoT", model="Zemote Hub",
                          suggested_area=self._device.get("roomName") or None)

    @property
    def is_on(self): return self._shadow_value != 0

    @property
    def percentage(self):
        if self._shadow_value == 0: return 0
        return ranged_value_to_percentage(SPEED_RANGE, INVERSE_MAP.get(self._shadow_value, 1))

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(
            self.hass, f"{SIGNAL_STATE_UPDATED}_{self._serial}", self._handle_state_update))
        val = self._hub.get_channel_state(self._serial, self._channel)
        if val is not None:
            self._shadow_value = int(val)
            self.async_write_ha_state()

    @callback
    def _handle_state_update(self, reported):
        raw = reported.get(self._channel)
        if raw is not None:
            self._shadow_value = int(raw)
            self.async_write_ha_state()

    def turn_on(self, percentage=None, **kwargs):
        step = max(1, min(SPEED_COUNT, math.ceil(percentage_to_ranged_value(SPEED_RANGE, percentage)))) if percentage else 1
        shadow = SPEED_MAP[step]
        self._shadow_value = shadow
        self._hub.set_channel(self._serial, self._channel, shadow)
        self.schedule_update_ha_state()

    def turn_off(self, **kwargs):
        self._shadow_value = 0
        self._hub.set_channel(self._serial, self._channel, 0)
        self.schedule_update_ha_state()

    def set_percentage(self, percentage):
        self.turn_off() if percentage == 0 else self.turn_on(percentage=percentage)
