"""Zemote lock platform — standalone smart lock modules.

Unlock is restricted to local network requests only.
Lock modules use custom ST/SC payloads instead of normal channel values.
Default state is locked (fail-safe) until real state is fetched from server.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_STATE_UPDATED

_LOGGER = logging.getLogger(__name__)

_LOCAL_NETWORKS = [
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
]


def _is_local_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return any(ip in net for net in _LOCAL_NETWORKS)
    except ValueError:
        return False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    locks = [ZemoteLock(hub, d) for d in hub.devices if d["platform"] == "lock"]
    async_add_entities(locks, True)


class ZemoteLock(LockEntity):
    """Represents a Zemote standalone lock module."""

    _attr_has_entity_name = True

    def __init__(self, hub: Any, device: dict) -> None:
        self._hub    = hub
        self._device = device
        self._serial = device["serialNumber"]

        self._attr_unique_id = f"zemote_{device['applianceId']}"
        self._attr_name = device.get("rawName") or device["name"]

        room = device.get("roomName") or ""
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=device.get("roomName") or device.get("hubName") or self._serial,
            manufacturer="Zemote",
            model="Smart Lock",
            suggested_area=room or None,
        )

    @property
    def is_locked(self) -> bool:
        state = self._hub.device_states.get(self._serial, {})
        if not state:
            return True
        st = str(state.get("ST", "")).lower()
        sc = str(state.get("SC", "")).lower()
        if st in {"on", "ok"} and sc not in {"on", "ok"}:
            return True
        if sc in {"on", "ok"} or st == "off":
            return False
        return True

    async def async_lock(self, **kwargs: Any) -> None:
        self._hub.publish(self._serial, {"SC": "off", "ST": "on"})

    async def async_unlock(self, **kwargs: Any) -> None:
        origin_ip = self._get_origin_ip()
        if origin_ip is None:
            _LOGGER.debug("Zemote: unlock allowed (no origin IP — internal call)")
        elif not _is_local_ip(origin_ip):
            _LOGGER.warning(
                "Zemote: unlock BLOCKED for '%s' — origin IP %s is not on local network",
                self._attr_name,
                origin_ip,
            )
            return
        self._hub.publish(self._serial, {"SC": "on", "ST": "off"})

    def _get_origin_ip(self) -> str | None:
        context = getattr(self, "_context", None)
        if context is None:
            return None
        return getattr(context, "origin_ip", None)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_STATE_UPDATED}_{self._serial}",
                self._handle_update,
            )
        )
        await self.hass.async_add_executor_job(self._request_state)

    def _request_state(self) -> None:
        if self._hub._mqtt and self._hub._mqtt.is_connected():
            topic = f"$aws/things/{self._serial}/shadow/get"
            self._hub._mqtt.publish(topic, "", qos=0)
            _LOGGER.debug("Zemote lock: requested shadow state for %s", self._serial)

    @callback
    def _handle_update(self, reported: dict) -> None:
        if "ST" in reported or "SC" in reported:
            self.async_write_ha_state()
