# Changelog

## 1.5.0

Adds configurable battery-camera live view limits, a repair for stream preloading and integration options, using pycloudedge 0.1.13.

Changes:
- Add options (Configure): refresh interval, a maximum live view time for battery cameras, session keepalives for battery cameras, and camera audio. Saving a change reloads the integration; saving without changes does not.
- Limit a battery camera's live view to 10 minutes by default, then let the camera rest: a dashboard left open no longer keeps it awake for good.
- Raise a repair issue when "Preload camera stream" is on for a battery camera, with a fix that turns it off and lets the unwatched stream end. Ignoring the issue lasts across reloads.
- Fix the Motion Sensitivity and Speaker Volume sensors, which failed as soon as they were enabled.
- Shorten the frozen picture after a camera stops answering from 90 to 60 seconds.
- Setup: spaces around the email are removed, and an address that is not an email is reported on its field instead of as a connection failure.
- "Enable debug logging" on the integration page now includes the pycloudedge library.
- A live session that ignored its stop request (possible with pycloudedge 0.1.12 when the stop came before the session started) is asked again as soon as it delivers anything, instead of keeping the camera streaming unwatched.
- The live stream no longer changes the coordinator's device data from its worker thread, and sensors are no longer rebuilt on every update that brings no new parameter.
- README: actions, the `cloudedge_event` event, debug logging from the UI and troubleshooting.
- Update pycloudedge to 0.1.13 for fragmented-video recovery, MQTT callback handling and authentication recovery after temporary credential-renewal failures.

Requirements: Home Assistant 2026.9.4 or newer. Restart Home Assistant after updating. Existing battery cameras use the new 10-minute live view limit by default; set it to `0` in Configure to remove the limit.

## 1.4.4

Fixes live streaming, account setup and camera diagnostics, using pycloudedge 0.1.12. Cameras that previously negotiated a relay connection without displaying video can now stream in Home Assistant.

Changes:
- Improve stream reconnects, profile selection, buffering, slow-viewer handling, decoder restarts and session cleanup.
- Include camera audio as AAC in the live stream and use the current live frame for snapshots without waking idle cameras.
- Correct dormant-camera status and expose battery, Wi-Fi and motion-detection attributes.
- Retry setup after temporary cloud failures and support accounts with owned/default and shared homes.
- Remove P2P secrets from entity attributes, service events and logs. Opening live view no longer changes the floodlight setting.

Requirements: Home Assistant 2026.9.4 or newer. Restart Home Assistant after updating.

Security note: upgrading does not erase secrets stored by earlier versions in recorder history, backups or shared logs. Follow the cleanup guidance in the README if affected.

Validation: 266 integration tests and 331 library tests; live checks on three cameras. The previously failing camera delivered H.264 at 2304×1296, with 1,292 frames and no reconnects during the Home Assistant check; the session stopped automatically afterwards.
