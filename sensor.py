"""Zemote sensor platform — battery status sensor for smart locks.

The Zemote lock shadow reports BAT as a string status:
  "ok"      → battery healthy
  "low"     → battery low
  "offline" → device offline
  "203"     → numeric codes possible in some firmware
There is no numeric percentage in the shadow protocol.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_STATE_UPDATED

_LOGGER = logging.getLogger(__name__)

# Numeric BAT codes the device sometimes sends
_BAT_CODES: dict[str, str] = {
    "203": "ok",
    "205": "offline",
}


def _parse_bat(raw: Any) -> str:
    """Normalise raw BAT value to a human-readable string."""
    s = str(raw).strip()
    return _BAT_CODES.get(s, s)


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
    """Battery status sensor for a Zemote smart lock.

    Displays the raw BAT status string from the device shadow
    (e.g. 'ok', 'low', 'offline').
    """

    _attr_has_entity_name = False
    _attr_icon = "mdi:battery"

    def __init__(self, hub: Any, device: dict) -> None:
        self._hub    = hub
        self._serial = device["serialNumber"]

        self._attr_unique_id = f"zemote_{device['applianceId']}_battery"
        self._attr_name      = f"{device['name']} Battery"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
        )

        self._status: str | None = None

        # Seed from already-received shadow state
        existing = hub.device_states.get(self._serial, {})
        if "BAT" in existing:
            self._status = _parse_bat(existing["BAT"])

    @property
    def native_value(self) -> str | None:
        return self._status

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
        self._status = _parse_bat(reported["BAT"])
        _LOGGER.debug("Zemote lock %s battery: %s", self._serial, self._status)
        self.async_write_ha_state()
