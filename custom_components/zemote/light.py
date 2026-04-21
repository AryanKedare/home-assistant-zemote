"""Zemote light platform — normal lights + MOODlight (cesrm).

Normal lights  : platform=="light"     → ZemoteLight   (on/off + dimmer)
MOODlight      : platform=="moodlight" → ZemoteMoodlight (RGB colour wheel)

MOODlight protocol (confirmed from live AWS IoT shadow):
  key   : MDL
  value : "RRR,GGG,BBB"  zero-padded 3-digit decimals  e.g. "083,008,126"
  off   : "000,000,000"
"""
from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_STATE_UPDATED

_LOGGER = logging.getLogger(__name__)

_DEFAULT_RGB: tuple[int, int, int] = (127, 127, 127)


# ── helpers ───────────────────────────────────────────────────────── #

def _mdl_str(r: int, g: int, b: int) -> str:
    return f"{int(r):03d},{int(g):03d},{int(b):03d}"


def _parse_mdl(mdl: str) -> tuple[int, int, int] | None:
    try:
        parts = mdl.split(",")
        if len(parts) != 3:
            return None
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, AttributeError):
        return None


# ── platform setup ────────────────────────────────────────────────── #

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    entities: list[LightEntity] = []

    for d in hub.devices:
        if d["platform"] == "light":
            entities.append(ZemoteLight(hub, d))
        elif d["platform"] == "moodlight":
            entities.append(ZemoteMoodlight(hub, d))

    async_add_entities(entities, True)


# ── Normal light ──────────────────────────────────────────────────── #

class ZemoteLight(LightEntity):
    """On/off or dimmable light channel."""

    _attr_has_entity_name = True

    def __init__(self, hub: Any, device: dict) -> None:
        self._hub     = hub
        self._device  = device
        self._serial  = device["serialNumber"]
        self._channel = device["channelKey"]

        self._attr_unique_id = f"zemote_{device['applianceId']}"
        self._attr_name      = device.get("name")

        if device.get("dimmable", False):
            self._attr_color_mode            = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        else:
            self._attr_color_mode            = ColorMode.ONOFF
            self._attr_supported_color_modes = {ColorMode.ONOFF}

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device["applianceId"])},
            name=device.get("name"),
            manufacturer="Zemote",
            model="Hub Module",
            suggested_area=device.get("roomName") or None,
        )

    @property
    def is_on(self) -> bool:
        val = self._hub.get_channel_state(self._serial, self._channel)
        try:
            return int(val) > 0
        except (TypeError, ValueError):
            return False

    @property
    def brightness(self) -> int | None:
        if self._attr_color_mode != ColorMode.BRIGHTNESS:
            return None
        val = self._hub.get_channel_state(self._serial, self._channel)
        try:
            return math.ceil(int(val) / 100 * 255)
        except (TypeError, ValueError):
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._attr_color_mode == ColorMode.BRIGHTNESS:
            bri_255 = kwargs.get(ATTR_BRIGHTNESS, 255)
            bri_pct = max(1, min(100, round(bri_255 / 255 * 100)))
            self._hub.set_channel(self._serial, self._channel, bri_pct)
        else:
            self._hub.set_channel(self._serial, self._channel, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._hub.set_channel(self._serial, self._channel, 0)

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
        if self._channel in reported:
            self.async_write_ha_state()


# ── MOODlight ─────────────────────────────────────────────────────── #

class ZemoteMoodlight(LightEntity):
    """MOODlight RGB entity — cesrm serial, MDL = RRR,GGG,BBB protocol."""

    _attr_has_entity_name        = True
    _attr_color_mode             = ColorMode.RGB
    _attr_supported_color_modes  = {ColorMode.RGB}

    def __init__(self, hub: Any, device: dict) -> None:
        self._hub    = hub
        self._serial = device["serialNumber"]

        self._attr_unique_id  = f"zemote_{device['applianceId']}"
        self._attr_name       = device.get("name", "MOODlight")

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device["applianceId"])},
            name=device.get("name", "MOODlight"),
            manufacturer="Zemote",
            model="MOODlight",
            suggested_area=device.get("roomName") or None,
        )

        self._rgb: tuple[int, int, int] = _DEFAULT_RGB
        self._on:  bool                 = False

    @property
    def is_on(self) -> bool:
        return self._on

    @property
    def rgb_color(self) -> tuple[int, int, int]:
        return self._rgb

    @property
    def brightness(self) -> int:
        return max(self._rgb)

    async def async_turn_on(self, **kwargs: Any) -> None:
        r, g, b = self._rgb

        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]

        if ATTR_BRIGHTNESS in kwargs and ATTR_RGB_COLOR not in kwargs:
            scale = kwargs[ATTR_BRIGHTNESS] / 255
            r = round(r * scale)
            g = round(g * scale)
            b = round(b * scale)

        r = max(0, min(255, r))
        g = max(0, min(255, g))
        b = max(0, min(255, b))

        # avoid accidental off when brightness is dragged to 0
        if r == 0 and g == 0 and b == 0:
            r, g, b = _DEFAULT_RGB

        self._rgb = (r, g, b)
        self._on  = True
        self._hub.set_channel(self._serial, "MDL", _mdl_str(r, g, b))
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._on = False
        self._hub.set_channel(self._serial, "MDL", "000,000,000")
        self.async_write_ha_state()

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
        mdl = reported.get("MDL")
        if mdl is None:
            return
        parsed = _parse_mdl(mdl)
        if parsed is None:
            _LOGGER.warning("Zemote MOODlight: bad MDL value '%s'", mdl)
            return
        r, g, b = parsed
        if r == 0 and g == 0 and b == 0:
            self._on = False
        else:
            self._on  = True
            self._rgb = (r, g, b)
        self.async_write_ha_state()
