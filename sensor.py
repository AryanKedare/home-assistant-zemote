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


def _parse_bat(raw: Any) -> tuple[int | None, str | None]:
    """
    Returns (pct_int_or_None, label_string_or_None).

    Device can send:
      "ok"        → battery is fine but no numeric level → (None, "ok")
      0–100       → direct percentage
      101–201     → percentage - 100 (unregistered device offset)
      205         → offline
    """
    try:
        b = int(raw)
    except (ValueError, TypeError):
        # Non-numeric strings like "ok" — store as label, no numeric value
        return None, str(raw)

    if b == 205:
        return None, "offline"
    if b <= 100:
        return b, f"{b}%"
    if b <= 201:
        pct = b - 100
        return pct, f"{pct}% (unregistered)"
    return None, str(b)


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
    """Battery sensor for a Zemote smart lock.

    Numeric percentage when the device reports a number;
    otherwise shows a text label (e.g. 'ok', 'offline').
    """

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
        self._battery_label: str | None = None

        # Seed from already-received shadow state so we're not Unknown on startup
        existing = hub.device_states.get(self._serial, {})
        if "BAT" in existing:
            self._battery_pct, self._battery_label = _parse_bat(existing["BAT"])

    @property
    def native_value(self) -> int | None:
        """Return numeric % when available, else None (HA shows Unknown)."""
        return self._battery_pct

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._battery_label is not None:
            return {"battery_status": self._battery_label}
        return {}

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
        self._battery_pct, self._battery_label = _parse_bat(reported["BAT"])
        self.async_write_ha_state()
