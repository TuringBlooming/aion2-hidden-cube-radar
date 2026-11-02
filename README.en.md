# AION2 Hidden Cube Radar

A community accessibility aid for AION2 players who cannot hear or have difficulty distinguishing the **Hidden Cube sound cue**. It aims especially to help deaf and hard-of-hearing players access supplementary cues through map markers, coordinates, and visual notifications.

**This is a free, open-source, public-interest project with a non-commercial, non-profit purpose.** The project is not operated to earn revenue from sales, subscriptions, or advertising. Players are not asked to disclose hearing conditions or provide medical documentation.

The local Windows tool records red Hidden Cubes observed in the game's incoming connection data, marks their coordinates, and updates their interaction or opened state when further events arrive. Visual cues can be used independently; optional sound alerts are supplementary and can be disabled.

**Version 1.5.0 · Windows desktop · MIT for original code and original data**

[中文](README.md) · [Architecture](docs/architecture.md) · [Data and assets](docs/data-and-assets.md) · [Building and releasing](docs/releasing.md)

This is an independent project with no official endorsement. It only shows observations received by the local client. **Coverage of the whole map, all channels, and all servers is not guaranteed.** Receiving a record does not prove a new spawn or that the Hidden Cube is still available.

## Features

- Dark desktop controls with native high-DPI rendering. The overlay uses outlined status icons, a legend and a distance scale.
- Drag either map to leave Follow mode and browse freely; later position updates keep your view. Use Follow to centre the character again.
- Historical candidates appear as blue diamonds in both maps by default. Toggle them or click a point for details; they do not indicate current availability.
- Use Clear target to remove the selection, connector and edge arrow without deleting history. New alerts still work. The desktop overlay also supports right-click to clear.

- One desktop manager to start or stop receiving data, open the overlay or browser map, and access history and the data folder.
- Choose Simplified Chinese, English, Japanese, or Korean in the manager’s upper-right corner. Maps and alerts share the choice, which is saved across restarts. Language selection also works before reception starts.
- Sound and Windows notifications for new Hidden Cube observations. Choose Soft chime, Bell, Bubbles, Arcade, Sonar, or the original alarm; preview the sound and set its volume from 0–100%. The default is Soft chime at 35%; 0% is silent. This does not change system or game volume.
- Alert settings in the desktop manager and the browser map share the same saved choices. Previews do not create observations or indicate a Hidden Cube sighting.
- Optionally keep the overlay hidden until a new live Hidden Cube alert arrives. Reception continues while hidden. Use the hide-and-wait button in the overlay, or open it manually from the manager. Keep the manager running; it can be minimized. Exiting the whole tool stops reception. History, replay, previews, and ordinary status updates do not trigger a popup.
- Local map detail is preserved when a target arrives. Following keeps the character reference centered; distant targets get a screen-edge arrow.
- Separate observed, interacted, opened, and unknown states.
- Persistent local history and clearly labeled estimated refresh windows.
- Built-in artwork, calibrated coordinates and detailed tiles for 18 maps; optional local imports can override the bundled resources.
- Complete historical candidate points and a separate public observation dataset with provenance.

Character coordinates are intermittent references. They expire after **10 seconds** and are not continuous tracking. Expired references do not produce live distance estimates. An edge arrow describes the target's direction relative to the current map view.

### Usage notes: positioning and flight

- **Continuous positioning during flight is not supported in this version.** The reference may remain at the pre-flight location for a long time. A centered marker does not establish that it is still your current location.
- To request a fresh reference through normal gameplay, stand on safe ground, **press Space to jump in place, and land normally**. Existing tests usually received a new reference when landing. This is a single update rather than continuous tracking, and an update is not guaranteed after every jump.
- Walking continuously can also update slowly. When the reference expires, use the official in-game minimap to determine your location; do not navigate from stale coordinates, connectors, or distances.
- The tool does not press Space, move the character, or operate wings. These actions are performed by the player in the game.

A temporary quiet period on the same connection preserves its confirmed map; new observations resume positioning automatically. After a real disconnect, replacement connection or app restart, “Map unconfirmed” means a normal channel or map change is needed to confirm the current map again. Browsing the world map is not used as evidence of the character's map.

## Using the Windows package

1. Extract `AION2-Hidden-Cube-Radar-1.5.0-windows-x64.zip` completely.
2. Open its `AION2 Hidden Cube Radar` folder, run **`AION2 Hidden Cube Radar.exe`**, and keep `_internal/` beside it.
3. The manager does not start capture automatically. Run the game and choose **开始实时接收** (Start live receiving) when you want live observations.
4. Choose **显示悬浮地图** (Show overlay) or **打开完整地图** (Open full map). Select a target to inspect its coordinates and recorded state.
5. Use **停止接收** (Stop receiving) to stop capture. Your history remains in the data folder.

The manager also provides Browse history, Open data folder, and Import map assets. Built-in maps work without an import; an optional matching artwork and calibration pack can override them. Browsing history or a map does not implicitly start capture. Manager labels follow the selected interface language.

There is one user-facing EXE entry point. The `_internal/` directory contains runtime dependencies; the EXE alone is not portable. Python is not required to use the release package.

Live capture requires **[Npcap](https://npcap.com/#download)**, installed separately from its official source under its own license. This project does not bundle its driver or installer. Without Npcap access, the built-in maps and history remain available.

The tool does not elevate itself, change PowerShell execution policy, install drivers, or register startup tasks. Releases are currently unsigned; no claim is made that SmartScreen or antivirus software will never flag them. Keep security software enabled. A suspected false-positive report should include the version, SHA-256, and detection name.

Git is optional for local history versioning. No archive is automatically pushed to a remote.

## Run from source

Use Windows PowerShell from the repository root. Python 3.12 x64 with Tkinter is the recommended development setup.

```powershell
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pip install -e .
.venv/Scripts/python.exe -m aion2_redbag_map
```

This opens the same manager without starting capture. Calling the virtual environment's Python directly avoids activation scripts and execution-policy changes.

## Build and test

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-build.txt
.venv/Scripts/python.exe scripts/build.py
```

Artifacts are written under `dist/`:

- `AION2 Hidden Cube Radar/AION2 Hidden Cube Radar.exe` and `_internal/`: the runnable directory.
- `AION2-Hidden-Cube-Radar-1.5.0-windows-x64.zip`: the complete Windows package.
- `aion2-hidden-cube-radar-1.5.0-source.zip`: the source archive.
- `SHA256SUMS.txt`: checksums for both ZIP files.

The Windows package also includes its corresponding source, public datasets, build information, and actual dependency licenses under `_internal/`. Building does not publish or upload a release. See [the release guide](docs/releasing.md).

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -v
node tests/test_alert_settings_ui.cjs
node tests/test_i18n.cjs
node tests/test_player_ui.cjs
node tests/test_refresh_ui.cjs
```

Node.js is only required for browser-code tests. Offline tests do not establish compatibility with every game version, accelerator, or network setup.

## Data and implementation boundaries

Local history, settings, logs, and imported assets live under `AION2-RedBag-Map/data/` in the user's Windows Documents folder. The manager can open that folder. The tool has no telemetry, remote update check, or automatic upload. Its browser server binds only to loopback, normally on port `18726`.

The legacy data-folder name is retained for upgrade compatibility. AION2 Hidden Cube Radar reuses existing locations, preferences, and history without renaming or moving the archive. Python module names and local service identifiers also retain their compatibility names.

- [`candidates.json`](src/aion2_redbag_map/static/candidates.json) preserves **3,722 source records and 3,721 unique coordinate locations**. [Provenance](data/provenance.json) records the source and conversion. These are not confirmed red Hidden Cubes or current spawns.
- [`data/observations.json`](data/observations.json) contains the project's public anonymized observations. Its `record_count` is the authoritative count for that snapshot.
- Personal `history.json` is not automatically published or merged into the public dataset.
- [`static/maps/`](src/aion2_redbag_map/static/maps) contains 18 overviews and 321 detailed tiles. The calibrated [`maps.json`](src/aion2_redbag_map/maps.json) and [`map-source.json`](src/aion2_redbag_map/map-source.json) preserve the geometry, origin and image checksums. These resources were obtained from A2Helper 0.1.25 and verified unchanged in 0.1.29; they are included in both source and Windows packages.
- Optional local imports go into the data folder's `assets/`. Imported calibration and matching image names take precedence over bundled resources. Supported layouts include `maps.json` plus `maps/`, or the older `static/maps/` layout. Maps are not downloaded automatically.

The collector enumerates process names/PIDs and the Windows TCP table, then applies exact incoming-connection filters through Npcap. It does not open the game process, access game memory, inject code, automate input, or send game packets. The `OpenProcess(SYNCHRONIZE)` use in `git_archive.py` only waits for the tool's own parent process to exit. See [architecture](docs/architecture.md).

These implementation limits are not a guarantee of acceptance by the game's rules or anti-cheat system.

## License and contributions

[MIT](LICENSE) covers original project code and original public data. It does not relicense third-party content. See [third-party notices](THIRD_PARTY_NOTICES.md), [data and assets](docs/data-and-assets.md), and [CONTRIBUTING](CONTRIBUTING.md).

The public-interest and non-commercial purpose describes how this project is developed and operated; it adds no use restriction to MIT. The [standard MIT terms](https://opensource.org/license/mit) permit commercial use and redistribution subject to their conditions. Third-party data and assets do not acquire MIT permission through inclusion here.

**Redistribution permission for the A2Helper-derived candidate data, map artwork and calibration has not been confirmed.** These files are included at the project author's request with their provenance and original rights status preserved. Inclusion does not relicense them as MIT or imply official game endorsement. Downstream use and redistribution still require checking the upstream rights.

Do not attach personal archives, raw packet captures, control tokens, or unredacted logs to public issue reports.
