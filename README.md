# Zemote Home Assistant Integration

[![GitHub license](https://img.shields.io/github/license/AryanKedare/ha-zemote.svg)](https://github.com/AryanKedare/ha-zemote/blob/ha-zemote/LICENSE)
[![GitHub issues](https://img.shields.io/github/issues/AryanKedare/ha-zemote.svg)](https://github.com/AryanKedare/ha-zemote/issues/)
[![GitHub pull-requests](https://img.shields.io/github/issues-pr/AryanKedare/ha-zemote.svg)](https://github.com/AryanKedare/ha-zemote/pulls/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat-square)](http://makeapullrequest.com)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

[![GitHub watchers](https://img.shields.io/github/watchers/AryanKedare/ha-zemote.svg?style=social&label=Watch)](https://github.com/AryanKedare/ha-zemote/watchers/)
[![GitHub forks](https://img.shields.io/github/forks/AryanKedare/ha-zemote.svg?style=social&label=Fork)](https://github.com/AryanKedare/ha-zemote/network/)
[![GitHub stars](https://img.shields.io/github/stars/AryanKedare/ha-zemote.svg?style=social&label=Star)](https://github.com/AryanKedare/ha-zemote/stargazers/)

If you find this integration useful, please give it a ⭐ or fork it and contribute!

# Zemote Integration Documentation

<p align="center">
    <img src="zemote.png" width="50%">
</p>

The **Zemote** integration connects your [Zemote](https://www.zemote.in/) smart home devices to Home Assistant. It gives you real-time, cloud-push control over your lights, fans, switches, and covers — all from the Home Assistant UI.

## Supported Platforms

| Platform | Description |
|----------|-------------|
| `light`  | Dimmable and non-dimmable lights (type prefix `L*`) |
| `switch` | Switches, TV, AC, DTH, DVD, Home Theatre, Projector, and Moodlight |
| `fan`    | Fans with speed control (type prefix `F*`) |
| `cover`  | Curtains / blinds |

## Prerequisites

- A working **Zemote account** with devices already set up in the Zemote app
- Home Assistant version **2023.1** or later (recommended)
- [HACS](https://hacs.xyz/) installed (for easy installation)

## Installation Guide

### Via HACS (Recommended)

1. Open **HACS** in Home Assistant
2. Go to **Integrations** → click the three-dot menu → **Custom repositories**
3. Add `https://github.com/AryanKedare/ha-zemote` with category **Integration**
4. Search for **Zemote** and click **Download**
5. Restart Home Assistant

### Manual Installation

1. Download or clone the `ha-zemote` branch of this repository
2. Copy the `custom_components/zemote` folder into your HA `config/custom_components/` directory
3. Restart Home Assistant

## Configuration

After installation, add the integration via the Home Assistant UI:

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for **Zemote**
3. Follow the setup flow — your Zemote identity will be fetched and your devices discovered automatically

## Dependencies

The following Python packages are required and installed automatically:

- [`boto3 >= 1.26.0`](https://pypi.org/project/boto3/) — AWS SDK for Cognito & IoT
- [`paho-mqtt >= 2.0.0`](https://pypi.org/project/paho-mqtt/) — MQTT client
- [`certifi >= 2023.0.0`](https://pypi.org/project/certifi/) — CA certificates for TLS

## Pull Requests Workflow

1. **Fork the project**: Fork the `ha-zemote` branch to your own GitHub account
2. **Start development**: Make your changes on the forked branch
3. **Commit changes**: Push your changes to your fork
4. **Create a Pull Request**: Open a PR targeting the `ha-zemote` branch
5. **Review**: PRs will be reviewed before merging

## Issue Feedback

You can report bugs or request features via **[GitHub Issues](https://github.com/AryanKedare/ha-zemote/issues/)**.