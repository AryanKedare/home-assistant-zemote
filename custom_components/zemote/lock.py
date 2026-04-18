"""Zemote lock platform — door lock modules.

Unlock is restricted to local network requests only.
Lock (securing) is always allowed from anywhere.
"""
from __future__ import annotations

import ipaddress
import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_STATE_UPDATED

_LOGGER = logging.getLogger(__name__)

# RFC-1918 + link-local private ranges
_LOCAL_NETWORKS = [
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
]


def _is_local_ip(ip_str: str) -> bool:
    """Return True if ip_str is within a private/local network range."""
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
    locks = [
        ZemoteLock(hub, d)
        for d in hub.devices
        if d["platform"] == "lock"
    ]
    async_add_entities(locks, True)


class ZemoteLock(LockEntity):
    """Represents a Zemote lock channel.

    Unlock is only permitted when called from a local/home network IP.
    Locking is always permitted (fail-safe: locking from anywhere is fine).
    """

    def __init__(self, hub: Any, device: dict) -> None:
        self._hub     = hub
        self._device  = device
        self._serial  = device["serialNumber"]
        self._channel = device["channelKey"]

        self._attr_unique_id = f"zemote_{device['applianceId']}"
        self._attr_name      = device["name"]

        room = device.get("roomName")
        if room:
            self._attr_suggested_area = room

    @property
    def is_locked(self) -> bool | None:
        val = self._hub.get_channel_state(self._serial, self._channel)
        try:
            # 0 = unlocked, 1 = locked  (matches Zemote shadow convention)
            return int(val) == 1
        except (TypeError, ValueError):
            return None

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock — always allowed regardless of origin."""
        self._hub.set_channel(self._serial, self._channel, 1)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock — only allowed from local/home network."""
        origin_ip = self._get_origin_ip()

        if origin_ip is None:
            # No IP context means it came from an internal automation/script on
            # the HA host itself — treat as local and allow.
            _LOGGER.debug("Zemote: unlock allowed (no origin IP — internal call)")
        elif not _is_local_ip(origin_ip):
            _LOGGER.warning(
                "Zemote: unlock BLOCKED for '%s' — origin IP %s is not on local network",
                self._attr_name,
                origin_ip,
            )
            return

        self._hub.set_channel(self._serial, self._channel, 0)

    def _get_origin_ip(self) -> str | None:
        """Extract origin IP from HA context if available."""
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
        room = self._device.get("roomName")
        if room:
            await self._async_assign_area(room)

    async def _async_assign_area(self, room_name: str) -> None:
        """Look up or create the area and assign this device to it."""
        from homeassistant.helpers import area_registry as ar, device_registry as dr
        area_reg   = ar.async_get(self.hass)
        device_reg = dr.async_get(self.hass)

        area = area_reg.async_get_area_by_name(room_name)
        if area is None:
            area = area_reg.async_create(room_name)

        device = device_reg.async_get_device(identifiers={(DOMAIN, self._attr_unique_id)})
        if device and device.area_id != area.id:
            device_reg.async_update_device(device.id, area_id=area.id)

    @callback
    def _handle_update(self, reported: dict) -> None:
        if self._channel in reported:
            self.async_write_ha_state()
