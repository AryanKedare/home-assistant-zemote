"""Zemote fan platform — F1/F2 channels with 3-step speed control (1, 3, 5)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import int_states_in_range

from .const import DOMAIN, SIGNAL_STATE_UPDATED

_LOGGER = logging.getLogger(__name__)

# Zemote firmware only responds to odd speed values: 0=off, 1=low, 3=medium, 5=high
# Even values (2, 4) are treated as off by the device firmware.
# We expose 3 discrete steps mapped to shadow values 1, 3, 5.
SPEED_STEPS = [1, 3, 5]   # actual shadow values sent to device
NUM_SPEEDS   = len(SPEED_STEPS)  # = 3


def _pct_to_speed(percentage: int) -> int:
    """Map 1-100% to the nearest odd speed step (1, 3, 5)."""
    if percentage <= 0:
        return 0
    # Divide range into 3 equal buckets
    idx = min(NUM_SPEEDS - 1, int((percentage - 1) * NUM_SPEEDS / 100))
    return SPEED_STEPS[idx]


def _speed_to_pct(speed: int) -> int:
    """Map shadow speed value back to percentage for HA display."""
    if speed <= 0:
        return 0
    # Find nearest step
    nearest = min(SPEED_STEPS, key=lambda s: abs(s - speed))
    idx = SPEED_STEPS.index(nearest)
    # Return midpoint of the bucket
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

    def __init__(self, hub: Any, device: dict) -> None:
        self._hub     = hub
        self._device  = device
        self._serial  = device["serialNumber"]
        self._channel = device["channelKey"]

        self._attr_unique_id = f"zemote_{device['applianceId']}"
        self._attr_name      = device["name"]
        self._attr_supported_features = (
            FanEntityFeature.SET_SPEED
            | FanEntityFeature.TURN_ON
            | FanEntityFeature.TURN_OFF
        )
        self._attr_speed_count = NUM_SPEEDS  # 3

        room = device.get("roomName")
        if room:
            self._attr_suggested_area = room

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
        # Assign area via device registry — works even for existing entities
        room = self._device.get("roomName")
        if room:
            await self._async_assign_area(room)

    async def _async_assign_area(self, room_name: str) -> None:
        """Look up or create the area and assign this device to it."""
        from homeassistant.helpers import area_registry as ar, device_registry as dr
        area_reg  = ar.async_get(self.hass)
        device_reg = dr.async_get(self.hass)

        area = area_reg.async_get_area_by_name(room_name)
        if area is None:
            area = area_reg.async_create(room_name)

        device = device_reg.async_get_device(identifiers={(DOMAIN, self._attr_unique_id)})
        if device and device.area_id != area.id:
            device_reg.async_update_device(device.id, area_id=area.id)

    @callback
    def _handle_update(self, reported: dict) -> None:
        if self._channel in reported:
            self.async_write_ha_state()
