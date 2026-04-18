"""Zemote switch platform — non-dimmable lights and power modules."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_STATE_UPDATED

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    switches = [
        ZemoteSwitch(hub, d)
        for d in hub.devices
        if d["platform"] == "switch"
    ]
    async_add_entities(switches, True)


class ZemoteSwitch(SwitchEntity):
    """Represents a Zemote switch channel."""

    _attr_has_entity_name = False

    def __init__(self, hub: Any, device: dict) -> None:
        self._hub     = hub
        self._device  = device
        self._serial  = device["serialNumber"]
        self._channel = device["channelKey"]

        self._attr_unique_id = f"zemote_{device['applianceId']}"
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

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._hub.set_channel(self._serial, self._channel, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._hub.set_channel(self._serial, self._channel, 0)

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
