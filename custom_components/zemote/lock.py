"""Zemote lock platform — standalone smart lock modules.

Hardware behaviour:
  - The lock is ALWAYS physically locked by default.
  - Sending NLK=<device_id> temporarily unlocks it; hardware auto-relocks
    itself after a few seconds (no manual lock command needed).
  - Sending NLK="ok" resets the NLK state field only.
  - Shadow reported is always empty on this device — lock state is tracked
    purely via NLK responses on the update/accepted topic.

NLK response codes:
  ok  / 203 → unlocked successfully
  013       → device ID not matched
  206       → vacation mode ON, unlock blocked
  205       → lock offline
  002       → access denied
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_STATE_UPDATED

_LOGGER = logging.getLogger(__name__)

# How long (seconds) to show "unlocked" in HA before auto-relocking.
AUTO_RELOCK_SECS = 10

_ANDROID_ID_RE = re.compile(r'^[0-9a-f]{16}$', re.IGNORECASE)

_NLK_CODES: dict[str, str] = {
    "ok":  "command accepted",
    "203": "unlocked",
    "002": "access denied — device ID not authorised",
    "205": "lock is offline",
    "013": "device ID not matched",
    "206": "vacation mode ON — unlock blocked",
}


def _pick_device_id(lock_data: list[dict]) -> str | None:
    if not lock_data:
        return None
    for entry in lock_data:
        did = entry.get("deviceId", "")
        if _ANDROID_ID_RE.match(did):
            return did
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

        lock_data: list[dict] = device.get("lockData", [])
        self._device_id: str | None = _pick_device_id(lock_data)
        if self._device_id:
            _LOGGER.info(
                "Zemote lock %s: using device_id=%s for NLK unlock",
                self._serial, self._device_id,
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

        # Hardware is always locked by default
        self._locked: bool = True
        self._relock_timer: threading.Timer | None = None

        self._battery: str | None = None
        self._rssi: str | None = None
        self._firmware: str | None = None
        self._uptime: str | None = None
        self._vacation: str | None = None
        self._nlk_last: str | None = None

    # ── State ──────────────────────────────────────────────────

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

    # ── Actions ──────────────────────────────────────────────────

    async def async_unlock(self, **kwargs: Any) -> None:
        if not self._device_id:
            _LOGGER.error(
                "Zemote lock %s: cannot unlock — no device ID available", self._serial
            )
            return
        _LOGGER.info("Zemote lock %s: sending UNLOCK (NLK=%s)", self._serial, self._device_id)
        await self.hass.async_add_executor_job(self._send_nlk, self._device_id)

    async def async_lock(self, **kwargs: Any) -> None:
        """Hardware auto-relocks — just update HA state."""
        _LOGGER.info("Zemote lock %s: lock called — resetting HA state", self._serial)
        self._cancel_relock_timer()
        self._locked = True
        self.async_write_ha_state()

    def _send_nlk(self, nlk_value: str) -> None:
        """Publish NLK and wait for response via a dedicated paho subscription.

        Mirrors the working CLI script: subscribe to update/accepted,
        publish the NLK desired payload, wait up to 15s for the echo.
        Uses a unique mid-string so we can cleanly unsubscribe after.
        """
        topic_accepted = f"$aws/things/{self._serial}/shadow/update/accepted"
        received = threading.Event()
        result: list[str] = []

        # Use a unique callback tag so multiple locks don't cross-fire
        cb_id = f"_nlk_{uuid.uuid4().hex[:8]}"

        def _on_nlk_msg(client, userdata, message):
            try:
                data = json.loads(message.payload.decode())
                reported = data.get("state", {}).get("reported", {})
                nlk = reported.get("NLK")
                if nlk and not received.is_set():
                    result.append(nlk)
                    received.set()
            except Exception as err:
                _LOGGER.debug("Zemote lock NLK parse error: %s", err)

        mqtt_client = self._hub._mqtt

        # Subscribe with a message-callback-add so we don't disturb the
        # hub's global on_message handler
        mqtt_client.message_callback_add(topic_accepted, _on_nlk_msg)
        mqtt_client.subscribe(topic_accepted, qos=1)

        try:
            self._hub.publish(self._serial, {"NLK": nlk_value})
            _LOGGER.debug("Zemote lock %s: waiting 15s for NLK response", self._serial)
            received.wait(timeout=15)
        finally:
            mqtt_client.unsubscribe(topic_accepted)
            mqtt_client.message_callback_remove(topic_accepted)

        if result:
            nlk = result[0]
            self._nlk_last = nlk
            desc = _NLK_CODES.get(nlk, f"unknown response: {nlk}")
            _LOGGER.info("Zemote lock %s NLK=%s (%s)", self._serial, nlk, desc)

            if nlk in ("ok", "203"):
                self._locked = False
                self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)
                self._schedule_relock()
            elif nlk == "206":
                _LOGGER.warning(
                    "Zemote lock %s: unlock blocked — vacation mode ON", self._serial
                )
                self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)
            else:
                _LOGGER.warning(
                    "Zemote lock %s: unlock failed NLK=%s (%s)", self._serial, nlk, desc
                )
                self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)
        else:
            _LOGGER.warning("Zemote lock %s: no NLK response within 15s", self._serial)

    # ── Auto-relock timer ─────────────────────────────────────────────

    def _schedule_relock(self) -> None:
        self._cancel_relock_timer()
        self._relock_timer = threading.Timer(AUTO_RELOCK_SECS, self._do_relock)
        self._relock_timer.daemon = True
        self._relock_timer.start()
        _LOGGER.debug(
            "Zemote lock %s: auto-relock in %ds", self._serial, AUTO_RELOCK_SECS
        )

    def _cancel_relock_timer(self) -> None:
        if self._relock_timer is not None:
            self._relock_timer.cancel()
            self._relock_timer = None

    def _do_relock(self) -> None:
        _LOGGER.info("Zemote lock %s: auto-relocking HA state", self._serial)
        self._locked = True
        self._relock_timer = None
        self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

    # ── Shadow updates (attributes only) ──────────────────────────────

    @callback
    def _handle_update(self, reported: dict) -> None:
        changed = False

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

    # ── HA lifecycle ──────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_STATE_UPDATED}_{self._serial}",
                self._handle_update,
            )
        )
        await self.hass.async_add_executor_job(self._request_shadow_state)

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_relock_timer()

    def _request_shadow_state(self) -> None:
        if self._hub._mqtt and self._hub._mqtt.is_connected():
            topic = f"$aws/things/{self._serial}/shadow/get"
            self._hub._mqtt.publish(topic, "", qos=0)
            _LOGGER.debug("Zemote lock: requested shadow state for %s", self._serial)
