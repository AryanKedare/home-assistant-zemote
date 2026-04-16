"""Light platform for Zemote integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
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
    async_add_entities([
        ZemoteLight(hub, d) for d in hub.devices if d.get("platform") == "light"
    ])


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
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=self._device.get("hubName", self._serial),
            manufacturer="Contera IoT",
            model="Zemote Hub",
        )

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
        if ATTR_BRIGHTNESS in kwargs and self._dimmable:
            pct = max(1, round(kwargs[ATTR_BRIGHTNESS] * 100 / 255))
        else:
            pct = 100
        self._hub.set_channel(self._serial, self._channel, pct)
        self._apply_value(pct)
        self.schedule_update_ha_state()

    def turn_off(self, **kwargs: Any) -> None:
        self._hub.set_channel(self._serial, self._channel, 0)
        self._apply_value(0)
        self.schedule_update_ha_state()
