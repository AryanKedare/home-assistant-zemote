"""Zemote Home Automation - Home Assistant integration."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import paho.mqtt.client as mqtt

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, PLATFORMS, SIGNAL_STATE_UPDATED

_LOGGER = logging.getLogger(__name__)

try:
    from paho.mqtt.client import CallbackAPIVersion
    _PAHO_V2 = True
except ImportError:
    _PAHO_V2 = False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = ZemoteHub(hass, entry)
    try:
        await hass.async_add_executor_job(hub.setup)
    except Exception as err:
        raise ConfigEntryNotReady(f"Zemote setup failed: {err}") from err
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub: ZemoteHub = hass.data[DOMAIN][entry.entry_id]
    hub.disconnect()
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return ok


class ZemoteHub:
    """Central hub — one per config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass         = hass
        self.entry        = entry
        self._mqtt        = None
        self.devices: list[dict]            = entry.data.get("devices", [])
        self.device_states: dict[str, dict] = {}

    def setup(self) -> None:
        self._connect_mqtt()
        self._subscribe_all()

    def disconnect(self) -> None:
        if self._mqtt:
            try:
                self._mqtt.loop_stop()
                self._mqtt.disconnect()
            except Exception:
                pass
            self._mqtt = None

    def _connect_mqtt(self) -> None:
        if _PAHO_V2:
            client = mqtt.Client(
                callback_api_version=CallbackAPIVersion.VERSION2,
                client_id=str(uuid.uuid4()),
                protocol=mqtt.MQTTv311,
            )
        else:
            client = mqtt.Client(client_id=str(uuid.uuid4()), protocol=mqtt.MQTTv311)
        client.on_connect    = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message    = self._on_shadow_message
        self._mqtt = client

    def _on_connect(self, client, userdata, flags, rc_or_reason, properties=None) -> None:
        rc = rc_or_reason if not hasattr(rc_or_reason, "value") else rc_or_reason.value
        if rc != 0:
            _LOGGER.error("Zemote MQTT connect failed rc=%s", rc)

    def _on_disconnect(self, client, userdata, rc_or_flags, rc=None, properties=None) -> None:
        code = rc if rc is not None else rc_or_flags
        if code != 0:
            _LOGGER.warning("Zemote MQTT disconnected unexpectedly (rc=%s)", code)

    def _subscribe_all(self) -> None:
        for serial in {d["serialNumber"] for d in self.devices}:
            if self._mqtt:
                self._mqtt.subscribe(f"$aws/things/{serial}/shadow/update/accepted", qos=0)

    def _on_shadow_message(self, client, userdata, message) -> None:
        try:
            parts    = message.topic.split("/")
            serial   = parts[2] if len(parts) > 2 else "unknown"
            outer    = json.loads(message.payload.decode("utf-8"))
            state    = outer.get("state", {})
            reported = state.get("reported", {}) if isinstance(state, dict) else {}
            self.device_states[serial] = {**self.device_states.get(serial, {}), **reported}
            self.hass.loop.call_soon_threadsafe(
                async_dispatcher_send, self.hass,
                f"{SIGNAL_STATE_UPDATED}_{serial}", reported,
            )
        except Exception as err:
            _LOGGER.error("Zemote shadow parse error [%s]: %s", message.topic, err)

    def publish(self, serial: str, payload: dict, _bypass_check: bool = False) -> None:
        if not _bypass_check and (self._mqtt is None or not self._mqtt.is_connected()):
            _LOGGER.warning("Zemote: MQTT not connected, dropping publish to %s", serial)
            return
        if self._mqtt:
            self._mqtt.publish(
                f"$aws/things/{serial}/shadow/update",
                json.dumps({"state": {"desired": payload}}),
                qos=0,
            )

    def set_channel(self, serial: str, channel_key: str, value: int | str) -> None:
        self.publish(serial, {channel_key: value})

    def get_channel_state(self, serial: str, channel_key: str) -> Any:
        return self.device_states.get(serial, {}).get(channel_key)
