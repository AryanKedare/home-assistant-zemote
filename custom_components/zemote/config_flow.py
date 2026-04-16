"""Config flow for Zemote integration."""
from __future__ import annotations

import logging
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
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

STEP_USER_SCHEMA = vol.Schema({
    vol.Required("email"):    str,
    vol.Required("password"): str,
})


class ZemoteConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = CONFIG_VERSION

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ZemoteOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email    = user_input["email"].strip()
            password = user_input["password"]
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()
            try:
                auth_ok = await self.hass.async_add_executor_job(_verify_login, email, password)
            except Exception as err:
                _LOGGER.exception("Zemote auth error: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if not auth_ok:
                    errors["base"] = "invalid_auth"
                else:
                    try:
                        data = await self.hass.async_add_executor_job(_fetch_account_data, email)
                    except Exception as err:
                        _LOGGER.exception("Zemote config flow error: %s", err)
                        errors["base"] = "cannot_connect"
                    else:
                        if not data.get("devices"):
                            errors["base"] = "no_devices"
                        else:
                            return self.async_create_entry(title=email, data={"email": email, **data})
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)


class ZemoteOptionsFlow(config_entries.OptionsFlow):
    """Allow re-scanning devices from the options UI."""

    def __init__(self, config_entry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            email = self._config_entry.data.get("email", "")
            try:
                data = await self.hass.async_add_executor_job(_fetch_account_data, email)
            except Exception as err:
                _LOGGER.exception("Zemote options rescan error: %s", err)
                return self.async_abort(reason="cannot_connect")
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={**self._config_entry.data, **data},
            )
            return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({}),
            description_placeholders={"info": "Click Submit to re-scan devices."},
        )


def _verify_login(email: str, password: str) -> bool:
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
    resp   = dynamo.Table("Verification").get_item(Key={"email": email})
    item   = resp.get("Item")
    return bool(item and item.get("password", "") == password)


def _classify_lfm(sub_type: str, dimmable_status: str) -> tuple[str, bool]:
    t = sub_type.upper()
    if t.startswith(FAN_TYPE_PREFIXES):
        return "fan", False
    if t.startswith(LIGHT_TYPE_PREFIXES):
        return "light", dimmable_status == DIMMER_YES
    return "switch", False


def _prefixed_name(room: str, name: str) -> str:
    return f"{room} {name}" if room else name


def _scan_all(table, filter_expr) -> list[dict]:
    items, kwargs = [], {"FilterExpression": filter_expr}
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
    hubs = dynamo.Table(TABLE_MASTER).query(KeyConditionExpression=Key("email").eq(email)).get("Items", [])
    serial_to_hub = {h["serialNumber"]: h for h in hubs if "serialNumber" in h}
    modules = dynamo.Table(TABLE_MODULE_DATA).query(KeyConditionExpression=Key("email").eq(email)).get("Items", [])
    rooms = _scan_all(dynamo.Table(TABLE_ROOM), Attr("email").eq(email))
    appliance_room: dict[str, str] = {}
    room_list: list[dict] = []
    for r in rooms:
        room_name = r.get("name", "")
        room_id   = str(r.get("roomId", ""))
        if not room_name or not room_id:
            continue
        room_list.append({"roomId": room_id, "name": room_name})
        for aid in r.get("roomDevices", "").split(","):
            aid = aid.strip()
            if aid and aid not in appliance_room:
                appliance_room[aid] = room_name
    devices: list[dict] = []
    seen_ids: set[str]  = set()
    for mod in modules:
        serial       = mod.get("serialNumber", "")
        appliance_id = mod.get("applianceId", "")
        hub_name     = serial_to_hub.get(serial, {}).get("deviceName", serial)
        for item in mod.get("lfmData") or []:
            sub_id = item.get("applianceId", "")
            sub_type = item.get("type", "")
            if not sub_id or not sub_type or sub_type.upper() == DIMMER_NA or sub_id in seen_ids:
                continue
            seen_ids.add(sub_id)
            platform, dimmable = _classify_lfm(sub_type, item.get("dimmableStatus", ""))
            room = appliance_room.get(sub_id, "")
            devices.append({"applianceId": sub_id, "moduleId": appliance_id, "serialNumber": serial,
                            "name": _prefixed_name(room, item.get("name") or sub_id), "channelKey": sub_type,
                            "dimmable": dimmable, "platform": platform, "hubName": hub_name, "roomName": room})
        for item in mod.get("powerModuleData") or []:
            sub_id = item.get("applianceId", "")
            sub_type = item.get("type", "")
            if not sub_id or not sub_type or sub_type.upper() == DIMMER_NA or sub_id in seen_ids:
                continue
            seen_ids.add(sub_id)
            room = appliance_room.get(sub_id, "")
            devices.append({"applianceId": sub_id, "moduleId": appliance_id, "serialNumber": serial,
                            "name": _prefixed_name(room, item.get("name") or sub_id), "channelKey": sub_type,
                            "dimmable": False, "platform": "switch", "hubName": hub_name, "roomName": room})
        for item in mod.get("curtainData") or []:
            sub_id = item.get("applianceId", "")
            sub_type = item.get("type", "")
            if not sub_id or not sub_type or sub_type.upper() == DIMMER_NA or sub_id in seen_ids:
                continue
            seen_ids.add(sub_id)
            room = appliance_room.get(sub_id, "")
            devices.append({"applianceId": sub_id, "moduleId": appliance_id, "serialNumber": serial,
                            "name": _prefixed_name(room, item.get("name") or sub_id), "channelKey": sub_type,
                            "dimmable": False, "platform": "cover", "hubName": hub_name, "roomName": room})
        sur = mod.get("surData")
        if sur:
            sur_type = sur.get("type", "").upper()
            sur_id   = sur.get("applianceId") or appliance_id
            raw_name = sur.get("name") or sur_id
            if sur_type and sur_type != DIMMER_NA and raw_name != DIMMER_NA and sur_id not in seen_ids:
                seen_ids.add(sur_id)
                room = appliance_room.get(sur_id, "")
                devices.append({"applianceId": sur_id, "moduleId": appliance_id, "serialNumber": serial,
                                "name": _prefixed_name(room, raw_name), "channelKey": sur_type, "dimmable": False,
                                "platform": SUR_TYPE_MAP.get(sur_type, "switch"), "hubName": hub_name, "roomName": room,
                                "surBrand": sur.get("brand", ""), "surCodeset": sur.get("codeset", "")})
        rgb = mod.get("rgbData")
        if rgb:
            rgb_id = rgb.get("applianceId") or appliance_id
            raw_name = rgb.get("name") or rgb_id
            if rgb_id and rgb_id not in seen_ids:
                seen_ids.add(rgb_id)
                room = appliance_room.get(rgb_id, "")
                devices.append({"applianceId": rgb_id, "moduleId": appliance_id, "serialNumber": serial,
                                "name": _prefixed_name(room, raw_name), "channelKey": rgb.get("type", "RGB"),
                                "dimmable": True, "platform": "light", "hubName": hub_name, "roomName": room, "isRgb": True})
    _LOGGER.info("Zemote: discovered %d devices for %s", len(devices), email)
    return {"identity_id": identity_id, "devices": devices, "rooms": room_list}
