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

Home Assistant 2025.12 or newer is required: live streaming relies on the PyAV
release that Home Assistant ships from that version on.

If Home Assistant runs with an `http_proxy` environment variable, add
`127.0.0.1` to the lowercase `no_proxy` variable. Without it, the HLS live
view, live still images and recordings fail, and the proxy receives the
camera's local stream URL, secret included; the WebRTC live view is not
affected. Home Assistant opens that URL with FFmpeg, which reads only the
lowercase variables and compares `no_proxy` entries with the host name, so
`localhost` or `127.0.0.0/8` does not cover `127.0.0.1`. The integration logs
a warning when it detects this setup.

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
windows, so the stream may reconnect when the camera goes back to sleep. While
Home Assistant waits, the camera snapshot shows a language-neutral animated
loading indicator.
Incoming G.711 camera audio is included in the live stream as browser-compatible
AAC mono audio.

Each camera also exposes a **Stream profile** select entity:

- `Auto`: starts with the camera's own default stream, the one the CloudEdge
  app opens (adaptive stream `105` when supported). If the live stream is
  degraded it falls back to the `SD` stream, but only when that stream is
  lighter: a camera whose default already is its smallest profile stays on it.
- `HD`: uses the adaptive stream when supported, otherwise the camera's
  largest advertised resolution.
- `SD`: uses the camera's smallest advertised resolution, never larger than
  `HD`.

Some battery cameras advertise their smallest profile first. Earlier versions
treated that first profile as the main stream, so on those cameras `HD` and
`SD` were swapped and a degraded `Auto` stream moved to the largest one.

Depending on the model these choices map to legacy IDs `0/1` or to modern
profile IDs `100`–`105`; a camera with a single available stream uses it for
every choice. The active and available IDs are shown in the camera
diagnostic attributes.

Use `HD` when you want the best quality and the connection is stable. Use `SD`
when the stream is choppy, stalls, or the camera is on a slower connection.

Stream diagnostics report `streaming` only after a usable video keyframe arrives.
If a connection is established but produces no video, the integration retries
with a bounded recovery window. After four attempts in a row without video,
automatic retries pause for 60 seconds, doubling with each further failed
attempt up to 10 minutes; `stream_state` then reads `retry_cooldown` and
`stream_retry_cooldown` shows the seconds left. Changing the Stream profile
ends the pause; once video arrives again, a later pause starts again from 60
seconds. Changing profiles reopens
the decoder when the video format changes. Battery-camera wake time and
live-view limits still depend on the device.

After a short network pause the live video catches up with about one second
of fast-forward instead of staying behind; during a longer pause, such as a
battery camera reconnecting, the last keyframe stays on screen, and video
resumes from the camera's next keyframe.

Opening the live view no longer changes any camera setting. Earlier versions
turned IoT parameter `167` on for each live view and off afterwards; on
floodlight cameras that parameter is the front light switch, so viewing could
switch the light and overwrite its setting. The `stream_live_switch` diagnostic
attribute has been removed.

Earlier versions exposed the camera's P2P secrets: `host_key` in the attributes
of the `Status` sensor (`sensor.<device>_status`), and the P2P credentials in
the log and in the `cloudedge_device_info` event of the
`cloudedge.get_device_info` service. The attribute has been removed, the
service output is redacted, and the secret IoT parameters `3`, `21` and `217`
(device key, safe password, video password) no longer become sensors. Updating
does not remove what the recorder already stored, nor what backups made before
the update contain: purge the history of those
`Status` sensors, and of any `Device key`, `Safe password` or `Video password`
sensor you had enabled, with `recorder.purge_entities`, then delete the
leftover secret sensors. Old `cloudedge_device_info` events expire with the
recorder history (10 days by default); to remove them now, exclude that event
type (`exclude: event_types`) in the `recorder` configuration and run
`recorder.purge` with `apply_filter: true`. Treat logs you have already shared
as compromised (earlier versions also logged the service output at INFO level),
and pair the camera again if you published them.

## Status, snapshots and startup

- The `Connection Status` sensor follows the CloudEdge cloud status: a
  sleeping battery camera reads `dormancy`. Earlier versions showed `offline`
  for sleeping cameras from the second refresh on, so automations triggered by
  `offline` could fire while a camera was only asleep. The integration no
  longer pings cameras on the local network.
- While a live view is streaming, the camera's still image (dashboard cards,
  notifications through `/api/camera_proxy`) is the current live frame.
  Otherwise it remains the last alarm snapshot or the product picture: a still
  image never starts a stream, so it does not wake a sleeping camera.
- Camera attributes such as `battery_percent`, `wifi_strength` and
  `motion_det_enable` are now published; earlier versions never showed them.
- The `cloudedge_device_info` event of `cloudedge.get_device_info` now
  includes the device configuration, and an alarm snapshot no longer makes the
  recorder drop the event.
- If CloudEdge cannot be reached when Home Assistant starts, Home Assistant
  retries the integration's setup after 5, 10, 20 seconds and so on, up to
  every 10 minutes. Earlier versions loaded without entities and waited for
  the next refresh interval.

## Development and streaming tests

Use Python 3.14 and install `requirements-dev.txt`. Media regression tests also
need an `ffmpeg` executable with H.264, HEVC and AAC support. The tests generate
their own video and do not require camera credentials:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests/ -p no:cacheprovider
```

CI also runs the suite on Home Assistant 2025.12.0, the minimum declared in
`hacs.json`, with Python 3.13 and the stream requirements of that release (see
`.github/workflows/ci.yml`). Raise both together.

When testing changes in both repositories, install the corresponding local
`pycloudedge` checkout in this development environment, or place it on
`PYTHONPATH`. Release the library before updating the pinned version in both
`manifest.json` and `requirements-dev.txt`.

### Test with Docker

Keep the `pycloudedge` checkout next to this repository. The development Compose
uses Home Assistant 2026.9.4, mounts this integration directly, and installs the
adjacent library's current source at each container start. Its local package
version matches the integration manifest so HA retains the development build.
Account settings and recordings stay in the ignored `ha-config/` directory.

```sh
cd cloudedge-ha
docker compose up -d --force-recreate
docker compose logs -f homeassistant
```

Open `http://localhost:8123`. After editing either checkout, use
`docker compose restart homeassistant` to reload both. The Compose file preserves
host networking for the P2P transport; on macOS it uses the Docker VM's network.
With Colima, port forwarding must expose the VM's port 8123 to the host. Stop the
test environment with `docker compose down` (the configuration directory remains).

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
