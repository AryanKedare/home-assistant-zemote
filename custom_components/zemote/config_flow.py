"""Config flow for Zemote integration."""
from __future__ import annotations

import logging
import re
import subprocess
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    CONFIG_VERSION,
    AWS_REGION_COGNITO,
    AWS_REGION_DYNAMO,
    IDENTITY_POOL_ID,
    TABLE_MASTER,
    TABLE_MODULE_DATA,
    TABLE_ROOM,
    DIMMER_NA,
    DIMMER_YES,
    FAN_TYPE_PREFIXES,
    LIGHT_TYPE_PREFIXES,
    SUR_TYPE_MAP,
    MOODLIGHT_SERIAL_PREFIX,
    IR_REMOTE_TABLES,
    IR_POWER_ON_FUNCTIONS,
    IR_POWER_OFF_FUNCTIONS,
    IR_POWER_TOGGLE_FUNCTIONS,
)

_LOGGER = logging.getLogger(__name__)

TABLE_VERIFICATION = "Verification"
STEP_USER_SCHEMA = vol.Schema({
    vol.Required("email"): str,
    vol.Required("password"): str,
})

_ANDROID_ID_RE = re.compile(r'^[0-9a-f]{16}$', re.IGNORECASE)


def _sync_clock() -> None:
    for cmd in (
        ["ntpdate", "-u", "pool.ntp.org"],
        ["chronyc", "makestep"],
        ["timedatectl", "set-ntp", "true"],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            if result.returncode == 0:
                _LOGGER.debug("Zemote: clock synced via %s", cmd[0])
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    _LOGGER.warning(
        "Zemote: could not sync system clock — AWS calls may fail if clock "
        "skew exceeds 5 minutes."
    )


def _strip_module_prefix(name: str, hub_name: str) -> str:
    stripped = name.strip()
    prefix = hub_name.strip() if hub_name else ""
    if prefix and stripped.lower().startswith(prefix.lower()):
        stripped = stripped[len(prefix):].strip()
    return stripped if stripped else name.strip()


def _pick_lock_device_id(module: dict) -> str | None:
    lock_data = module.get("lockData") or []
    if not isinstance(lock_data, list):
        return None
    for entry in lock_data:
        did = str(entry.get("deviceId", "")).strip()
        if _ANDROID_ID_RE.match(did):
            return did
    if lock_data:
        return str(lock_data[0].get("deviceId", "")).strip() or None
    return None


def _fetch_ir_codes(
    dynamo,
    sur_type: str,
    brand: str,
    codeset: str,
) -> tuple[str | None, str | None]:
    """Fetch ON and OFF IR code strings from the remote data DynamoDB table.

    Returns (on_code, off_code). Either may be None if not found.
    If only a toggle code exists, returns (toggle_code, toggle_code).
    """
    table_name = IR_REMOTE_TABLES.get(sur_type.upper())
    if not table_name:
        _LOGGER.debug("Zemote IR: no remote table for sur_type=%s", sur_type)
        return None, None

    try:
        table = dynamo.Table(table_name)
        # Try primary key query first
        try:
            resp  = table.query(
                KeyConditionExpression=Key("brand").eq(brand) & Key("codeset").eq(codeset)
            )
            items = resp.get("Items", [])
        except Exception:
            items = []

        if not items:
            resp  = table.scan(
                FilterExpression=Attr("brand").eq(brand) & Attr("codeset").eq(codeset)
            )
            items = resp.get("Items", [])

        if not items:
            _LOGGER.warning(
                "Zemote IR: no data in %s for brand=%s codeset=%s",
                table_name, brand, codeset,
            )
            return None, None

        # Flatten codesetData across all items
        codeset_data: list[dict] = []
        for item in items:
            cd = item.get("codesetData") or []
            if isinstance(cd, list):
                codeset_data.extend(cd)

        def _find(keywords: list[str]) -> str | None:
            for kw in keywords:
                for entry in codeset_data:
                    fn  = str(entry.get("function", "")).lower().strip()
                    raw = str(entry.get("rawData",   "")).strip().rstrip(",")
                    if kw in fn and raw:
                        return raw
            return None

        on_code  = _find(IR_POWER_ON_FUNCTIONS)
        off_code = _find(IR_POWER_OFF_FUNCTIONS)
        toggle   = _find(IR_POWER_TOGGLE_FUNCTIONS)

        # If no separate on/off found, use toggle for both
        if not on_code:  on_code  = toggle
        if not off_code: off_code = toggle

        if not on_code and not off_code:
            fns = [str(e.get("function", "")) for e in codeset_data]
            _LOGGER.warning(
                "Zemote IR: could not match power functions in %s brand=%s codeset=%s. "
                "Available: %s", table_name, brand, codeset, fns,
            )

        return on_code, off_code

    except Exception as err:
        _LOGGER.warning("Zemote IR: error fetching from %s: %s", table_name, err)
        return None, None


class ZemoteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Zemote config flow."""

    VERSION = CONFIG_VERSION

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            email    = user_input["email"].strip()
            password = user_input["password"]

            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            try:
                auth_ok = await self.hass.async_add_executor_job(
                    _verify_login, email, password
                )
            except Exception as err:
                _LOGGER.exception("Zemote auth error: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if not auth_ok:
                    errors["base"] = "invalid_auth"
                else:
                    try:
                        data = await self.hass.async_add_executor_job(
                            _fetch_account_data, email
                        )
                    except Exception as err:
                        _LOGGER.exception("Zemote config flow error: %s", err)
                        errors["base"] = "cannot_connect"
                    else:
                        if not data.get("devices"):
                            errors["base"] = "no_devices"
                        else:
                            return self.async_create_entry(
                                title=email,
                                data={"email": email, **data},
                            )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )


def _verify_login(email: str, password: str) -> bool:
    _sync_clock()

    cognito     = boto3.client("cognito-identity", region_name=AWS_REGION_COGNITO)
    identity_id = cognito.get_id(IdentityPoolId=IDENTITY_POOL_ID)["IdentityId"]
    creds       = cognito.get_credentials_for_identity(IdentityId=identity_id)["Credentials"]

    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretKey"],
        aws_session_token=creds["SessionToken"],
        region_name=AWS_REGION_DYNAMO,
    )
    dynamo = session.resource("dynamodb", region_name=AWS_REGION_DYNAMO)
    table  = dynamo.Table(TABLE_VERIFICATION)

    resp = table.get_item(Key={"email": email})
    item = resp.get("Item")
    if not item:
        _LOGGER.warning("Zemote: no Verification record for %s", email)
        return False

    if item.get("password", "") != password:
        _LOGGER.warning("Zemote: password mismatch for %s", email)
        return False

    _LOGGER.info("Zemote: login verified for %s", email)
    return True


def _classify_lfm(sub_type: str, dimmable_status: str) -> tuple[str, bool]:
    t = sub_type.upper()
    if t.startswith(FAN_TYPE_PREFIXES):
        return "fan", False
    if t.startswith(LIGHT_TYPE_PREFIXES):
        dimmable = dimmable_status == DIMMER_YES
        return "light", dimmable
    return "switch", False


def _scan_all(table, filter_expr) -> list[dict]:
    items  = []
    kwargs = {"FilterExpression": filter_expr}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def _fetch_account_data(email: str) -> dict:
    cognito     = boto3.client("cognito-identity", region_name=AWS_REGION_COGNITO)
    identity_id = cognito.get_id(IdentityPoolId=IDENTITY_POOL_ID)["IdentityId"]
    creds       = cognito.get_credentials_for_identity(IdentityId=identity_id)["Credentials"]

    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretKey"],
        aws_session_token=creds["SessionToken"],
        region_name=AWS_REGION_DYNAMO,
    )
    dynamo = session.resource("dynamodb", region_name=AWS_REGION_DYNAMO)

    master_resp      = dynamo.Table(TABLE_MASTER).query(
        KeyConditionExpression=Key("email").eq(email)
    )
    masters          = master_resp.get("Items", [])
    serial_to_master = {h["serialNumber"]: h for h in masters if "serialNumber" in h}

    module_resp = dynamo.Table(TABLE_MODULE_DATA).query(
        KeyConditionExpression=Key("email").eq(email)
    )
    modules = module_resp.get("Items", [])

    rooms = _scan_all(dynamo.Table(TABLE_ROOM), Attr("email").eq(email))

    appliance_room: dict[str, str] = {}
    room_list: list[dict] = []
    for r in rooms:
        room_name = r.get("name", "")
        room_id   = str(r.get("roomId", ""))
        if not room_name or not room_id:
            continue
        room_list.append({"roomId": room_id, "name": room_name})
        raw_devices = r.get("roomDevices", "")
        for aid in raw_devices.split(","):
            aid = aid.strip()
            if aid and aid not in appliance_room:
                appliance_room[aid] = room_name

    devices:  list[dict] = []
    seen_ids: set[str]   = set()

    for mod in modules:
        serial       = mod.get("serialNumber", "")
        appliance_id = mod.get("applianceId", "")
        hub          = serial_to_master.get(serial, {})
        hub_name     = hub.get("deviceName", serial)

        for item in mod.get("lfmData") or []:
            sub_id          = item.get("applianceId", "")
            sub_type        = item.get("type", "")
            raw_name        = _strip_module_prefix(item.get("name") or sub_id, hub_name)
            dimmable_status = item.get("dimmableStatus", "")
            if not sub_id or not sub_type or sub_type.upper() == DIMMER_NA or sub_id in seen_ids:
                continue
            seen_ids.add(sub_id)
            platform, dimmable = _classify_lfm(sub_type, dimmable_status)
            room = appliance_room.get(sub_id, "")
            devices.append({
                "applianceId": sub_id,
                "moduleId":    appliance_id,
                "serialNumber": serial,
                "name":        raw_name,
                "channelKey":  sub_type,
                "dimmable":    dimmable,
                "platform":    platform,
                "hubName":     hub_name,
                "roomName":    room,
            })

        for item in mod.get("powerModuleData") or []:
            sub_id   = item.get("applianceId", "")
            sub_type = item.get("type", "")
            raw_name = _strip_module_prefix(item.get("name") or sub_id, hub_name)
            if not sub_id or not sub_type or sub_type.upper() == DIMMER_NA or sub_id in seen_ids:
                continue
            seen_ids.add(sub_id)
            room = appliance_room.get(sub_id, "")
            devices.append({
                "applianceId": sub_id,
                "moduleId":    appliance_id,
                "serialNumber": serial,
                "name":        raw_name,
                "channelKey":  sub_type,
                "dimmable":    False,
                "platform":    "switch",
                "hubName":     hub_name,
                "roomName":    room,
            })

        for item in mod.get("curtainData") or []:
            sub_id   = item.get("applianceId", "")
            sub_type = item.get("type", "")
            raw_name = _strip_module_prefix(item.get("name") or sub_id, hub_name)
            if not sub_id or not sub_type or sub_type.upper() == DIMMER_NA or sub_id in seen_ids:
                continue
            seen_ids.add(sub_id)
            room = appliance_room.get(sub_id, "")
            devices.append({
                "applianceId": sub_id,
                "moduleId":    appliance_id,
                "serialNumber": serial,
                "name":        raw_name,
                "channelKey":  sub_type,
                "dimmable":    False,
                "platform":    "cover",
                "hubName":     hub_name,
                "roomName":    room,
            })

        sur = mod.get("surData")
        if sur:
            sur_type = sur.get("type", "").upper()
            sur_name = sur.get("name", "")
            sur_id   = appliance_id
            raw_name = _strip_module_prefix(sur_name or sur_id, hub_name)

            if sur_type and sur_type != DIMMER_NA and sur_name != DIMMER_NA:
                if sur_id and sur_id not in seen_ids:
                    seen_ids.add(sur_id)
                    room     = appliance_room.get(sur_id, "")
                    platform = SUR_TYPE_MAP.get(sur_type, "switch")
                    brand    = sur.get("brand", "") or ""
                    codeset  = sur.get("codeset", "") or ""

                    entry: dict = {
                        "applianceId":  sur_id,
                        "moduleId":     sur_id,
                        "serialNumber": serial,
                        "name":         raw_name or sur_type,
                        "channelKey":   sur_type,
                        "dimmable":     False,
                        "platform":     platform,
                        "hubName":      hub_name,
                        "roomName":     room,
                        "surBrand":     brand,
                        "surCodeset":   codeset,
                        "surCodeOn":    None,
                        "surCodeOff":   None,
                    }

                    # Fetch and store IR codes at config time so runtime
                    # publish needs no DynamoDB call
                    if platform != "moodlight" and brand and codeset:
                        on_code, off_code = _fetch_ir_codes(
                            dynamo, sur_type, brand, codeset
                        )
                        entry["surCodeOn"]  = on_code
                        entry["surCodeOff"] = off_code
                        if on_code:
                            _LOGGER.info(
                                "Zemote IR: stored codes for %s %s brand=%s codeset=%s on=%s off=%s",
                                sur_type, sur_id, brand, codeset,
                                bool(on_code), bool(off_code),
                            )
                        else:
                            _LOGGER.warning(
                                "Zemote IR: could not fetch IR codes for %s %s brand=%s codeset=%s",
                                sur_type, sur_id, brand, codeset,
                            )

                    if platform == "moodlight":
                        entry["channelKey"] = "MDL"
                        _LOGGER.info(
                            "Zemote: MOODlight applianceId=%s serial=%s",
                            sur_id, serial,
                        )

                    devices.append(entry)

        rgb = mod.get("rgbData")
        if rgb:
            rgb_id   = rgb.get("applianceId") or appliance_id
            raw_name = _strip_module_prefix(rgb.get("name") or rgb_id, hub_name)
            if rgb_id and rgb_id not in seen_ids:
                seen_ids.add(rgb_id)
                room = appliance_room.get(rgb_id, "")
                devices.append({
                    "applianceId":  rgb_id,
                    "moduleId":     appliance_id,
                    "serialNumber": serial,
                    "name":         raw_name,
                    "channelKey":   rgb.get("type", "RGB"),
                    "dimmable":     True,
                    "platform":     "light",
                    "hubName":      hub_name,
                    "roomName":     room,
                    "isRgb":        True,
                })

    for master in masters:
        serial = str(master.get("serialNumber", ""))
        if "slm" not in serial.lower():
            continue
        appliance_id = (
            master.get("applianceId", "")
            or serial_to_master.get(serial, {}).get("applianceId", "")
        )
        if not appliance_id:
            module       = next((m for m in modules if m.get("serialNumber") == serial), None)
            appliance_id = module.get("applianceId", "") if module else ""
        if not appliance_id or appliance_id in seen_ids:
            continue

        lock_module = next((m for m in modules if m.get("serialNumber") == serial), None)
        device_id   = _pick_lock_device_id(lock_module or {})
        if device_id:
            _LOGGER.info("Zemote config_flow: lock %s deviceId=%s", serial, device_id)
        else:
            _LOGGER.warning("Zemote config_flow: lock %s — no deviceId found in lockData", serial)

        seen_ids.add(appliance_id)
        room     = appliance_room.get(appliance_id, "Other") or "Other"
        raw_name = master.get("deviceName") or appliance_id or serial
        devices.append({
            "applianceId":  appliance_id,
            "moduleId":     appliance_id,
            "serialNumber": serial,
            "name":         raw_name,
            "channelKey":   "LOCK",
            "dimmable":     False,
            "platform":     "lock",
            "hubName":      raw_name,
            "roomName":     room,
            "isLockModule": True,
            "deviceId":     device_id,
        })

    _LOGGER.info("Zemote: discovered %d devices for %s", len(devices), email)
    return {
        "identity_id": identity_id,
        "devices":     devices,
        "rooms":       room_list,
    }
