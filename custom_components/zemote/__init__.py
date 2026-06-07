from __future__ import annotations

import json
import logging
import os
import ssl
import tempfile
import time
import uuid
from typing import Any

import boto3
import certifi
import paho.mqtt.client as mqtt

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    AWS_REGION_COGNITO,
    AWS_REGION_DYNAMO,
    AWS_IOT_ENDPOINT,
    IDENTITY_POOL_ID,
    IOT_POLICY_NAME,
    PLATFORMS,
    SIGNAL_STATE_UPDATED,
    IR_REMOTE_TABLES,
    IR_POWER_ON_FUNCTIONS,
    IR_POWER_OFF_FUNCTIONS,
    IR_POWER_TOGGLE_FUNCTIONS,
)

_LOGGER = logging.getLogger(__name__)

CERT_STORAGE_KEY = f"{DOMAIN}_cert"
CERT_STORAGE_VERSION = 1

try:
    from paho.mqtt.client import CallbackAPIVersion
    _PAHO_V2 = True
except ImportError:
    _PAHO_V2 = False


async def _load_or_create_cert(hass: HomeAssistant, creds: dict, region: str) -> dict:
    store = Store(hass, CERT_STORAGE_VERSION, CERT_STORAGE_KEY)
    cert_data = await store.async_load()
    if cert_data:
        _LOGGER.debug("Zemote: loaded existing X.509 cert from storage")
        return cert_data
    _LOGGER.info("Zemote: creating new X.509 certificate via AWS IoT...")
    cert_data = await hass.async_add_executor_job(_create_cert, creds, region)
    await store.async_save(cert_data)
    _LOGGER.info("Zemote: certificate created and stored (id: %s...)", cert_data["certificateId"][:16])
    return cert_data


def _create_cert(creds: dict, region: str) -> dict:
    iot = boto3.client(
        "iot",
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretKey"],
        aws_session_token=creds["SessionToken"],
    )
    result = iot.create_keys_and_certificate(setAsActive=True)
    cert_data = {
        "certificateArn": result["certificateArn"],
        "certificateId": result["certificateId"],
        "certificatePem": result["certificatePem"],
        "privateKey": result["keyPair"]["PrivateKey"],
    }
    iot.attach_policy(policyName=IOT_POLICY_NAME, target=cert_data["certificateArn"])
    return cert_data


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    identity_id = entry.data.get("identity_id", "")
    creds = await hass.async_add_executor_job(
        _fetch_cognito_credentials, identity_id, AWS_REGION_COGNITO
    )
    cert_data = await _load_or_create_cert(hass, creds, AWS_REGION_COGNITO)

    hub = ZemoteHub(hass, entry, cert_data, creds)
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


def _fetch_cognito_credentials(identity_id: str, region: str) -> dict:
    cognito = boto3.client("cognito-identity", region_name=region)
    if not identity_id:
        identity_id = cognito.get_id(IdentityPoolId=IDENTITY_POOL_ID)["IdentityId"]
    return cognito.get_credentials_for_identity(IdentityId=identity_id)["Credentials"]


class ZemoteHub:
    """Central hub — one per config entry / Zemote account."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        cert_data: dict,
        creds: dict | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._cert_data = cert_data
        self._creds: dict = creds or {}
        self._mqtt = None
        self.devices: list[dict] = entry.data.get("devices", [])
        self.device_states: dict[str, dict] = {}

    def setup(self) -> None:
        self._connect_mqtt()
        self._subscribe_all()
        self._ping_all()

    def disconnect(self) -> None:
        if self._mqtt:
            try:
                self._mqtt.loop_stop()
                self._mqtt.disconnect()
            except Exception:
                pass
            self._mqtt = None

    def _connect_mqtt(self) -> None:
        cert_pem = self._cert_data["certificatePem"]
        key_pem = self._cert_data["privateKey"]

        cert_file = tempfile.NamedTemporaryFile(suffix=".pem", delete=False, mode="w")
        cert_file.write(cert_pem)
        cert_file.flush()
        cert_file.close()

        key_file = tempfile.NamedTemporaryFile(suffix=".key", delete=False, mode="w")
        key_file.write(key_pem)
        key_file.flush()
        key_file.close()

        try:
            if _PAHO_V2:
                client = mqtt.Client(
                    callback_api_version=CallbackAPIVersion.VERSION2,
                    client_id=str(uuid.uuid4()),
                    protocol=mqtt.MQTTv311,
                )
            else:
                client = mqtt.Client(client_id=str(uuid.uuid4()), protocol=mqtt.MQTTv311)

            client.tls_set(
                ca_certs=certifi.where(),
                certfile=cert_file.name,
                keyfile=key_file.name,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )

            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_shadow_message

            client.connect(AWS_IOT_ENDPOINT, port=8883, keepalive=60)
            client.loop_start()

            for _ in range(100):
                if client.is_connected():
                    break
                time.sleep(0.1)
            else:
                client.loop_stop()
                raise TimeoutError("Zemote: MQTT TLS connection timed out after 10s")

            self._mqtt = client
            _LOGGER.info("Zemote: connected to AWS IoT (%s) via X.509/TLS port 8883", AWS_IOT_ENDPOINT)
        finally:
            os.unlink(cert_file.name)
            os.unlink(key_file.name)

    def _on_connect(self, client, userdata, flags, rc_or_reason, properties=None) -> None:
        rc = rc_or_reason if not hasattr(rc_or_reason, "value") else rc_or_reason.value
        if rc != 0:
            _LOGGER.error("Zemote MQTT connect failed rc=%s", rc)

    def _on_disconnect(self, client, userdata, rc_or_flags, rc=None, properties=None) -> None:
        code = rc if rc is not None else rc_or_flags
        if code != 0:
            _LOGGER.warning("Zemote MQTT unexpectedly disconnected (rc=%s)", code)

    def _subscribe_all(self) -> None:
        for serial in {d["serialNumber"] for d in self.devices}:
            for suffix in ("update/accepted", "get/accepted"):
                topic = f"$aws/things/{serial}/shadow/{suffix}"
                self._mqtt.subscribe(topic, qos=0)
                _LOGGER.debug("Zemote: subscribed to %s", topic)

    def _ping_all(self) -> None:
        """Send startup pings to non-lock devices only.

        Lock devices are intentionally excluded from the shadow/get ping.
        AWS IoT would respond with a get/accepted carrying the full shadow
        state, which includes the last stored NLK response. Dispatching
        that to ZemoteLock._handle_update would trigger a spurious unlock
        on every HA restart (e.g. NLK=203 persisted in shadow -> door
        appears unlocked / auto-relock fires unnecessarily).

        Lock state is purely event-driven: it only changes when the user
        actually calls async_unlock().
        """
        lock_serials = {d["serialNumber"] for d in self.devices if d.get("platform") == "lock"}
        non_lock_serials = {
            d["serialNumber"] for d in self.devices
            if d.get("platform") != "lock"
        }
        for serial in non_lock_serials:
            self.publish(serial, {"PING": "ping"}, _bypass_check=True)
        if lock_serials:
            _LOGGER.debug(
                "Zemote: skipping shadow/get ping for lock(s) %s to prevent spurious unlocks",
                lock_serials,
            )

    def _on_shadow_message(self, client, userdata, message) -> None:
        try:
            parts = message.topic.split("/")
            serial = parts[2] if len(parts) > 2 else "unknown"
            outer = json.loads(message.payload.decode("utf-8"))
            state = outer.get("state", {})
            if isinstance(state, str):
                state = json.loads(state)

            reported = state.get("reported", {})
            if isinstance(reported, str):
                reported = json.loads(reported)

            # Skip messages that carry no reported state — these are just AWS IoT
            # echoing back our desired payload. Acting on them would corrupt state.
            if not reported:
                _LOGGER.debug("Zemote: skipping no-reported message on %s", serial)
                return

            _LOGGER.debug("Zemote shadow %s -> %s", serial, reported)
            self.device_states[serial] = {
                **self.device_states.get(serial, {}),
                **reported,
            }
            self.hass.loop.call_soon_threadsafe(
                async_dispatcher_send,
                self.hass,
                f"{SIGNAL_STATE_UPDATED}_{serial}",
                reported,
            )
        except Exception as err:
            _LOGGER.error("Zemote shadow parse error [%s]: %s", message.topic, err)

    def publish(self, serial: str, payload: dict, _bypass_check: bool = False) -> None:
        if self._mqtt is None or not self._mqtt.is_connected():
            _LOGGER.warning("Zemote: MQTT not connected, dropping publish to %s", serial)
            return
        topic = f"$aws/things/{serial}/shadow/update"
        msg = json.dumps({"state": {"desired": payload}})
        self._mqtt.publish(topic, msg, qos=0)
        _LOGGER.debug("Zemote -> %s : %s", topic, msg[:200])

    def set_channel(self, serial: str, channel_key: str, value: int | str) -> None:
        self.publish(serial, {channel_key: value})

    def get_channel_state(self, serial: str, channel_key: str) -> Any:
        return self.device_states.get(serial, {}).get(channel_key)

    async def async_send_ir_command(
        self,
        serial: str,
        sur_type: str,
        brand: str,
        codeset: str,
        want_on: bool,
    ) -> bool:
        """Fire an IR command for a SUR device via the shadow 'code' key.

        The Zemote hub firmware expects:
            {"state": {"desired": {"code": "[#%Oxxxxx]<pulse,timings,...>N"}}}

        The pre-formatted code string is stored in the device config entry
        under surCodeOn / surCodeOff (set during config_flow). If only one
        code is stored (toggle devices), the same code is used for both
        on and off.
        """
        if want_on:
            code = (
                self._device_ir_code(serial, "surCodeOn")
                or self._device_ir_code(serial, "surCode")
            )
        else:
            code = (
                self._device_ir_code(serial, "surCodeOff")
                or self._device_ir_code(serial, "surCode")
            )

        if not code:
            _LOGGER.error(
                "Zemote IR: no IR code stored for serial=%s sur_type=%s want_on=%s. "
                "Re-run config flow to re-sync devices.",
                serial, sur_type, want_on,
            )
            return False

        self.publish(serial, {"code": code})
        _LOGGER.info(
            "Zemote IR: sent %s command to serial=%s type=%s code[0:40]=%s",
            "ON" if want_on else "OFF", serial, sur_type, code[:40],
        )
        return True

    def _device_ir_code(self, serial: str, field: str) -> str | None:
        """Return the stored IR code string for a given field from device config."""
        for dev in self.devices:
            if dev.get("serialNumber") == serial:
                val = dev.get(field, "") or ""
                return val if val else None
        return None
