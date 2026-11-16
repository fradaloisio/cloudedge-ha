# CloudEdge 1.5.0 release preparation

This candidate pairs CloudEdge Home Assistant **1.5.0** with pycloudedge
**0.1.13**. Preparation does not create tags, upload packages or publish GitHub
releases. The release notes are the corresponding sections of each repository's
`CHANGELOG.md`.

## Candidate

- Integration branch: `feat/battery-options`.
- Library branch: `fix/corrective-0.1.13` in the adjacent `pycloudedge` checkout.
- Minimum Home Assistant: **2026.9.4**, running Python **3.14**.
- Minimum library Python: **3.10**.
- `manifest.json` and `requirements-dev.txt` both require `pycloudedge==0.1.13`.
- Existing battery cameras get a **10-minute maximum live view** by default.
  Configure accepts `0` to remove it. Restart Home Assistant after updating.

## Local validation before publication

Follow `../pycloudedge/RELEASE.md` to build and test the library candidate. In
an isolated Python 3.14 environment, install its wheel **before** the integration
requirements while 0.1.13 is not yet available on PyPI:

```sh
python -m pip install ../pycloudedge/dist-staging/0.1.13/pycloudedge-0.1.13-py3-none-any.whl
python -m pip install -r requirements-dev.txt
python -m pytest tests/ -p no:cacheprovider
git diff --check
```

FFmpeg with H.264, HEVC and AAC support is required for the media tests. The
Docker development environment also installs the adjacent library source at the
version required by the integration, without downloading the unreleased package.

The integration CI installs from PyPI. Its checks can pass with the new pin
only after pycloudedge 0.1.13 is published; use the local wheel for preparation.

## Publication order after approval

1. Review and commit both candidates with the repository owner's configured
   author identity and no `Co-authored-by` trailer.
2. Complete the library release described in `../pycloudedge/RELEASE.md`, including
   the clean tag build, artifact checks and publication of **0.1.13** to PyPI.
3. Confirm a fresh environment can install `pycloudedge==0.1.13` from PyPI.
4. Merge the integration candidate and require its CI checks to pass with the
   published library. Verify the manifest version is **1.5.0** and the library
   pin is **0.1.13**.
5. Only then create/push `v1.5.0` and publish the GitHub release, using the
   `1.5.0` changelog section as its notes. A component ZIP, if attached, contains
   the contents of `custom_components/cloudedge/` at the ZIP root for manual
   installation into that directory; the repository tag remains usable by HACS.

Any prepared local ZIP or library archive is a candidate. Rebuild release
artifacts from the approved tagged commits and rerun their checks before upload.
Do not include account files, Home Assistant configuration, logs, caches or
credentials in release archives.
