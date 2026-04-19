"""Zemote lock platform — standalone smart lock modules.

Unlock uses the NLK mechanism: publish NLK=<device_id> to unlock,
NLK="ok" to reset/lock. Device ID is auto-selected from lockData
stored in the device dict, preferring Android hex IDs (non-UUID format).

NLK response codes:
  ok  / 203 → unlocked successfully
  013       → device ID not matched
  206       → vacation mode ON, unlock blocked
  205       → lock offline
  002       → access denied
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_STATE_UPDATED

_LOGGER = logging.getLogger(__name__)

# Matches Android hex IDs (16 hex chars). iOS UUIDs are longer with dashes.
_ANDROID_ID_RE = re.compile(r'^[0-9a-f]{16}$', re.IGNORECASE)

_NLK_CODES: dict[str, str] = {
    "ok":  "unlocked/command accepted",
    "203": "unlocked",
    "002": "access denied — device ID not authorised",
    "205": "lock is offline",
    "013": "device ID not matched",
    "206": "vacation mode ON — unlock blocked",
}


def _pick_device_id(lock_data: list[dict]) -> str | None:
    """Pick the best device ID from lockData.

    Preference order:
      1. Android hex ID (16 lowercase hex chars) — most reliable
      2. Any iOS UUID as fallback
    Returns None if lockData is empty.
    """
    if not lock_data:
        return None
    # Prefer Android IDs
    for entry in lock_data:
        did = entry.get("deviceId", "")
        if _ANDROID_ID_RE.match(did):
            return did
    # Fall back to first available
    return lock_data[0].get("deviceId") or None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    locks = [ZemoteLock(hub, d) for d in hub.devices if d.get("platform") == "lock"]
    async_add_entities(locks, True)


class ZemoteLock(LockEntity):
    """Represents a Zemote standalone smart lock module."""

    _attr_has_entity_name = False
    _attr_supported_features = LockEntityFeature(0)

    def __init__(self, hub: Any, device: dict) -> None:
        self._hub = hub
        self._device = device
        self._serial: str = device["serialNumber"]

        # Auto-select device ID from lockData
        lock_data: list[dict] = device.get("lockData", [])
        self._device_id: str | None = _pick_device_id(lock_data)
        if self._device_id:
            _LOGGER.info(
                "Zemote lock %s: using device_id=%s for NLK unlock",
                self._serial,
                self._device_id,
            )
        else:
            _LOGGER.warning(
                "Zemote lock %s: no device ID found in lockData — unlock will be skipped",
                self._serial,
            )

        self._attr_unique_id = f"zemote_{device['applianceId']}"
        self._attr_name = device["name"]

        room = device.get("roomName") or ""
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=device.get("hubName") or self._serial,
            manufacturer="Zemote",
            model="Smart Lock",
            suggested_area=room or None,
        )

        # Internal state — default locked until shadow reports otherwise
        self._locked: bool = True
        self._battery: str | None = None
        self._rssi: str | None = None
        self._firmware: str | None = None
        self._uptime: str | None = None
        self._vacation: str | None = None
        self._nlk_last: str | None = None

    # ── State ────────────────────────────────────────────────────────────────

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

    # ── Actions ──────────────────────────────────────────────────────────────

    async def async_unlock(self, **kwargs: Any) -> None:
        if not self._device_id:
            _LOGGER.error(
                "Zemote lock %s: cannot unlock — no device ID available", self._serial
            )
            return
        _LOGGER.info("Zemote lock %s: sending UNLOCK (NLK=%s)", self._serial, self._device_id)
        await self.hass.async_add_executor_job(self._send_nlk, self._device_id)

    async def async_lock(self, **kwargs: Any) -> None:
        _LOGGER.info("Zemote lock %s: sending LOCK/RESET (NLK=ok)", self._serial)
        await self.hass.async_add_executor_job(self._send_nlk, "ok")

    def _send_nlk(self, nlk_value: str) -> None:
        """Publish NLK desired state and wait up to 15 s for the reported echo."""
        received = threading.Event()
        result: list[str] = []

        original_handler = self._hub._mqtt.on_message

        def _on_nlk_response(client, userdata, message):
            # Always forward to the hub's normal handler
            if original_handler:
                original_handler(client, userdata, message)
            try:
                import json
                data = json.loads(message.payload.decode())
                reported = data.get("state", {}).get("reported", {})
                nlk = reported.get("NLK")
                if nlk and not received.is_set():
                    result.append(nlk)
                    received.set()
            except Exception:
                pass

        self._hub._mqtt.on_message = _on_nlk_response
        try:
            self._hub.publish(self._serial, {"NLK": nlk_value})
            received.wait(timeout=15)
        finally:
            self._hub._mqtt.on_message = original_handler

        if result:
            nlk = result[0]
            self._nlk_last = nlk
            desc = _NLK_CODES.get(nlk, f"unknown response: {nlk}")
            _LOGGER.info("Zemote lock %s NLK response: %s (%s)", self._serial, nlk, desc)

            # Update locked state based on response
            if nlk in ("ok", "203"):
                if nlk_value == "ok":
                    self._locked = True
                else:
                    self._locked = False
            elif nlk == "206":
                _LOGGER.warning("Zemote lock %s: unlock blocked — vacation mode ON", self._serial)

            self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)
        else:
            _LOGGER.warning("Zemote lock %s: no NLK response within 15s", self._serial)

    # ── Shadow state parsing ──────────────────────────────────────────────────

    @callback
    def _handle_update(self, reported: dict) -> None:
        changed = False

        # Lock state: L1=0 → locked, L1=1 → unlocked
        if "L1" in reported:
            l1 = reported["L1"]
            new_locked = (int(l1) == 0)
            if new_locked != self._locked:
                self._locked = new_locked
                changed = True

        # Battery
        if "BAT" in reported:
            try:
                bat_int = int(reported["BAT"])
                if bat_int == 205:
                    self._battery = "offline"
                elif bat_int <= 100:
                    self._battery = f"{bat_int}%"
                elif bat_int <= 201:
                    self._battery = f"{bat_int - 100}% (unregistered)"
                else:
                    self._battery = str(bat_int)
            except (ValueError, TypeError):
                self._battery = str(reported["BAT"])
            changed = True

        if "RSSI" in reported:
            self._rssi = str(reported["RSSI"])
            changed = True

        if "CV" in reported:
            self._firmware = str(reported["CV"])
            changed = True

        if "ONTIME" in reported:
            self._uptime = str(reported["ONTIME"])
            changed = True

        if "SET" in reported:
            self._vacation = str(reported["SET"])
            changed = True

        if "NLK" in reported:
            self._nlk_last = str(reported["NLK"])
            changed = True

        if changed:
            self.async_write_ha_state()

    # ── HA lifecycle ─────────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_STATE_UPDATED}_{self._serial}",
                self._handle_update,
            )
        )
        await self.hass.async_add_executor_job(self._request_shadow_state)

    def _request_shadow_state(self) -> None:
        if self._hub._mqtt and self._hub._mqtt.is_connected():
            topic = f"$aws/things/{self._serial}/shadow/get"
            self._hub._mqtt.publish(topic, "", qos=0)
            _LOGGER.debug("Zemote lock: requested shadow state for %s", self._serial)
