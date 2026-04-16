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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub: ZemoteHub = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for d in hub.devices:
        if d.get("platform") != "switch":
            continue
        if d.get("surBrand") or d.get("surCodeset"):
            entities.append(ZemoteSur(hub, d))
        else:
            entities.append(ZemoteSwitch(hub, d))
    async_add_entities(entities)


class ZemoteSwitch(SwitchEntity):
    """Zemote switch."""

    def __init__(self, hub: ZemoteHub, device: dict) -> None:
        self._hub        = hub
        self._device     = device
        self._serial     = device["serialNumber"]
        self._channel    = device["channelKey"]
        self._attr_name  = device["name"]
        self._attr_unique_id = device["applianceId"]
        self._state      = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=self._device.get("hubName", self._serial),
            manufacturer="Contera IoT",
            model="Zemote Hub",
        )

    @property
    def is_on(self) -> bool:
        return self._state

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
            self._state = int(val) != 0

    @callback
    def _handle_state_update(self, reported: dict) -> None:
        raw = reported.get(self._channel)
        if raw is not None:
            self._state = int(raw) != 0
            self.async_write_ha_state()

    def turn_on(self, **kwargs: Any) -> None:
        self._state = True
        self._hub.set_channel(self._serial, self._channel, 1)
        self.schedule_update_ha_state()

    def turn_off(self, **kwargs: Any) -> None:
        self._state = False
        self._hub.set_channel(self._serial, self._channel, 0)
        self.schedule_update_ha_state()


class ZemoteSur(SwitchEntity):
    """Zemote IR blaster (SUR) device."""

    def __init__(self, hub: ZemoteHub, device: dict) -> None:
        self._hub        = hub
        self._device     = device
        self._serial     = device["serialNumber"]
        self._channel    = device["channelKey"]
        self._attr_name  = device["name"]
        self._attr_unique_id = device["applianceId"]
        self._state      = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=self._device.get("hubName", self._serial),
            manufacturer="Contera IoT",
            model="Zemote Hub",
        )

    @property
    def is_on(self) -> bool:
        return self._state

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_STATE_UPDATED}_{self._serial}",
                self._handle_state_update,
            )
        )

    @callback
    def _handle_state_update(self, reported: dict) -> None:
        raw = reported.get(self._channel)
        if raw is not None:
            self._state = int(raw) != 0
            self.async_write_ha_state()

    def turn_on(self, **kwargs: Any) -> None:
        self._state = True
        self._hub.publish(self._serial, {
            self._channel: 1,
            "brand":   self._device.get("surBrand", ""),
            "codeset": self._device.get("surCodeset", ""),
        })
        self.schedule_update_ha_state()

    def turn_off(self, **kwargs: Any) -> None:
        self._state = False
        self._hub.set_channel(self._serial, self._channel, 0)
        self.schedule_update_ha_state()
