"""Zemote sensor platform — battery sensors for smart locks."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
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
    sensors = [
        ZemoteLockBattery(hub, d)
        for d in hub.devices
        if d.get("platform") == "lock"
    ]
    async_add_entities(sensors, True)


class ZemoteLockBattery(SensorEntity):
    """Battery percentage sensor for a Zemote smart lock."""

    _attr_has_entity_name   = False
    _attr_device_class      = SensorDeviceClass.BATTERY
    _attr_state_class       = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, hub: Any, device: dict) -> None:
        self._hub    = hub
        self._serial = device["serialNumber"]

        self._attr_unique_id = f"zemote_{device['applianceId']}_battery"
        self._attr_name      = f"{device['name']} Battery"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
        )

        self._battery_pct: int | None = None

    @property
    def native_value(self) -> int | None:
        return self._battery_pct

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
        if "BAT" not in reported:
            return
        try:
            b = int(reported["BAT"])
            if b == 205:
                self._battery_pct = None
            elif b <= 100:
                self._battery_pct = b
            elif b <= 201:
                self._battery_pct = b - 100
            else:
                self._battery_pct = None
        except (ValueError, TypeError):
            self._battery_pct = None
        self.async_write_ha_state()
