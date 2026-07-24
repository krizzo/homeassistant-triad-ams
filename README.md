[![HACS](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration) [![Validate](https://github.com/bharat/homeassistant-triad-ams/actions/workflows/validate.yml/badge.svg?branch=main)](https://github.com/bharat/homeassistant-triad-ams/actions/workflows/validate.yml) [![Lint](https://github.com/bharat/homeassistant-triad-ams/actions/workflows/lint.yml/badge.svg?branch=main)](https://github.com/bharat/homeassistant-triad-ams/actions/workflows/lint.yml) [![Release](https://img.shields.io/github/v/release/bharat/homeassistant-triad-ams?sort=semver)](https://github.com/bharat/homeassistant-triad-ams/releases)

Triad AMS for Home Assistant
============================

A custom Home Assistant integration for controlling a Triad AMS 8x8 audio matrix switch over TCP. The integration exposes one media player entity per active output zone and lets you select a routed input, adjust volume, and optionally mirror metadata from an upstream media player entity.

Attribution: The device protocol and command bytes used by this integration were derived from the excellent work by Tim Weiler — https://github.com/tim-weiler/triad-audio-matrix. Thank you Tim!

Status
------
- Supported hardware:
  - Triad AMS 8x8 (Audio Matrix Switch)
  - Triad AMS 16x16 (Audio Matrix Switch)
  - Triad AMS 24x24 (Audio Matrix Switch)
- Transport: TCP (default port 52000)
- Discovery: Not implemented (manual host/port entry)

Features
--------
- One media player entity per active output (zone)
- Turn on/off a zone (routes/disconnects the source)
- Select source (routed input)
- Set volume per zone
- Optional input→entity linking
  - Link a Triad input to a Home Assistant `media_player` entity (e.g., Sonos)
  - Triad output entity proxies metadata (title/artist/album/artwork) from the linked player when that input is selected
- Simple config flow
  - Select device model (TS-AMS8, TS-AMS16, or TS-AMS24)
  - Choose which outputs and inputs are active
  - Optionally set a link for each input
- Service-based routing
  - `triad_ams.turn_on_with_source` for "route the input feeding entity X to zone Y" automations
  - `triad_ams.set_route` for direct `(output, input)` routing without involving HA entities (use `input: 0` to disconnect)
- Advanced audio settings entities (see below): input gain/delay, balance,
  bass/treble, loudness, DSP output modes, 12-band room EQ, 2.1 crossover,
  test tone, audio-sense sensors, and a device reboot button
- Safe device handling
  - Serialized command writes
  - Trigger zone on when the first output is routed; off when the last output disconnects
  - Remembers and restores the last input when a zone is turned back on

Installation (HACS)
-------------------
This integration is available directly in HACS under the Integration category.

1. In Home Assistant, go to HACS → Integrations.
2. Search for "Triad AMS" and install.
3. Restart Home Assistant when prompted

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=bharat&repository=homeassistant-triad-ams)

Manual install (without HACS)
-----------------------------
- Copy the `custom_components/triad_ams` folder from this repository into your Home Assistant `config/custom_components/` directory
- Restart Home Assistant

Configuration
-------------
1. Settings → Devices & Services → “Add Integration” → search for “Triad AMS”
2. Enter the Triad AMS host/IP and port (default 52000), and select your device model (TS-AMS8, TS-AMS16, or TS-AMS24)
3. In the next step:
   - Active Outputs: select which zones you want entities for
   - Active Inputs: select which inputs are usable as sources
   - Optional “link_input_<n>”: choose a `media_player` entity to mirror metadata when that input is routed
4. Save. The entry reloads and entities are created for active outputs only

Notes
-----
- You can rename outputs (zones) and set areas from each entity’s settings page
- If you later change the active lists or links in Options, the integration reloads and updates entities automatically
- The device model selected during initial setup determines the number of available inputs and outputs

Advanced audio settings entities
--------------------------------
Beyond the media players, the integration exposes the device's advanced audio
settings (reverse-engineered from packet captures) as settings entities
grouped under the Triad AMS device:

- Per output (zone): balance, bass/treble (shelf gain), loudness switch, and
  DSP mode select (Stereo / Mono / DSP Bypass / 2.1 variants / Test Signal)
- Per output, disabled by default (enable from the entity registry as needed):
  shelf frequency/Q, max volume, turn-on volume, audio delay, 12-band
  parametric room EQ (frequency/gain/Q per band), room EQ lock, 2.1 crossover
  (frequency, filter type, sub volume offset), and test tone (switch + level)
- Per input: gain; disabled by default: audio delay and an "audio detected"
  binary sensor
- Device: a reboot button, plus firmware version and MAC address in device
  info and diagnostics

Settings entities read their value from the device when added (or when you
call `homeassistant.update_entity`) and update optimistically when changed.

Services
--------
- `triad_ams.turn_on_with_source` — entity service on a Triad AMS `media_player`. Takes an `input_entity_id` of another HA `media_player`; the integration looks up which Triad input is linked to that entity and routes it to the target zone. Best when both ends of the action are HA entities (the upstream source has a media_player and you want the zone to mirror it).
- `triad_ams.set_route` — global service. Takes two integers, `output` (1..N) and `input` (0..M); routes the input to the output directly. Use `input: 0` to disconnect the output. Best for headless or calibration automations that think in terms of physical port numbers rather than HA entities. Raises if more than one Triad AMS config entry is configured (no implicit broadcast).
- `triad_ams.set_protocol_debug` — toggles protocol-level logging across all configured entries. Field: `enabled: true|false`.

Limitations
-----------
- Only the Triad AMS 8x8 model has been confirmed with real hardware; the 16x16 and 24x24 are supported in code but untested.
- No automatic discovery — enter host/port manually.
- The device emits no state push; the integration reads state on demand and around actions.

Troubleshooting
---------------
- Need protocol-level logging?
  - Enable the integration logger in `configuration.yaml`:
    - `logger.logs.custom_components.triad_ams: debug`
  - Toggle at runtime via the `triad_ams.set_protocol_debug` service (`enabled: true/false`).
- No inputs in the source list?
  - Make sure the inputs are marked active in Options
- Metadata not shown?
  - Set a `link_input_<n>` to the upstream player for that input; the Triad entity will proxy media fields only when linked and routed to that input
- Old output devices linger in the UI?
  - The integration prunes stale entities/devices on reload when outputs are deactivated. If you still see devices, check for disabled entities tied to them

Credits
-------
- Protocol reference and driver data: Tim Weiler — https://github.com/tim-weiler/triad-audio-matrix
- The `set_route(output, input)` service shape was adopted from [Richt198/hass-control4-avm](https://github.com/Richt198/hass-control4-avm), a community Home Assistant integration for the Control4 AVM-16S1-B audio matrix. Thanks to Richt198 for the clean, orthogonal service surface.
- [OtisPresley/control4-mediaplayer](https://github.com/OtisPresley/control4-mediaplayer) is a related community integration for the Control4 Matrix Amp. We track it for ideas worth borrowing as both projects evolve.
- Integration author: @bharat (and contributors)

License
-------
This project inherits the license of this repository. See LICENSE for details.
