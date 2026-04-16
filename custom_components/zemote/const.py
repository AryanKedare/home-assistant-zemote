"""Constants for Zemote integration."""

DOMAIN = "zemote"
CONFIG_VERSION = 1

# AWS regions
AWS_REGION_COGNITO = "ap-southeast-1"
AWS_REGION_DYNAMO  = "ap-south-1"
AWS_IOT_ENDPOINT   = "adw6e1ab8zihe-ats.iot.ap-southeast-1.amazonaws.com"
IDENTITY_POOL_ID   = "ap-southeast-1:e1070201-318a-4cbd-a07e-f8c90bf81af5"
IOT_POLICY_NAME    = "zemote_policy"

# DynamoDB table names
TABLE_MASTER      = "Master"
TABLE_MODULE_DATA = "Module_Data"
TABLE_ROOM        = "Room"

# HA platforms
PLATFORMS = ["light", "switch", "fan", "cover"]

# Dispatcher signal
SIGNAL_STATE_UPDATED = "zemote_state_updated"

# Device type sentinels
DIMMER_NA  = "NA"
DIMMER_YES = "1"

FAN_TYPE_PREFIXES   = ("F",)
LIGHT_TYPE_PREFIXES = ("L",)

SUR_TYPE_MAP = {
    "TV":           "switch",
    "AC":           "switch",
    "DTH":          "switch",
    "DVD":          "switch",
    "HOME_THEATRE": "switch",
    "PROJECTOR":    "switch",
    "MOODLIGHT":    "light",
}
