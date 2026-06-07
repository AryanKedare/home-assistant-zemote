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

Security — home-network safeguard:
  async_unlock() calls _enforce_home_network() before publishing the
  NLK command.  The check uses two fields from the HA request context:

    origin_ip  — set by HA HTTP middleware on every direct HTTP request
                  (web UI, Companion App on LAN, REST API)
    user_id    — set for any authenticated session, including cloud relay
                  sessions (Nabu Casa) that carry no origin_ip

  Decision table:
    origin_ip present, local subnet   → ALLOW
    origin_ip present, external IP    → BLOCK
    origin_ip absent,  user_id absent → ALLOW  (internal: automation/script)
    origin_ip absent,  user_id set    → BLOCK  (cloud/remote: Nabu Casa etc.)

Spurious-unlock prevention:
  AWS IoT echoes shadow updates back as update/accepted messages that include
  the previous reported state (including old NLK values).  Additionally,
  shadow/get responses on startup replay the last stored NLK.  To avoid
  acting on stale NLK echoes, _handle_update tracks the last *processed*
  NLK value and ignores repeated/identical values — a real unlock always
  arrives with _unlock_pending=True set by async_unlock().
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
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
    locks = [ZemoteLock(hub, d) for d in hub.devices if d.get("platform") == "lock"]
    async_add_entities(locks, True)


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

        # Spurious-unlock guard — see module docstring.
        self._nlk_processed: str | None = None
        self._unlock_pending: bool = False

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
        """Unlock the door — only permitted from the home network.

        See module docstring for the full decision table.
        """
        if not self._device_id:
            _LOGGER.error("Zemote lock %s: no device ID — cannot unlock", self._serial)
            return

        await self._enforce_home_network(kwargs)

        self._unlock_pending = True
        _LOGGER.info("Zemote lock %s: publishing NLK=%s", self._serial, self._device_id)
        self._hub.publish(self._serial, {"NLK": self._device_id})

    async def _enforce_home_network(self, kwargs: dict) -> None:
        """Raise HomeAssistantError if the caller is not on the home network.

        Decision table (see module docstring for full explanation):
          origin_ip present + local subnet   → allow
          origin_ip present + external IP    → block
          origin_ip absent  + user_id absent → allow  (internal automation/script)
          origin_ip absent  + user_id set    → block  (cloud/remote — Nabu Casa etc.)
        """
        from homeassistant.components import network

        context = kwargs.get("context") or getattr(self, "_context", None)
        caller_ip_str: str | None = getattr(context, "origin_ip", None)
        user_id: str | None = getattr(context, "user_id", None)

        if caller_ip_str is None:
            if user_id is None:
                # True internal call: automation, script, or other HA service
                # running inside the HA process with no user session.
                _LOGGER.debug(
                    "Zemote lock %s: no origin_ip and no user_id — "
                    "allowing trusted internal call",
                    self._serial,
                )
                return
            else:
                # A user session exists but has no origin_ip.  This is the
                # fingerprint of a Nabu Casa / remote cloud relay call —
                # the request was forwarded by the cloud and the original
                # external IP was not preserved as origin_ip.
                _LOGGER.warning(
                    "Zemote lock %s: unlock BLOCKED — remote/cloud session "
                    "(user_id=%s, no origin_ip — likely Nabu Casa or remote access)",
                    self._serial, user_id,
                )
                raise HomeAssistantError(
                    "Unlock blocked: remote access via the cloud is not permitted. "
                    "Connect to your home Wi-Fi to unlock the door."
                )

        # origin_ip is present — check it against local subnets.
        try:
            caller_ip = ipaddress.ip_address(caller_ip_str)
        except ValueError:
            _LOGGER.warning(
                "Zemote lock %s: unparseable origin_ip %r — blocking as precaution",
                self._serial, caller_ip_str,
            )
            raise HomeAssistantError(
                f"Unlock blocked: could not parse caller IP ({caller_ip_str!r})"
            )

        try:
            adapters = await network.async_get_adapters(self.hass)
        except Exception as exc:
            _LOGGER.warning(
                "Zemote lock %s: failed to retrieve network adapters (%s) — blocking unlock",
                self._serial, exc,
            )
            raise HomeAssistantError(
                "Unlock blocked: could not verify home network adapters"
            ) from exc

        on_home_network = False
        for adapter in adapters:
            for ipv4 in adapter.get("ipv4", []):
                try:
                    iface_net = ipaddress.IPv4Network(
                        f"{ipv4['address']}/{ipv4['network_prefix']}", strict=False
                    )
                    if caller_ip in iface_net:
                        _LOGGER.debug(
                            "Zemote lock %s: caller %s matched local subnet %s — allowing",
                            self._serial, caller_ip_str, iface_net,
                        )
                        on_home_network = True
                        break
                except (ValueError, KeyError):
                    continue
            if on_home_network:
                break

        if not on_home_network:
            _LOGGER.warning(
                "Zemote lock %s: unlock BLOCKED — caller IP %s is not on the home network",
                self._serial, caller_ip_str,
            )
            raise HomeAssistantError(
                f"Unlock blocked: your device ({caller_ip_str}) is not on the home network. "
                "Connect to your home Wi-Fi to unlock the door."
            )

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

            # ── Spurious-unlock guard ─────────────────────────
            if nlk == self._nlk_processed and not self._unlock_pending:
                _LOGGER.debug(
                    "Zemote lock %s: ignoring repeated/echoed NLK=%s",
                    self._serial, nlk,
                )
                if changed:
                    self.async_write_ha_state()
                return
            # ── End guard ─────────────────────────────────────

            self._nlk_processed = nlk
            self._unlock_pending = False
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
        # NOTE: Intentionally no shadow/get here.
        # A get/accepted response replays the last stored NLK from AWS IoT,
        # which would cause a spurious unlock on every HA restart.
        # Lock state is event-driven — only changes on a real async_unlock().

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_relock()
