"""Zemote lock platform — standalone smart lock modules.

Hardware behaviour:
  - Always physically locked by default.
  - NLK=<device_id> temporarily unlocks; hardware auto-relocks after a few seconds.
  - No manual lock command exists.
  - Shadow reported is always empty — lock state driven purely by NLK response.

Flow:
  async_unlock()
    └─ hub.publish(NLK=device_id)          [fire-and-forget, non-blocking]
  _on_shadow_message() in hub
    └─ dispatcher → _handle_update(reported)
        └─ NLK in reported → update state + schedule auto-relock
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
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
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, SIGNAL_STATE_UPDATED

_LOGGER = logging.getLogger(__name__)

AUTO_RELOCK_SECS = 10

_NLK_CODES: dict[str, str] = {
    "ok":  "command accepted",
    "203": "unlocked",
    "002": "access denied — device ID not authorised",
    "205": "lock is offline",
    "013": "device ID not matched",
    "206": "vacation mode ON — unlock blocked",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for d in hub.devices:
        if d.get("platform") == "lock":
            lock = ZemoteLock(hub, d)
            entities.append(lock)
            entities.append(ZemoteLockBattery(hub, d, lock))
    async_add_entities(entities, True)


class ZemoteLock(LockEntity):
    """Zemote smart lock — unlock only, hardware auto-relocks."""

    _attr_has_entity_name = False
    _attr_supported_features = LockEntityFeature(0)

    def __init__(self, hub: Any, device: dict) -> None:
        self._hub = hub
        self._serial: str = device["serialNumber"]

        self._device_id: str | None = device.get("deviceId") or None
        if self._device_id:
            _LOGGER.info("Zemote lock %s: device_id=%s", self._serial, self._device_id)
        else:
            _LOGGER.warning("Zemote lock %s: no device ID — cannot unlock", self._serial)

        self._attr_unique_id = f"zemote_{device['applianceId']}"
        self._attr_name = device["name"]
        room = device.get("roomName") or ""
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=device.get("hubName") or self._serial,
            manufacturer="Zemote",
            model=device.get("serialNumber"),
            serial_number=device.get("serialNumber"),
            suggested_area=room or None,
        )

        self._locked: bool = True
        self._relock_cancel = None

        self._battery: str | None = None
        self._battery_pct: int | None = None
        self._rssi: str | None = None
        self._firmware: str | None = None
        self._uptime: str | None = None
        self._vacation: str | None = None
        self._nlk_last: str | None = None

    # ── State ───────────────────────────────────────────────

    @property
    def is_locked(self) -> bool:
        return self._locked

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        if self._battery is not None:
            attrs["battery"] = self._battery
        if self._rssi is not None:
            attrs["rssi"] = self._rssi
        if self._firmware is not None:
            attrs["firmware"] = self._firmware
        if self._uptime is not None:
            attrs["uptime"] = self._uptime
        if self._vacation is not None:
            attrs["vacation_mode"] = self._vacation
        if self._nlk_last is not None:
            attrs["nlk_last_response"] = self._nlk_last
        if self._device_id:
            attrs["device_id"] = self._device_id
        return attrs

    # ── Actions ─────────────────────────────────────────────

    async def async_unlock(self, **kwargs: Any) -> None:
        if not self._device_id:
            _LOGGER.error("Zemote lock %s: no device ID — cannot unlock", self._serial)
            return
        _LOGGER.info("Zemote lock %s: publishing NLK=%s", self._serial, self._device_id)
        self._hub.publish(self._serial, {"NLK": self._device_id})

    async def async_lock(self, **kwargs: Any) -> None:
        """Hardware auto-relocks — just reset HA state immediately."""
        self._cancel_relock()
        self._locked = True
        self.async_write_ha_state()

    # ── Update handler ────────────────────────────────────────

    @callback
    def _handle_update(self, reported: dict) -> None:
        changed = False

        if "BAT" in reported:
            try:
                b = int(reported["BAT"])
                if b == 205:
                    self._battery = "offline"
                    self._battery_pct = None
                elif b <= 100:
                    self._battery = f"{b}%"
                    self._battery_pct = b
                elif b <= 201:
                    self._battery = f"{b - 100}% (unregistered)"
                    self._battery_pct = b - 100
                else:
                    self._battery = str(b)
                    self._battery_pct = None
            except (ValueError, TypeError):
                self._battery = str(reported["BAT"])
                self._battery_pct = None
            changed = True

        for key, attr in (("RSSI", "_rssi"), ("CV", "_firmware"),
                          ("ONTIME", "_uptime"), ("SET", "_vacation")):
            if key in reported:
                setattr(self, attr, str(reported[key]))
                changed = True

        if "NLK" in reported:
            nlk = str(reported["NLK"])
            self._nlk_last = nlk
            desc = _NLK_CODES.get(nlk, f"unknown: {nlk}")
            _LOGGER.info("Zemote lock %s NLK=%s (%s)", self._serial, nlk, desc)

            if nlk in ("ok", "203"):
                self._locked = False
                self._schedule_relock()
            elif nlk == "206":
                _LOGGER.warning("Zemote lock %s: unlock blocked — vacation mode ON", self._serial)
            else:
                _LOGGER.warning("Zemote lock %s: NLK=%s — %s", self._serial, nlk, desc)
            changed = True

        if changed:
            self.async_write_ha_state()

    # ── Auto-relock ──────────────────────────────────────────

    @callback
    def _schedule_relock(self) -> None:
        self._cancel_relock()
        self._relock_cancel = async_call_later(
            self.hass, AUTO_RELOCK_SECS, self._do_relock
        )
        _LOGGER.debug("Zemote lock %s: auto-relock in %ds", self._serial, AUTO_RELOCK_SECS)

    @callback
    def _do_relock(self, _now: Any) -> None:
        _LOGGER.info("Zemote lock %s: auto-relocking", self._serial)
        self._locked = True
        self._relock_cancel = None
        self.async_write_ha_state()

    def _cancel_relock(self) -> None:
        if self._relock_cancel is not None:
            self._relock_cancel()
            self._relock_cancel = None

    # ── HA lifecycle ─────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_STATE_UPDATED}_{self._serial}",
                self._handle_update,
            )
        )
        if self._hub._mqtt and self._hub._mqtt.is_connected():
            self._hub._mqtt.publish(
                f"$aws/things/{self._serial}/shadow/get", "", qos=0
            )

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_relock()


class ZemoteLockBattery(SensorEntity):
    """Battery percentage sensor for a Zemote smart lock."""

    _attr_has_entity_name   = False
    _attr_device_class      = SensorDeviceClass.BATTERY
    _attr_state_class       = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, hub: Any, device: dict, lock: ZemoteLock) -> None:
        self._lock   = lock
        self._serial = device["serialNumber"]

        self._attr_unique_id = f"zemote_{device['applianceId']}_battery"
        self._attr_name      = f"{device['name']} Battery"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
        )

    @property
    def native_value(self) -> int | None:
        return self._lock._battery_pct

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
        if "BAT" in reported:
            self.async_write_ha_state()
