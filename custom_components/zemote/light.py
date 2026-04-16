"""Light platform for Zemote integration."""
from __future__ import annotations

import colorsys
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ZemoteHub
from .const import DOMAIN, SIGNAL_STATE_UPDATED


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    hub: ZemoteHub = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for d in hub.devices:
        if d.get("platform") != "light":
            continue
        if d.get("isRgb"):
            entities.append(ZemoteRgbLight(hub, d))
        else:
            entities.append(ZemoteLight(hub, d))
    async_add_entities(entities)


def _device_info(device: dict, serial: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=device.get("hubName", serial),
        manufacturer="Contera IoT",
        model="Zemote Hub",
        suggested_area=device.get("roomName") or None,
    )


class ZemoteLight(LightEntity):
    """Zemote dimmable or on/off light."""

    def __init__(self, hub: ZemoteHub, device: dict) -> None:
        self._hub        = hub
        self._device     = device
        self._serial     = device["serialNumber"]
        self._channel    = device["channelKey"]
        self._attr_name  = device["name"]
        self._attr_unique_id = device["applianceId"]
        self._dimmable   = device.get("dimmable", False)
        self._state      = False
        self._brightness = 255
        if self._dimmable:
            self._attr_color_mode            = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        else:
            self._attr_color_mode            = ColorMode.ONOFF
            self._attr_supported_color_modes = {ColorMode.ONOFF}

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._device, self._serial)

    @property
    def is_on(self) -> bool:
        return self._state

    @property
    def brightness(self) -> int | None:
        return self._brightness if self._dimmable else None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_STATE_UPDATED}_{self._serial}",
                self._handle_state_update,
            )
        )
        val = self._hub.get_channel_state(self._serial, self._channel)
        if val is not None:
            self._apply_value(int(val))

    @callback
    def _handle_state_update(self, reported: dict) -> None:
        raw = reported.get(self._channel)
        if raw is not None:
            self._apply_value(int(raw))
            self.async_write_ha_state()

    def _apply_value(self, value: int) -> None:
        if value == 0:
            self._state = False
            self._brightness = 0
        else:
            self._state = True
            self._brightness = min(255, round(value * 255 / 100))

    def turn_on(self, **kwargs: Any) -> None:
        pct = max(1, round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)) if (ATTR_BRIGHTNESS in kwargs and self._dimmable) else 100
        self._hub.set_channel(self._serial, self._channel, pct)
        self._apply_value(pct)
        self.schedule_update_ha_state()

    def turn_off(self, **kwargs: Any) -> None:
        self._hub.set_channel(self._serial, self._channel, 0)
        self._apply_value(0)
        self.schedule_update_ha_state()


class ZemoteRgbLight(LightEntity):
    """Zemote RGB light."""

    _attr_color_mode            = ColorMode.HS
    _attr_supported_color_modes = {ColorMode.HS}

    def __init__(self, hub: ZemoteHub, device: dict) -> None:
        self._hub        = hub
        self._device     = device
        self._serial     = device["serialNumber"]
        self._channel    = device["channelKey"]
        self._attr_name  = device["name"]
        self._attr_unique_id = device["applianceId"]
        self._state      = False
        self._brightness = 255
        self._hs_color: tuple[float, float] = (0, 0)

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._device, self._serial)

    @property
    def is_on(self) -> bool:
        return self._state

    @property
    def brightness(self) -> int:
        return self._brightness

    @property
    def hs_color(self) -> tuple[float, float]:
        return self._hs_color

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_STATE_UPDATED}_{self._serial}",
                self._handle_state_update,
            )
        )

    @callback
    def _handle_state_update(self, reported: dict) -> None:
        r = reported.get("R")
        g = reported.get("G")
        b = reported.get("B")
        if r is not None and g is not None and b is not None:
            r, g, b = int(r), int(g), int(b)
            self._state = any([r, g, b])
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            self._hs_color  = (h * 360, s * 100)
            self._brightness = round(v * 255)
            self.async_write_ha_state()

    def turn_on(self, **kwargs: Any) -> None:
        hs  = kwargs.get(ATTR_HS_COLOR, self._hs_color)
        bri = kwargs.get(ATTR_BRIGHTNESS, self._brightness)
        h, s = hs[0] / 360, hs[1] / 100
        v    = bri / 255
        r, g, b = [round(c * 255) for c in colorsys.hsv_to_rgb(h, s, v)]
        self._state = True
        self._hs_color  = (hs[0], hs[1])
        self._brightness = bri
        self._hub.publish(self._serial, {"R": r, "G": g, "B": b})
        self.schedule_update_ha_state()

    def turn_off(self, **kwargs: Any) -> None:
        self._state = False
        self._hub.publish(self._serial, {"R": 0, "G": 0, "B": 0})
        self.schedule_update_ha_state()
