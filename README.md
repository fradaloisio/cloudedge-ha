# CloudEdge Home Assistant Integration

An Home Assistant integration for CloudEdge cameras. This integration provides control and monitoring of your CloudEdge devices through Home Assistant.
The primary purpose of this integration is to enable **automation control** for your CloudEdge cameras. By integrating with Home Assistant, you can create powerful automations to manage your cameras based on your automations and routines such as automatically enable **motion detection** when the "Away from Home" mode is activated or nobody is detected at home.

> **Disclaimer**: This integration is currently in **beta**. While it provides an interface for interacting with CloudEdge cameras, there are some known and unknown issues (see the Beta Notice section) that will be addressed in future versions.

## Support the Project

If you find this library useful, consider supporting its development! Your contributions help maintain and improve the project.

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/I3I71LBUUU)

## Features

- 🎥 **Camera Integration**: Full camera entity support with device information
- 📡 **Live Streaming**: Experimental P2P live stream support for supported cameras
- 📊 **Sensor Monitoring**: Battery levels, WiFi strength, motion sensitivity, and more
- 🔧 **Device Control**: Switch entities for lights, motion detection, LED status, and notifications
- � **Complete Parameter Access**: All device parameters exposed as entities (disabled by default)
- �🔄 **Auto-refresh**: Configurable refresh intervals to keep device status current
- 🏠 **Multi-home Support**: Supports devices across multiple homes/locations

## Important note about CloudEdge sessions

CloudEdge allows only **one active session per account**. If you log in to this integration using your main account, the CloudEdge app on your phone will be logged out. 

### Recommendation:
To avoid disruptions, it is recommended to:
1. Create a **second CloudEdge account**.
2. Share access to your homes/devices with this second account.
3. Use the second account credentials for this integration.

This ensures that your main account remains logged in on your phone while the integration operates independently.

## Installation

### Method 1: HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to "Integrations"
3. Click the three dots in the top right corner and select "Custom repositories"
4. Add `https://github.com/fradaloisio/cloudedge-ha` as a custom repository
5. Select "Integration" as the category
6. Click "Add"
7. Search for "CloudEdge" and install
8. Restart Home Assistant

### Method 2: Manual Installation

1. Download the latest release from GitHub
2. Extract the files to your Home Assistant `custom_components` directory:
   ```
   custom_components/cloudedge/
   ```
3. Restart Home Assistant


## Enable Debug Logging

Add this to your `configuration.yaml` to enable detailed logging:

```yaml
logger:
  default: warning
  logs:
    custom_components.cloudedge: debug
    cloudedge: debug
```

Then restart Home Assistant and check the logs for detailed information.

## Live Streaming

Supported cameras expose a Home Assistant `camera.*` entity with experimental
live streaming. Battery-powered CloudEdge cameras wake only for finite live-view
windows, so the stream may reconnect when the camera goes back to sleep.

Each camera also exposes a **Stream profile** select entity:

- `Auto`: starts with the camera's advertised default (including adaptive
  stream `105` when supported) and switches to the lowest-resolution profile
  if the live stream is degraded.
- `HD`: uses the camera's advertised main/high-quality stream.
- `SD`: uses the camera's advertised lowest-resolution stream.

Depending on the model these choices map to legacy IDs `0/1` or to modern
profile IDs `100`–`105`. The active and available IDs are shown in the camera
diagnostic attributes.

Use `HD` when you want the best quality and the connection is stable. Use `SD`
when the stream is choppy, stalls, or the camera is on a slower connection.

## Automation Examples

One of the main goals of this integration is making CloudEdge cameras useful inside Home Assistant automations.
In a typical setup you will use:

- `binary_sensor.*_motion` entities as triggers
- `camera.*` entities to attach the latest snapshot to a notification
- `switch.*_motion_detection` entities to enable or disable motion detection

Replace the example entity IDs and notification service below with your own names from Developer Tools.

### Notify when a camera detects motion

This example sends a mobile notification and includes the latest CloudEdge camera snapshot.

```yaml
alias: CloudEdge alerts
description: ""
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.balcony_motion
      - binary_sensor.garage_motion
    to: "on"

variables:
  camera_map:
    binary_sensor.citofono_motion: camera.balcony_balcony
    binary_sensor.garage_motion: camera.garage_garage
  camera_entity: "{{ camera_map[trigger.entity_id] }}"
  name: "{{ trigger.to_state.name | default(trigger.entity_id) }}"

actions:
  - action: notify.mobile_app_XXX
    data:
      title: "Alert triggered {{ name }}"
      message: "Alert triggered {{ name }}"
      data:
        image: "/api/camera_proxy/{{ camera_entity }}"

mode: single
```

### Away From Home example

If you use an `input_boolean`, presence automation, or alarm mode helper, you can arm all cameras at once by enabling motion detection when nobody is home.

The exact `switch.*_motion_detection` entity IDs may differ depending on your device names, so confirm them in Home Assistant Developer Tools before copying this example.

```yaml
input_boolean:
  away_from_home:
    name: Away From Home
    icon: mdi:home-export-outline

automation:
  - alias: CloudEdge - Enable motion detection on all cameras when away
    trigger:
      - platform: state
        entity_id: input_boolean.away_from_home
        to: "on"
    action:
      - service: switch.turn_on
        target:
          entity_id:
            - switch.balcony_motion_detection
            - switch.garage_motion_detection
            - switch.backyard_motion_detection
    mode: single

  - alias: CloudEdge - Disable motion detection on all cameras when back home
    trigger:
      - platform: state
        entity_id: input_boolean.away_from_home
        to: "off"
    action:
      - service: switch.turn_off
        target:
          entity_id:
            - switch.balcony_motion_detection
            - switch.garage_motion_detection
            - switch.backyard_motion_detection
    mode: single
```

## Beta Notice

This integration is currently in **beta**. While it provides an interface for interacting with CloudEdge cameras, there are some known and unknown issues that will be addressed in future versions:

- **Status Reliability**: The API always shows the camera as online, which may not reflect the actual status.
- **Refresh Reliability**: The API refreshes only after some time when the CloudEdge app is not opened on the phone. This does not impact device control.
- **Streaming support**: Live streaming is experimental. It depends on CloudEdge P2P behavior and may be less smooth than the official mobile app on some accounts or regions.

We appreciate your understanding and welcome feedback to improve the integration.
