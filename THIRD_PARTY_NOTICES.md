# Third-party material

The MIT license covers original project code, documentation, the generated alert sound,
and the author's shared observations in `data/observations.json`. It does not grant
rights to third-party datasets, game artwork, trademarks, or separately licensed dependencies.

## Point data

`src/aion2_redbag_map/static/candidates.json` contains the complete A2Helper historical
hidden-bag candidate dataset (3,722 source entries, 3,721 coordinate locations).
`data/provenance.json` records its origin, conversion and checksum. The dataset is
retained here at the project author's request; an upstream redistribution license
has not been confirmed. Attribution is not permission. This project does not
relicense that upstream material as MIT. Resolve the upstream rights before
public redistribution of this dataset or a binary containing it.

## Map artwork and calibration

`src/aion2_redbag_map/static/maps/` includes all 339 existing WebP files: 18 overview
images and 321 detailed tiles. `src/aion2_redbag_map/maps.json` contains the matching
calibration for 18 maps. They were originally obtained from the A2Helper 0.1.25
distribution and verified unchanged against its 0.1.29 release. The source release,
verification date and per-image SHA-256 values are preserved in
`src/aion2_redbag_map/map-source.json`.

These resources are included in the source repository and Windows package at the
project author's request. Copyright and other rights remain with the original
rightsholders. An applicable upstream redistribution license has not been confirmed;
the project's MIT license does not grant rights to relicense this artwork or
calibration. Inclusion and attribution are not permission or official endorsement.
Users may optionally import matching resources they have the right to use; imported
geometry and same-named images override the bundled versions locally.

## Protocol research

The wire format was researched using local traffic samples and static inspection of
A2Helper's distribution. The parser is this project's Python implementation; this
does not imply authorization from A2Helper or NCSOFT.

## Runtime and build dependencies

- **CPython / standard library:** PSF license and included third-party licenses.
  <https://docs.python.org/3/license.html>
- **Tcl/Tk:** its own license; included with the Windows Python runtime.
  <https://www.tcl-lang.org/software/tcltk/license.html>
- **Pillow 13.0.0:** MIT-CMU and licenses of its included image libraries.
  The complete installed distribution license is copied into binary releases.
  <https://github.com/python-pillow/Pillow/blob/13.0.0/LICENSE>
- **PyInstaller 6.22.3:** GPL with its bootloader/distribution exception.
  <https://pyinstaller.org/en/stable/license.html>
- **Npcap:** external, separately licensed prerequisite for live receiving. Neither
  its driver, DLLs, installer nor license entitlement is bundled.
  <https://npcap.com/#download> · <https://github.com/nmap/npcap/blob/master/LICENSE>
- **Git:** optional external command for local history versions; not bundled.

Builds include actual installed dependency license texts under `_internal/licenses`.
See `build-info.json` and `SHA256SUMS.txt` for the exact release contents and versions.
No project code-signing identity or third-party security certification is claimed.
