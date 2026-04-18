"""Zemote fan platform — F1/F2 channels with 3-step speed control (1, 3, 5)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_STATE_UPDATED

_LOGGER = logging.getLogger(__name__)

SPEED_STEPS = [1, 3, 5]
NUM_SPEEDS  = len(SPEED_STEPS)


def _pct_to_speed(percentage: int) -> int:
    if percentage <= 0:
        return 0
    idx = min(NUM_SPEEDS - 1, int((percentage - 1) * NUM_SPEEDS / 100))
    return SPEED_STEPS[idx]


def _speed_to_pct(speed: int) -> int:
    if speed <= 0:
        return 0
    nearest = min(SPEED_STEPS, key=lambda s: abs(s - speed))
    idx = SPEED_STEPS.index(nearest)
    return round((idx + 1) * 100 / NUM_SPEEDS)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    fans = [
        ZemoteFan(hub, d)
        for d in hub.devices
        if d["platform"] == "fan"
    ]
    async_add_entities(fans, True)


class ZemoteFan(FanEntity):
    """Represents a Zemote fan — 3 speeds (low/medium/high) via odd shadow values."""

    _enable_turn_on_off_backwards_compat = False
    _attr_has_entity_name = False

    def __init__(self, hub: Any, device: dict) -> None:
        self._hub     = hub
        self._device  = device
        self._serial  = device["serialNumber"]
        self._channel = device["channelKey"]

        self._attr_unique_id = f"zemote_{device['applianceId']}"
        self._attr_supported_features = (
            FanEntityFeature.SET_SPEED
            | FanEntityFeature.TURN_ON
            | FanEntityFeature.TURN_OFF
        )
        self._attr_speed_count = NUM_SPEEDS
        self._attr_name = device["name"]

        room = device.get("roomName") or ""
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=device.get("hubName") or self._serial,
            manufacturer="Zemote",
            model="Hub Module",
            suggested_area=room or None,
        )

    @property
    def is_on(self) -> bool:
        val = self._hub.get_channel_state(self._serial, self._channel)
        try:
            return int(val) > 0
        except (TypeError, ValueError):
            return False

    @property
    def percentage(self) -> int | None:
        val = self._hub.get_channel_state(self._serial, self._channel)
        try:
            return _speed_to_pct(int(val))
        except (TypeError, ValueError):
            return None

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        speed = _pct_to_speed(percentage) if percentage is not None else 3
        if speed == 0:
            speed = 1
        self._hub.set_channel(self._serial, self._channel, speed)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._hub.set_channel(self._serial, self._channel, 0)

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            await self.async_turn_off()
            return
        self._hub.set_channel(self._serial, self._channel, _pct_to_speed(percentage))

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_STATE_UPDATED}_{self._serial}",
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self, reported: dict) -> None:
        if self._channel in reported:
            self.async_write_ha_state()
