"""Constants for Zemote integration."""

DOMAIN = "zemote"

# Config entry version — bump to force re-discovery when platform logic changes
# v4: switched MQTT transport from SigV4 WebSocket (port 443) to X.509 TLS (port 8883)
# v5: entity names now use room + raw_name only (no hub/module name prefix)
# v6: added moodlight platform for cesrm serial devices
CONFIG_VERSION = 6

# AWS
AWS_REGION_COGNITO = "ap-southeast-1"
AWS_REGION_DYNAMO  = "ap-south-1"
AWS_IOT_ENDPOINT   = "adw6e1ab8zihe-ats.iot.ap-southeast-1.amazonaws.com"
IDENTITY_POOL_ID   = "ap-southeast-1:e1070201-318a-4cbd-a07e-f8c90bf81af5"
IOT_POLICY_NAME    = "zemote_policy"

# DynamoDB table names
TABLE_MASTER      = "Master"
TABLE_MODULE_DATA = "Module_Data"
TABLE_ROOM        = "Room"

# HA platforms we register
PLATFORMS = ["light", "switch", "fan", "cover", "lock", "moodlight"]

# Dispatcher signal prefix — appended with serial number
SIGNAL_STATE_UPDATED = "zemote_state_updated"

# LFMData.dimmableStatus sentinels
DIMMER_NA  = "NA"   # not configured / skip
DIMMER_YES = "1"    # dimmable (slider shown in app)
# NOTE: dimmableStatus="0" means non-dimmable light — still a light, not a switch

# LFMData.type prefix → fan  ("F1", "F2" ...)
FAN_TYPE_PREFIXES   = ("F",)

# LFMData.type prefix → light ("L1" ... "L7" ...)
# Classification rule: F* = fan, L* = light, anything else = switch
LIGHT_TYPE_PREFIXES = ("L",)

# SurData.type values → HA platform
SUR_TYPE_MAP = {
    "TV":           "switch",
    "AC":           "switch",
    "DTH":          "switch",
    "DVD":          "switch",
    "HOME_THEATRE": "switch",
    "PROJECTOR":    "switch",
    "MOODLIGHT":    "moodlight",
}

# Lock module type prefix → lock platform
LOCK_TYPE_PREFIXES = ("DL",)

# MOODlight serial prefix — cesrm devices use MDL = 'RRR,GGG,BBB' protocol
MOODLIGHT_SERIAL_PREFIX = "cesrm"
