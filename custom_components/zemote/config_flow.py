"""Config flow for Zemote integration."""
from __future__ import annotations

import logging
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
)

_LOGGER = logging.getLogger(__name__)

TABLE_VERIFICATION = "Verification"
STEP_USER_SCHEMA = vol.Schema({
    vol.Required("email"): str,
    vol.Required("password"): str,
})


def _strip_module_token(name: str, hub_name: str) -> str:
    """Strip the module identifier token (last word of hub_name) from the
    start of name if present.

    e.g. hub_name='Child Bedroom SB3', name='SB3 Bar' -> 'Bar'
         hub_name='Children SB2',      name='SB2 Fan' -> 'Fan'
         hub_name='Child Bedroom 1595', name='1595 Entrance' -> 'Entrance'
    """
    if not hub_name:
        return name
    token = hub_name.split()[-1]  # last word of hub name, e.g. 'SB3', '1595'
    if name.lower().startswith(token.lower()):
        stripped = name[len(token):].strip()
        if stripped:  # don't return empty string
            return stripped
    return name


def _prefixed_name(room: str, name: str, hub_name: str = "") -> str:
    clean = _strip_module_token(name, hub_name)
    if room:
        return f"{room} {clean}"
    return clean


class ZemoteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Zemote config flow."""

    VERSION = CONFIG_VERSION

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input["email"].strip()
            password = user_input["password"]

            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            try:
                auth_ok = await self.hass.async_add_executor_job(
                    _verify_login, email, password
                )
            except Exception as err:  # noqa: BLE001
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
                    except Exception as err:  # noqa: BLE001
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
    cognito = boto3.client("cognito-identity", region_name=AWS_REGION_COGNITO)
    identity_id = cognito.get_id(IdentityPoolId=IDENTITY_POOL_ID)["IdentityId"]
    creds = cognito.get_credentials_for_identity(IdentityId=identity_id)["Credentials"]

    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretKey"],
        aws_session_token=creds["SessionToken"],
        region_name=AWS_REGION_DYNAMO,
    )
    dynamo = session.resource("dynamodb", region_name=AWS_REGION_DYNAMO)
    table = dynamo.Table(TABLE_VERIFICATION)

    resp = table.get_item(Key={"email": email})
    item = resp.get("Item")
    if not item:
        _LOGGER.warning("Zemote: no Verification record for %s", email)
        return False

    stored_password = item.get("password", "")
    if stored_password != password:
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
    items = []
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
    cognito = boto3.client("cognito-identity", region_name=AWS_REGION_COGNITO)
    identity_id = cognito.get_id(IdentityPoolId=IDENTITY_POOL_ID)["IdentityId"]
    creds = cognito.get_credentials_for_identity(IdentityId=identity_id)["Credentials"]

    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretKey"],
        aws_session_token=creds["SessionToken"],
        region_name=AWS_REGION_DYNAMO,
    )
    dynamo = session.resource("dynamodb", region_name=AWS_REGION_DYNAMO)

    master_resp = dynamo.Table(TABLE_MASTER).query(
        KeyConditionExpression=Key("email").eq(email)
    )
    masters = master_resp.get("Items", [])
    serial_to_master: dict[str, dict] = {
        h["serialNumber"]: h for h in masters if "serialNumber" in h
    }

    module_resp = dynamo.Table(TABLE_MODULE_DATA).query(
        KeyConditionExpression=Key("email").eq(email)
    )
    modules = module_resp.get("Items", [])

    rooms = _scan_all(dynamo.Table(TABLE_ROOM), Attr("email").eq(email))
    _LOGGER.debug("Zemote: fetched %d rooms for %s", len(rooms), email)

    appliance_room: dict[str, str] = {}
    room_list: list[dict] = []
    for r in rooms:
        room_name = r.get("name", "")
        room_id = str(r.get("roomId", ""))
        if not room_name or not room_id:
            continue
        room_list.append({"roomId": room_id, "name": room_name})
        raw_devices = r.get("roomDevices", "")
        for aid in raw_devices.split(","):
            aid = aid.strip()
            if aid and aid not in appliance_room:
                appliance_room[aid] = room_name

    _LOGGER.info("Zemote: built appliance->room map with %d entries", len(appliance_room))

    devices: list[dict] = []
    seen_ids: set[str] = set()

    for mod in modules:
        serial = mod.get("serialNumber", "")
        appliance_id = mod.get("applianceId", "")
        hub = serial_to_master.get(serial, {})
        hub_name = hub.get("deviceName", serial)

        for item in mod.get("lfmData") or []:
            sub_id = item.get("applianceId", "")
            sub_type = item.get("type", "")
            raw_name = item.get("name") or sub_id
            dimmable_status = item.get("dimmableStatus", "")
            if not sub_id or not sub_type or sub_type.upper() == DIMMER_NA or sub_id in seen_ids:
                continue
            seen_ids.add(sub_id)
            platform, dimmable = _classify_lfm(sub_type, dimmable_status)
            room = appliance_room.get(sub_id, "")
            devices.append({
                "applianceId": sub_id,
                "moduleId": appliance_id,
                "serialNumber": serial,
                "name": _prefixed_name(room, raw_name, hub_name),
                "channelKey": sub_type,
                "dimmable": dimmable,
                "platform": platform,
                "hubName": hub_name,
                "roomName": room,
            })

        for item in mod.get("powerModuleData") or []:
            sub_id = item.get("applianceId", "")
            sub_type = item.get("type", "")
            raw_name = item.get("name") or sub_id
            if not sub_id or not sub_type or sub_type.upper() == DIMMER_NA or sub_id in seen_ids:
                continue
            seen_ids.add(sub_id)
            room = appliance_room.get(sub_id, "")
            devices.append({
                "applianceId": sub_id,
                "moduleId": appliance_id,
                "serialNumber": serial,
                "name": _prefixed_name(room, raw_name, hub_name),
                "channelKey": sub_type,
                "dimmable": False,
                "platform": "switch",
                "hubName": hub_name,
                "roomName": room,
            })

        for item in mod.get("curtainData") or []:
            sub_id = item.get("applianceId", "")
            sub_type = item.get("type", "")
            raw_name = item.get("name") or sub_id
            if not sub_id or not sub_type or sub_type.upper() == DIMMER_NA or sub_id in seen_ids:
                continue
            seen_ids.add(sub_id)
            room = appliance_room.get(sub_id, "")
            devices.append({
                "applianceId": sub_id,
                "moduleId": appliance_id,
                "serialNumber": serial,
                "name": _prefixed_name(room, raw_name, hub_name),
                "channelKey": sub_type,
                "dimmable": False,
                "platform": "cover",
                "hubName": hub_name,
                "roomName": room,
            })

        sur = mod.get("surData")
        if sur:
            sur_type = sur.get("type", "").upper()
            sur_id = sur.get("applianceId") or appliance_id
            raw_name = sur.get("name") or sur_id
            if sur_type and sur_type != DIMMER_NA and raw_name != DIMMER_NA:
                if sur_id and sur_id not in seen_ids:
                    seen_ids.add(sur_id)
                    room = appliance_room.get(sur_id, "")
                    devices.append({
                        "applianceId": sur_id,
                        "moduleId": appliance_id,
                        "serialNumber": serial,
                        "name": _prefixed_name(room, raw_name, hub_name),
                        "channelKey": sur_type,
                        "dimmable": False,
                        "platform": SUR_TYPE_MAP.get(sur_type, "switch"),
                        "hubName": hub_name,
                        "roomName": room,
                        "surBrand": sur.get("brand", ""),
                        "surCodeset": sur.get("codeset", ""),
                    })

        rgb = mod.get("rgbData")
        if rgb:
            rgb_id = rgb.get("applianceId") or appliance_id
            raw_name = rgb.get("name") or rgb_id
            if rgb_id and rgb_id not in seen_ids:
                seen_ids.add(rgb_id)
                room = appliance_room.get(rgb_id, "")
                devices.append({
                    "applianceId": rgb_id,
                    "moduleId": appliance_id,
                    "serialNumber": serial,
                    "name": _prefixed_name(room, raw_name, hub_name),
                    "channelKey": rgb.get("type", "RGB"),
                    "dimmable": True,
                    "platform": "light",
                    "hubName": hub_name,
                    "roomName": room,
                    "isRgb": True,
                })

    for master in masters:
        serial = str(master.get("serialNumber", ""))
        if "slm" not in serial.lower():
            continue
        appliance_id = master.get("applianceId", "") or serial_to_master.get(serial, {}).get("applianceId", "")
        if not appliance_id:
            module = next((m for m in modules if m.get("serialNumber") == serial), None)
            appliance_id = module.get("applianceId", "") if module else ""
        if not appliance_id or appliance_id in seen_ids:
            continue
        seen_ids.add(appliance_id)
        room = appliance_room.get(appliance_id, "Other") or "Other"
        raw_name = master.get("deviceName") or appliance_id or serial
        devices.append({
            "applianceId": appliance_id,
            "moduleId": appliance_id,
            "serialNumber": serial,
            "name": _prefixed_name(room, raw_name),
            "channelKey": "LOCK",
            "dimmable": False,
            "platform": "lock",
            "hubName": raw_name,
            "roomName": room,
            "isLockModule": True,
        })

    _LOGGER.info("Zemote: discovered %d devices for %s", len(devices), email)
    return {
        "identity_id": identity_id,
        "devices": devices,
        "rooms": room_list,
    }
