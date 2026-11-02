"""Build an inspectable Windows onedir package and a clean source archive.

No publishing, signing, driver installation, network access or game collection.
Run using the isolated environment from requirements-build.txt.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = "AION2 Hidden Cube Radar"
SOURCE_DIRS = ("src", "tests", "scripts", "packaging", "docs", ".github")
PUBLIC_DATA = ("observations.json", "provenance.json")
SOURCE_FILES = ("README.md", "README.en.md", "CONTRIBUTING.md", "LICENSE",
                "THIRD_PARTY_NOTICES.md", "pyproject.toml", "requirements.txt",
                "requirements-build.txt", ".gitignore", ".gitattributes")
SKIP_PARTS = {"__pycache__", "private", ".venv", ".git", "assets", "backups"}
BAD_EXTENSIONS = {".cmd", ".bat", ".ps1", ".pcap", ".pcapng", ".log", ".lock", ".pyc"}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def git_revision():
    git = shutil.which("git")
    if git is None:
        return None
    result = subprocess.run([git, "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    return result.stdout.strip() if result.returncode == 0 else None


def source_files():
    paths = [ROOT / name for name in SOURCE_FILES]
    paths.extend(ROOT / "data" / name for name in PUBLIC_DATA)
    for name in SOURCE_DIRS:
        paths.extend(p for p in (ROOT / name).rglob("*") if p.is_file())
    return sorted(p for p in paths if not (set(p.relative_to(ROOT).parts) & SKIP_PARTS)
                  and p.suffix.lower() not in BAD_EXTENSIONS
                  and not any(part.endswith(".egg-info") for part in p.parts))


def icon(path):
    # Original geometric bag artwork, generated from this source; no external asset.
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (256, 256))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((14, 14, 242, 242), radius=52, fill="#172C43")
    draw.rounded_rectangle((86, 48, 170, 111), radius=25, outline="#FFD79B", width=12)
    draw.rounded_rectangle((57, 89, 199, 208), radius=32, fill="#CE4545")
    draw.rounded_rectangle((79, 145, 177, 190), radius=12, fill="#F26959")
    draw.line((88, 157, 167, 157), fill="#FFD79B", width=8)
    image.save(path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def copy_licenses(target):
    target.mkdir(parents=True, exist_ok=True)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, target / name)
    base = Path(sys.base_prefix)
    shutil.copy2(base / "LICENSE.txt", target / "Python-LICENSE.txt")
    shutil.copy2(base / "tcl/tk8.6/license.terms", target / "Tcl-Tk-license.terms")
    for name in ("Pillow", "PyInstaller"):
        distribution = metadata.distribution(name)
        found = False
        for entry in distribution.files or ():
            if ".dist-info/licenses/" in str(entry).replace("\\", "/"):
                source = Path(distribution.locate_file(entry))
                if source.is_file():
                    shutil.copy2(source, target / f"{name}-{source.name}")
                    found = True
        if not found:
            raise RuntimeError(f"Missing installed license for {name}")


def check_package(folder):
    if {p.name for p in folder.iterdir()} != {NAME + ".exe", "_internal"}:
        raise RuntimeError("The package root must contain only the app and _internal")
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        if (path.suffix.lower() in BAD_EXTENSIONS
                or path.name.lower() in {"wpcap.dll", "packet.dll", "npcap.sys", "history.json", "seed.json"}
                or (path.suffix.lower() == ".exe" and path.parent != folder)):
            raise RuntimeError(f"Unexpected distribution file: {path.relative_to(folder)}")
    resources = folder / "_internal/aion2_redbag_map"
    data = json.loads((resources / "static/candidates.json").read_text("utf-8"))
    # Exact dataset bytes must survive packaging; the schema itself is checked in tests.
    if digest(resources / "static/candidates.json") != digest(ROOT / "src/aion2_redbag_map/static/candidates.json"):
        raise RuntimeError("Candidate dataset changed during packaging")
    if not data:
        raise RuntimeError("Missing candidate points")
    for name in ("maps.json", "map-source.json"):
        if digest(resources / name) != digest(ROOT / "src/aion2_redbag_map" / name):
            raise RuntimeError(f"Map resource metadata changed during packaging: {name}")
    map_manifest = json.loads((resources / "map-source.json").read_text("utf-8"))
    map_files = {path.relative_to(resources / "static/maps").as_posix(): path
                 for path in (resources / "static/maps").rglob("*") if path.is_file()}
    if set(map_files) != set(map_manifest["map_assets"]):
        raise RuntimeError("The bundled map overview or tile set is incomplete")
    for relative, path in map_files.items():
        if digest(path) != map_manifest["map_assets"][relative]["sha256"]:
            raise RuntimeError(f"Map image changed during packaging: {relative}")
    if {p.name for p in (folder / "_internal/reference-data").iterdir()} != set(PUBLIC_DATA):
        raise RuntimeError("Unexpected public data file")
    for name in PUBLIC_DATA:
        if digest(folder / "_internal/reference-data" / name) != digest(ROOT / "data" / name):
            raise RuntimeError(f"Missing public dataset: {name}")


def zip_tree(folder, destination):
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, (Path(folder.name) / path.relative_to(folder)).as_posix())


def main():
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12):
        raise SystemExit("Build on Windows with 64-bit Python 3.12 including Tkinter.")
    import struct
    if struct.calcsize("P") != 8:
        raise SystemExit("64-bit Python is required.")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    dist, build = ROOT / "dist", ROOT / "build"
    if not all(path.resolve().is_relative_to(ROOT.resolve()) for path in (dist, build)):
        raise RuntimeError("Build directories must remain inside this project")
    dist.mkdir(exist_ok=True)
    build.mkdir(exist_ok=True)
    output = dist / NAME
    if output.exists():
        # Preserve earlier builds; never remove a potentially running directory.
        previous = build / f"previous-{datetime.now():%Y%m%d-%H%M%S-%f}"
        if (output.is_symlink() or not output.resolve().is_relative_to(dist.resolve())
                or not previous.resolve().is_relative_to(build.resolve())):
            raise RuntimeError("Previous build paths escaped this project")
        output.rename(previous)
    icon_path = build / "redbag.ico"
    icon(icon_path)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed", "--noupx",
               "--name", NAME, "--distpath", str(dist), "--workpath", str(build / "pyinstaller"),
               "--specpath", str(build), "--paths", str(ROOT / "src"),
               "--manifest", str(ROOT / "packaging/app.manifest"),
               "--version-file", str(ROOT / "packaging/version-info.txt"), "--icon", str(icon_path),
               "--collect-submodules", "aion2_redbag_map", "--hidden-import", "PIL.ImageTk",
               "--add-data", f"{icon_path}:aion2_redbag_map",
               "--add-data", f"{ROOT / 'src/aion2_redbag_map/static'}:aion2_redbag_map/static",
               "--add-data", f"{ROOT / 'src/aion2_redbag_map/maps.json'}:aion2_redbag_map",
               "--add-data", f"{ROOT / 'src/aion2_redbag_map/map-source.json'}:aion2_redbag_map",
               str(ROOT / "packaging/entry.py")]
    subprocess.run(command, cwd=ROOT, check=True)
    internal = output / "_internal"
    copy_licenses(internal / "licenses")
    reference = internal / "reference-data"
    reference.mkdir(exist_ok=True)
    for path in (ROOT / "data" / name for name in PUBLIC_DATA):
        shutil.copy2(path, reference / path.name)
    files = source_files()
    for path in files:
        destination = internal / "source" / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    manifest = {path.relative_to(ROOT).as_posix(): digest(path) for path in files}
    source_digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    report = {"application": NAME, "version": version, "built_at_utc": datetime.now(timezone.utc).isoformat(),
              "python": sys.version.split()[0], "platform": "Windows x64", "mode": "onedir/windowed/no UPX/asInvoker",
              "code_signed": False, "source_sha256": source_digest,
              "git_commit": git_revision(),
              "dependencies": {d.metadata["Name"]: d.version for d in metadata.distributions()
                               if d.metadata["Name"].lower() not in {"aion2-hidden-cube-radar", "aion2-redbag-map", "pip"}},
              "source_files": manifest}
    (internal / "build-info.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    check_package(output)
    checksums = [f"{digest(path)}  {path.relative_to(output).as_posix()}" for path in sorted(output.rglob("*")) if path.is_file()]
    (internal / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", "utf-8")
    binary_zip = dist / f"AION2-Hidden-Cube-Radar-{version}-windows-x64.zip"
    source_zip = dist / f"aion2-hidden-cube-radar-{version}-source.zip"
    zip_tree(output, binary_zip)
    with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            archive.write(path, (Path(f"aion2-hidden-cube-radar-{version}") / path.relative_to(ROOT)).as_posix())
    (dist / "SHA256SUMS.txt").write_text("\n".join(f"{digest(path)}  {path.name}" for path in (binary_zip, source_zip)) + "\n", "utf-8")
    print(json.dumps({"binary": str(binary_zip), "source": str(source_zip), "source_sha256": source_digest}, indent=2))


if __name__ == "__main__":
    main()
