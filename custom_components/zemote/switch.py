"""Zemote switch platform — non-dimmable lights, power modules, and IR (SUR) devices."""
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
    """Represents a Zemote switch channel (wired module or IR/SUR remote)."""

    _attr_has_entity_name = True

    def __init__(self, hub: Any, device: dict) -> None:
        self._hub     = hub
        self._device  = device
        self._serial  = device["serialNumber"]
        self._channel = device["channelKey"]

        # SUR / IR device detection — present and non-empty brand/codeset
        self._sur_type    = device.get("channelKey", "")  # e.g. "AC", "TV", "DTH"
        self._sur_brand   = device.get("surBrand", "") or ""
        self._sur_codeset = device.get("surCodeset", "") or ""
        self._is_sur      = bool(self._sur_brand and self._sur_codeset)

        # Optimistic state tracking for IR devices (no shadow feedback)
        self._optimistic_on: bool = False

        self._attr_unique_id = f"zemote_{device['applianceId']}"
        self._attr_name = None

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device["applianceId"])},
            name=device["name"],
            manufacturer="Zemote",
            model=device.get("serialNumber"),
            serial_number=device.get("serialNumber"),
            suggested_area=device.get("roomName") or None,
            via_device=(DOMAIN, self._serial),
        )

    @property
    def is_on(self) -> bool:
        if self._is_sur:
            # IR devices are one-directional — no shadow feedback.
            # Track state optimistically based on last command sent.
            return self._optimistic_on
        val = self._hub.get_channel_state(self._serial, self._channel)
        try:
            return int(val) > 0
        except (TypeError, ValueError):
            return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._is_sur:
            ok = await self._hub.async_send_ir_command(
                self._serial,
                self._sur_type,
                self._sur_brand,
                self._sur_codeset,
                want_on=True,
            )
            if ok:
                self._optimistic_on = True
                self.async_write_ha_state()
        else:
            self._hub.set_channel(self._serial, self._channel, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._is_sur:
            ok = await self._hub.async_send_ir_command(
                self._serial,
                self._sur_type,
                self._sur_brand,
                self._sur_codeset,
                want_on=False,
            )
            if ok:
                self._optimistic_on = False
                self.async_write_ha_state()
        else:
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
        # Wired channels report state back via shadow; IR devices do not
        if not self._is_sur and self._channel in reported:
            self.async_write_ha_state()
