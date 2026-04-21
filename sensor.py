"""Zemote sensor platform — battery percentage sensor for smart locks.

The Zemote lock shadow reports BAT as a numeric value (mirrored from
AWSSubscriptionAndFeedback.java lambdasubscribetotopicupdateaccepted4):

  0–100   → battery percentage directly
  101–201 → (value - 100)% for unregistered/guest device IDs
  205     → device offline
  other   → unknown, stored as raw string

BAT arrives via:
  - shadow/update/accepted  (live MQTT push)
  - shadow/get/accepted     (requested on startup by lock entity & hub._ping_all)

Both paths go through hub._on_shadow_message → SIGNAL_STATE_UPDATED dispatch
→ _handle_update here, so battery is populated on first load from cloud.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_STATE_UPDATED

_LOGGER = logging.getLogger(__name__)


def _parse_bat(raw: Any) -> tuple[int | None, str]:
    """Return (pct_or_None, display_string) from a raw BAT shadow value.

    Mirrors the BAT parsing logic in ZemoteLock._handle_update (lock.py).
    """
    try:
        b = int(raw)
    except (ValueError, TypeError):
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
    """Battery percentage sensor for a Zemote smart lock.

    Reads BAT from the AWS IoT Thing Shadow (state.reported.BAT).
    Value is fetched from cloud via shadow GET on startup — no device
    connection required to see the last-known battery level.
    """

    _attr_has_entity_name = False
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:battery"

    def __init__(self, hub: Any, device: dict) -> None:
        self._hub    = hub
        self._serial = device["serialNumber"]

        self._attr_unique_id = f"zemote_{device['applianceId']}_battery"
        self._attr_name      = f"{device['name']} Battery"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
        )

        self._pct: int | None = None
        self._display: str | None = None

        # Seed from already-received shadow state (e.g. if hub got the GET
        # response before this entity was added to HA)
        existing = hub.device_states.get(self._serial, {})
        if "BAT" in existing:
            self._pct, self._display = _parse_bat(existing["BAT"])

    @property
    def native_value(self) -> int | None:
        """Return battery percentage (0-100), or None if unknown/offline."""
        return self._pct

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        if self._display is not None:
            attrs["battery_status"] = self._display
        return attrs

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
        self._pct, self._display = _parse_bat(reported["BAT"])
        _LOGGER.debug(
            "Zemote lock %s battery: %s (%s%%)", self._serial, self._display, self._pct
        )
        self.async_write_ha_state()
