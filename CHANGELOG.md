# Changelog

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
