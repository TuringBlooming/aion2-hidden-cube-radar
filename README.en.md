# AION2 RedBag Map

A local Windows tool that records red hidden bags observed in the game's incoming connection data, marks their coordinates, and updates their interaction or opened state when further events arrive.

**Version 1.3.0 · Windows desktop · MIT for original code and original data**

[中文](README.md) · [Architecture](docs/architecture.md) · [Data and assets](docs/data-and-assets.md) · [Building and releasing](docs/releasing.md)

This is an independent project with no official endorsement. It only shows observations received by the local client. **Coverage of the whole map, all channels, and all servers is not guaranteed.** Receiving a record does not prove a new spawn or that the bag is still available.

## Features

- One desktop manager to start or stop receiving data, open the overlay or browser map, and access history and the data folder.
- Sound and Windows notifications for new red-bag observations.
- Local map detail is preserved when a target arrives. Following keeps the character reference centered; distant targets get a screen-edge arrow.
- Separate observed, interacted, opened, and unknown states.
- Persistent local history and clearly labeled estimated refresh windows.
- A coordinate grid by default, with optional user-imported map assets.
- Complete historical candidate points and a separate public observation dataset with provenance.

Character coordinates are intermittent references. They expire after **10 seconds** and are not continuous tracking. Expired references do not produce live distance estimates. An edge arrow describes the target's direction relative to the current map view.

## Using the Windows package

1. Extract `AION2-RedBag-Map-1.3.0-windows-x64.zip` completely.
2. Open its `AION2 RedBag Map` folder, run **`AION2 RedBag Map.exe`**, and keep `_internal/` beside it.
3. The manager does not start capture automatically. Run the game and choose **开始实时接收** (Start live receiving) when you want live observations.
4. Choose **显示悬浮地图** (Show overlay) or **打开完整地图** (Open full map). Select a target to inspect its coordinates and recorded state.
5. Use **停止接收** (Stop receiving) to stop capture. Your history remains in the data folder.

The manager also provides **浏览历史** (Browse history), **打开数据目录** (Open data folder), and **导入地图资源** (Import map assets). Browsing history or a map does not implicitly start capture. The manager's button labels are currently Chinese.

There is one user-facing EXE entry point. The `_internal/` directory contains runtime dependencies; the EXE alone is not portable. Python is not required to use the release package.

Live capture requires **[Npcap](https://npcap.com/#download)**, installed separately from its official source under its own license. This project does not bundle its driver or installer. Without Npcap access, the grid and history remain available.

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

- `AION2 RedBag Map/AION2 RedBag Map.exe` and `_internal/`: the runnable directory.
- `AION2-RedBag-Map-1.3.0-windows-x64.zip`: the complete Windows package.
- `aion2-redbag-map-1.3.0-source.zip`: the source archive.
- `SHA256SUMS.txt`: checksums for both ZIP files.

The Windows package also includes its corresponding source, public datasets, build information, and actual dependency licenses under `_internal/`. Building does not publish or upload a release. See [the release guide](docs/releasing.md).

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -v
node tests/test_i18n.cjs
node tests/test_player_ui.cjs
node tests/test_refresh_ui.cjs
```

Node.js is only required for browser-code tests. Offline tests do not establish compatibility with every game version, accelerator, or network setup.

## Data and implementation boundaries

Local history, settings, logs, and imported assets live under `AION2-RedBag-Map/data/` in the user's Windows Documents folder. The manager can open that folder. The tool has no telemetry, remote update check, or automatic upload. Its browser server binds only to loopback, normally on port `18726`.

- [`candidates.json`](src/aion2_redbag_map/static/candidates.json) preserves **3,722 source records and 3,721 unique coordinate locations**. [Provenance](data/provenance.json) records the source and conversion. These are not confirmed red bags or current spawns.
- [`data/observations.json`](data/observations.json) contains the project's public anonymized observations. Its `record_count` is the authoritative count for that snapshot.
- Personal `history.json` is not automatically published or merged into the public dataset.
- Third-party map images and calibrated `maps.json` are not bundled. Authorized local imports go into the data folder's `assets/`. Supported layouts include `maps.json` plus `maps/`, or the older `static/maps/` layout.

The collector enumerates process names/PIDs and the Windows TCP table, then applies exact incoming-connection filters through Npcap. It does not open the game process, access game memory, inject code, automate input, or send game packets. The `OpenProcess(SYNCHRONIZE)` use in `git_archive.py` only waits for the tool's own parent process to exit. See [architecture](docs/architecture.md).

These implementation limits are not a guarantee of acceptance by the game's rules or anti-cheat system.

## License and contributions

[MIT](LICENSE) covers original project code and original public data. It does not relicense third-party content. See [third-party notices](THIRD_PARTY_NOTICES.md), [data and assets](docs/data-and-assets.md), and [CONTRIBUTING](CONTRIBUTING.md).

**Redistribution permission for the A2Helper candidate dataset has not been confirmed.** Retaining the complete dataset and provenance does not establish permission to relicense it as MIT or to redistribute it unconditionally. Distribution containing that dataset still requires license verification or the appropriate permission.

Do not attach personal archives, raw packet captures, control tokens, or unredacted logs to public issue reports.
