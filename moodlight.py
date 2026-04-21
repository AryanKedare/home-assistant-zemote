"""Zemote MOODlight platform.

Protocol (confirmed from live AWS IoT shadow capture):
  - Serial prefix : cesrm
  - Shadow key    : MDL
  - Value format  : "RRR,GGG,BBB"  (zero-padded 3-digit decimals, 0-255 each)
  - OFF           : "000,000,000"
  - ON (last)     : re-publishes the last known MDL value

Home Assistant entity type: light
  - ColorMode.RGB  → shows colour wheel in UI
  - Brightness     : scales R,G,B uniformly (0-255)
  - Turn on        : restores last colour (default 127,127,127 white-mid)
  - Turn off       : publishes MDL = 000,000,000
"""
from __future__ import annotations

import logging
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

# Default colour when turned on with no prior state
_DEFAULT_RGB = (127, 127, 127)


def _mdl_str(r: int, g: int, b: int) -> str:
    """Format RGB as zero-padded MDL string e.g. '083,008,126'."""
    return f"{int(r):03d},{int(g):03d},{int(b):03d}"


def _parse_mdl(mdl: str) -> tuple[int, int, int] | None:
    """Parse 'RRR,GGG,BBB' string into (r, g, b) tuple. Returns None on error."""
    try:
        parts = mdl.split(",")
        if len(parts) != 3:
            return None
        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
        return (r, g, b)
    except (ValueError, AttributeError):
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    entities = [
        ZemoteMoodlight(hub, d)
        for d in hub.devices
        if d.get("platform") == "moodlight"
    ]
    if entities:
        _LOGGER.info("Zemote: setting up %d MOODlight entity(s)", len(entities))
    async_add_entities(entities, True)


class ZemoteMoodlight(LightEntity):
    """Zemote MOODlight — full RGB colour wheel entity.

    The device accepts MDL = 'RRR,GGG,BBB' on the AWS IoT shadow/update topic.
    Reported state arrives on shadow/update/accepted as the same MDL key.
    OFF is MDL = '000,000,000'.
    """

    _attr_has_entity_name = True
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}

    def __init__(self, hub: Any, device: dict) -> None:
        self._hub     = hub
        self._device  = device
        self._serial  = device["serialNumber"]   # e.g. cesrm10004302

        self._attr_unique_id = f"zemote_{device['applianceId']}"
        self._attr_name      = device.get("name", "MOODlight")

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device["applianceId"])},
            name=device.get("name", "MOODlight"),
            manufacturer="Zemote",
            model="MOODlight (cesrm)",
            suggested_area=device.get("roomName") or None,
            via_device=(DOMAIN, self._serial),
        )

        # Internal state
        self._rgb: tuple[int, int, int] = _DEFAULT_RGB
        self._on: bool = False

    # ── HA state properties ──────────────────────────────────────── #

    @property
    def is_on(self) -> bool:
        return self._on

    @property
    def rgb_color(self) -> tuple[int, int, int]:
        return self._rgb

    @property
    def brightness(self) -> int:
        """Return max channel value scaled to 0-255."""
        return max(self._rgb)

    # ── Commands ─────────────────────────────────────────────────── #

    async def async_turn_on(self, **kwargs: Any) -> None:
        r, g, b = self._rgb  # start from current colour

        # If a new RGB colour was given, use it directly
        if ATTR_RGB_COLOR in kwargs:
            r, g, b = kwargs[ATTR_RGB_COLOR]

        # If only brightness was given, scale the current colour
        if ATTR_BRIGHTNESS in kwargs and ATTR_RGB_COLOR not in kwargs:
            scale = kwargs[ATTR_BRIGHTNESS] / 255
            r = round(r * scale)
            g = round(g * scale)
            b = round(b * scale)

        # Clamp
        r = max(0, min(255, r))
        g = max(0, min(255, g))
        b = max(0, min(255, b))

        # If all zeros after scaling, use default to avoid unintended off
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

    # ── Dispatcher ───────────────────────────────────────────────── #

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
            _LOGGER.warning("Zemote MOODlight: could not parse MDL value '%s'", mdl)
            return
        r, g, b = parsed
        if r == 0 and g == 0 and b == 0:
            self._on = False
        else:
            self._on  = True
            self._rgb = (r, g, b)
        self.async_write_ha_state()
