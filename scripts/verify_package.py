"""Verify the frozen EXE offline using only owned children and temporary data.

No desktop, overlay, browser, packet capture, or existing service is started or
controlled. The HTTP client disables proxies and redirects. Control tokens stay
in memory and are never included in the report.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import tomllib
import wave
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXE = ROOT / "dist/AION2 Hidden Cube Radar/AION2 Hidden Cube Radar.exe"
DEFAULT_REPORT = ROOT / "build/qa/frozen-service-check.json"
APPLICATION_ID = "aion2-redbag-map"


class VerificationError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise VerificationError(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise VerificationError("HTTP redirects are forbidden during package verification")


def pe_properties(executable):
    import pefile  # Pinned build dependency; the runtime application does not need it.

    pe = pefile.PE(str(executable), fast_load=True)
    try:
        require(pe.FILE_HEADER.Machine == 0x8664, "The executable is not Windows x64")
        require(pe.OPTIONAL_HEADER.Subsystem == 2, "The executable is not a windowed GUI-subsystem build")
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        manifest_resources = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
        require(manifest_resources is not None, "The executable has no Windows resources")
        manifests = []
        for kind in manifest_resources.entries:
            if kind.id != 24:  # RT_MANIFEST
                continue
            for name in kind.directory.entries:
                for language in name.directory.entries:
                    resource = language.data.struct
                    manifests.append(ET.fromstring(pe.get_data(resource.OffsetToData, resource.Size)))
        levels = [element for manifest in manifests for element in manifest.iter()
                  if element.tag.rsplit("}", 1)[-1] == "requestedExecutionLevel"]
        require(bool(levels), "The executable has no requestedExecutionLevel manifest entry")
        require(all(item.get("level") == "asInvoker" and item.get("uiAccess") == "false" for item in levels),
                "The executable requests elevation or UI access")
        dpi_modes = [element.text for manifest in manifests for element in manifest.iter()
                     if element.tag.rsplit("}", 1)[-1] == "dpiAwareness"]
        require(any(value and "PerMonitorV2" in value for value in dpi_modes),
                "The executable is missing native high-DPI rendering")
        return {"architecture": "x64", "subsystem": "windows-gui",
                "requested_execution_level": "asInvoker", "ui_access": False, "dpi_awareness": "PerMonitorV2"}
    finally:
        pe.close()


def free_port():
    # The application's listening API cannot inherit a socket. Validate ownership
    # after launch as well, so a process winning this short bind race is untouched.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    require(port not in (18726, 18727), "The ephemeral port overlaps a standard application port")
    return port


def json_request(opener, base, path, *, action=None, token=None, settings=None):
    headers = {"Accept": "application/json"}
    data = None
    if action is not None:
        data = json.dumps({"action": action, **(settings or {})}).encode("utf-8")
        headers.update({"Content-Type": "application/json", "Origin": base, "X-Redbag-Key": token})
    request = Request(base + path, data=data, headers=headers)
    try:
        response = opener.open(request, timeout=1.5)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read(8 * 1024 * 1024 + 1)
        require(len(raw) <= 8 * 1024 * 1024, "Local response exceeds the verification size limit")
        status = response.code
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise VerificationError("Local service returned invalid JSON") from exc
    return status, value


def owned_state(opener, base, process, directory):
    status, value = json_request(opener, base, "/api/state")
    require(status == 200 and isinstance(value, dict), "The test service did not return its state")
    storage = value.get("storage")
    require(value.get("application_id") == APPLICATION_ID and value.get("app") == "AION2 RedBag Map"
            and value.get("process_id") == process.pid and process.poll() is None,
            "The loopback response does not belong to the retained test child")
    require(isinstance(storage, dict) and isinstance(storage.get("directory"), str)
            and Path(storage["directory"]).resolve() == directory.resolve(),
            "The test service is using an unexpected data directory")
    token = value.get("control_token")
    require(isinstance(token, str) and bool(token), "The owned test service has no control token")
    return value


def stop_owned_service(opener, base, process, directory):
    method = "already-exited"
    if process.poll() is None:
        try:
            state = owned_state(opener, base, process, directory)
            status, result = json_request(opener, base, "/api/control", action="shutdown", token=state["control_token"])
            if status == 200 and isinstance(result, dict) and result.get("ok") is True:
                method = "authenticated-shutdown"
        except (VerificationError, OSError, URLError, TimeoutError):
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            method = "terminate-owned-handle"
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                method = "kill-owned-handle"
                process.kill()
                process.wait(timeout=3)
    else:
        process.wait(timeout=1)
    return {"method": method, "exit_code": process.returncode, "exited": process.poll() is not None}


def verify(executable, report):
    require(os.name == "nt", "Run frozen-package verification on Windows")
    require(executable.is_file(), "The built executable was not found")
    expected_version = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    report["version"] = expected_version
    resource_root = ROOT / "src/aion2_redbag_map"
    map_manifest = json.loads((resource_root / "map-source.json").read_text("utf-8"))
    expected_map_images = len(map_manifest["map_assets"])
    require(expected_map_images == 339, "The map source manifest is incomplete")
    bundled_resources = executable.parent / "_internal/aion2_redbag_map"
    for name in ("maps.json", "map-source.json"):
        require((bundled_resources / name).read_bytes() == (resource_root / name).read_bytes(),
                "Bundled map resource metadata differs from the current source; rebuild the package")
    map_files = {path.relative_to(bundled_resources / "static/maps").as_posix(): path
                 for path in (bundled_resources / "static/maps").rglob("*") if path.is_file()}
    require(set(map_files) == set(map_manifest["map_assets"]), "The bundled map overview or tile set is incomplete")
    for relative, path in map_files.items():
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        require(digest == map_manifest["map_assets"][relative]["sha256"], "A bundled map image failed its source checksum")
    report["map_assets"] = {"images": expected_map_images, "overviews": sum("/" not in key for key in map_files),
                            "tiles": sum("/" in key for key in map_files), "source_checksums_match": True}
    with executable.open("rb") as stream:
        report["executable_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    report["pe"] = pe_properties(executable)
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
               "creationflags": subprocess.CREATE_NO_WINDOW}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    options["startupinfo"] = startup
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with tempfile.TemporaryDirectory(prefix="aion2-frozen-qa-") as temporary:
        workspace = Path(temporary)
        self_check_path = workspace / "self-check.json"
        completed = subprocess.run([str(executable), "--self-check", str(self_check_path)],
                                   cwd=workspace, timeout=30, check=False, **options)
        require(completed.returncode == 0 and self_check_path.is_file(), "Frozen self-check did not complete successfully")
        self_check = json.loads(self_check_path.read_text("utf-8"))
        resources = self_check.get("resources", {})
        require(self_check.get("ok") is True and self_check.get("frozen") is True
                and self_check.get("application_id") == APPLICATION_ID and self_check.get("version") == expected_version,
                "Frozen self-check identity, version, or module checks failed")
        require(self_check.get("capture_started") is False and self_check.get("gui_started") is False,
                "Frozen self-check reported collection or a GUI")
        require(resources.get("map_count") == 18 and resources.get("candidate_count") == 3721
                and resources.get("bundled_map_images") == expected_map_images, "Frozen resource counts differ from the public package")
        report["self_check"] = {"ok": True, "frozen": True, "capture_started": False, "gui_started": False}

        directory = workspace / "empty-readonly-history"
        directory.mkdir()
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        process = subprocess.Popen(
            [str(executable), "--worker", "service", "--no-capture", "--read-only", "--empty", "--no-browser",
             "--data-dir", str(directory), "--port", str(port)], cwd=workspace, **options)
        try:
            deadline = time.monotonic() + 20
            while True:
                require(process.poll() is None, "The offline test child exited before readiness")
                try:
                    state = owned_state(opener, base, process, directory)
                    break
                except (URLError, OSError, TimeoutError):
                    require(time.monotonic() < deadline, "The offline test child did not become ready")
                    time.sleep(.1)
            require(state.get("version") == expected_version, "HTTP service version differs from the expected release")
            require(state.get("mode") == "offline" and state["storage"].get("readonly") is True,
                    "The test child is not in offline read-only mode")
            require(state.get("records") == [] and state["storage"].get("record_count") == 0,
                    "The isolated test history is not empty")
            require(isinstance(state.get("maps"), list) and len(state["maps"]) == 18, "Expected 18 map entries")
            require(state.get("capture", {}).get("phase") == "paused" and state["capture"].get("packets") == 0,
                    "The offline child is not capture-free")
            status, history = json_request(opener, base, "/api/history-export")
            require(status == 200 and isinstance(history, dict) and history.get("schema") == 1
                    and history.get("records") == [] and "control_token" not in history,
                    "Empty history export failed or included a control token")
            status, candidates = json_request(opener, base, "/candidates.json")
            require(status == 200 and isinstance(candidates, dict)
                    and candidates.get("unique_count") == 3721 and candidates.get("source_count") == 3722
                    and sum(len(group.get("points", [])) for group in candidates.get("maps", {}).values()) == 3721,
                    "HTTP candidate dataset counts are incorrect")
            for relative in ("1110.webp", "1110/4_4.webp"):
                with opener.open(base + "/maps/" + relative, timeout=2) as response:
                    require(response.code == 200, "The offline service did not serve a bundled map image")
                    raw = response.read(8 * 1024 * 1024 + 1)
                require(len(raw) <= 8 * 1024 * 1024
                        and hashlib.sha256(raw).hexdigest() == map_manifest["map_assets"][relative]["sha256"],
                        "HTTP map overview or tile does not match the bundled resource")

            state = owned_state(opener, base, process, directory)
            require(state.get("sound_profile") == "chime" and state.get("sound_volume") == 35
                    and state.get("overlay_on_alert") is False, "Default alert choices are incorrect")
            with opener.open(base + "/api/alert-sound?profile=chime&volume=0", timeout=2) as response:
                sound = response.read(1024 * 1024)
                require(response.code == 200 and response.headers.get("Content-Type") == "audio/wav",
                        "The frozen service did not serve a configured sound")
            with wave.open(io.BytesIO(sound)) as wav:
                require(wav.getsampwidth() == 2 and wav.getnchannels() == 1
                        and not any(wav.readframes(wav.getnframes())), "Zero-volume audio is not silent PCM")
            choices = {"sound_profile": "bubble", "sound_volume": 12, "overlay_on_alert": True}
            status, saved = json_request(opener, base, "/api/control", action="alert_settings",
                                         token=state["control_token"], settings=choices)
            require(status == 200 and saved.get("ok") is True, "Frozen alert choices could not be saved")
            state = owned_state(opener, base, process, directory)
            require(all(state.get(key) == value for key, value in choices.items())
                    and state.get("alert_seq") == 0, "Changing choices generated an alert or lost settings")
            report["alert_settings"] = {"default_chime_volume": 35, "zero_volume_wav_verified": True,
                                        "saved_choices_verified": True, "preview_played": False}
            status, rejection = json_request(opener, base, "/api/control", action="resume", token=state["control_token"])
            require(status == 400 and isinstance(rejection, dict) and isinstance(rejection.get("error"), str),
                    "The offline test service did not reject an authenticated resume request")
            after = owned_state(opener, base, process, directory)
            require(after.get("mode") == "offline" and after["storage"].get("readonly") is True
                    and after.get("capture", {}).get("phase") == "paused" and after["capture"].get("packets") == 0,
                    "The offline service changed collection state after the rejected request")
            report["service"] = {"identity_and_version": True, "owned_child": True, "temporary_data": True,
                                 "mode": "offline", "readonly": True, "history_records": 0, "map_count": 18,
                                 "candidate_count": 3721, "candidate_source_count": 3722,
                                 "map_images": expected_map_images, "overview_and_tile_http_verified": True,
                                 "authenticated_resume_rejected": True, "captured_packets": 0,
                                 "proxy_disabled": True, "redirects_disabled": True}
        finally:
            report["cleanup"] = stop_owned_service(opener, base, process, directory)
        require(report["cleanup"]["method"] == "authenticated-shutdown" and report["cleanup"]["exit_code"] == 0,
                "The test service required forced cleanup or exited unsuccessfully")
        require(not (directory / "history.json").exists(), "The read-only check unexpectedly wrote a history file")

    expected_raw = (ROOT / "data/observations.json").read_bytes()
    bundled_raw = (executable.parent / "_internal/reference-data/observations.json").read_bytes()
    expected = json.loads(expected_raw)
    bundled = json.loads(bundled_raw)
    require(expected.get("schema") == 1 and expected.get("record_count") == len(expected.get("records", [])),
            "The source observation dataset has an invalid record count")
    require(bundled.get("schema") == 1 and bundled.get("record_count") == len(bundled.get("records", [])),
            "The bundled observation dataset has an invalid record count")
    report["reference_data"] = {"source_observations": expected["record_count"],
                                "bundled_observations": bundled["record_count"],
                                "matches_source": bundled_raw == expected_raw}
    require(bundled_raw == expected_raw, "Bundled public observations differ from the current source; rebuild the package")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", nargs="?", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="JSON result inside build/ or a temporary directory")
    args = parser.parse_args(argv)
    report_path = args.report.expanduser().resolve()
    if not (report_path.is_relative_to((ROOT / "build").resolve())
            or report_path.is_relative_to(Path(tempfile.gettempdir()).resolve())):
        parser.error("The report must be inside the project build directory or a temporary directory")
    report = {"ok": False, "checked_at_utc": datetime.now(timezone.utc).isoformat(), "application_id": APPLICATION_ID}
    try:
        verify(args.executable.expanduser().resolve(), report)
        report["ok"] = True
    except VerificationError as exc:
        report["error"] = str(exc)
    except Exception as exc:
        # Do not serialize exception values, local paths, HTTP bodies, or tokens.
        report["error"] = "Package verification failed: " + type(exc).__name__
    report_path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    report_path.write_text(raw, "utf-8")
    print(raw, end="")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
