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
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from . import ZemoteHub
from .const import DOMAIN, SIGNAL_STATE_UPDATED

SPEED_RANGE = (1, 5)
SPEED_COUNT = 3
SPEED_MAP   = {1: 1, 2: 3, 3: 5}
INVERSE_MAP = {v: k for k, v in SPEED_MAP.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub: ZemoteHub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ZemoteFan(hub, d) for d in hub.devices if d.get("platform") == "fan"])


class ZemoteFan(FanEntity):
    """Zemote 3-speed fan."""

    _attr_supported_features = FanEntityFeature.SET_SPEED
    _attr_speed_count        = SPEED_COUNT

    def __init__(self, hub: ZemoteHub, device: dict) -> None:
        self._hub          = hub
        self._device       = device
        self._serial       = device["serialNumber"]
        self._channel      = device["channelKey"]
        self._attr_name    = device["name"]
        self._attr_unique_id = device["applianceId"]
        self._shadow_value = 0

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=self._device.get("hubName", self._serial),
            manufacturer="Contera IoT",
            model="Zemote Hub",
            suggested_area=self._device.get("roomName") or None,
        )

    @property
    def is_on(self) -> bool:
        return self._shadow_value != 0

    @property
    def percentage(self) -> int | None:
        if self._shadow_value == 0:
            return 0
        step = INVERSE_MAP.get(self._shadow_value, 1)
        return ranged_value_to_percentage(SPEED_RANGE, step)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_STATE_UPDATED}_{self._serial}",
                self._handle_state_update,
            )
        )
        val = self._hub.get_channel_state(self._serial, self._channel)
        if val is not None:
            self._shadow_value = int(val)

    @callback
    def _handle_state_update(self, reported: dict) -> None:
        raw = reported.get(self._channel)
        if raw is not None:
            self._shadow_value = int(raw)
            self.async_write_ha_state()

    def turn_on(self, percentage: int | None = None, **kwargs: Any) -> None:
        if percentage is not None and percentage > 0:
            step = max(1, min(SPEED_COUNT, math.ceil(percentage_to_ranged_value(SPEED_RANGE, percentage))))
        else:
            step = 1
        shadow = SPEED_MAP[step]
        self._shadow_value = shadow
        self._hub.set_channel(self._serial, self._channel, shadow)
        self.schedule_update_ha_state()

    def turn_off(self, **kwargs: Any) -> None:
        self._shadow_value = 0
        self._hub.set_channel(self._serial, self._channel, 0)
        self.schedule_update_ha_state()

    def set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            self.turn_off()
        else:
            self.turn_on(percentage=percentage)
