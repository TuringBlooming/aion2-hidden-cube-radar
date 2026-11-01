"""Export an explicitly selected local history to a public, allowlisted dataset.

No capture, upload, service access, or automatic history discovery is performed.
Every input record is preserved, including duplicate coordinates and old cycles.
Missing historical timestamps remain null; no observation times are invented.
IDs hash only the ten public fields, matching data/observations.json. Exact
duplicate public rows receive deterministic occurrence suffixes instead of being
dropped. Changing private fields never changes an exported observation ID.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile


PUBLIC_FIELDS = (
    "map_id", "x", "y", "z", "status", "first_seen", "received_at",
    "last_seen", "opened_at", "refresh_anchor_at",
)
TIMESTAMP_FIELDS = PUBLIC_FIELDS[5:]
RECORD_FIELDS = frozenset((*PUBLIC_FIELDS, "id", "source"))
STATUSES = frozenset(("seen", "used", "opened", "unknown"))
DESCRIPTION = (
    "Historical observations shared by the project author. "
    "Not current availability or guaranteed respawn data."
)


class ExportError(ValueError):
    """Invalid input or an unsafe destination; nothing should be published."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExportError("Input contains a duplicate JSON object key")
        result[key] = value
    return result


def _constant(_value):
    raise ExportError("Input contains a non-finite JSON number")


def _number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def public_dataset(history):
    """Validate the complete snapshot before returning any public records."""
    if not isinstance(history, dict) or type(history.get("schema")) is not int or history["schema"] != 1:
        raise ExportError("Input must be a schema 1 history object")
    rows = history.get("records")
    if not isinstance(rows, list):
        raise ExportError("Input records must be a JSON array")
    if "record_count" in history and (
        type(history["record_count"]) is not int or history["record_count"] != len(rows)
    ):
        raise ExportError("Input record_count does not match the records array")

    records = []
    occurrences = {}
    for index, row in enumerate(rows, 1):
        prefix = f"Record {index}"
        if not isinstance(row, dict):
            raise ExportError(f"{prefix} must be a JSON object")
        if type(row.get("map_id")) is not int or row["map_id"] <= 0:
            raise ExportError(f"{prefix}: map_id must be a positive integer")
        for field in ("x", "y", "z"):
            if not _number(row.get(field)):
                raise ExportError(f"{prefix}: {field} must be a finite number")
        if not isinstance(row.get("status"), str) or row["status"] not in STATUSES:
            raise ExportError(f"{prefix}: status must be seen, used, opened, or unknown")
        for field in TIMESTAMP_FIELDS:
            value = row.get(field)
            if value is not None and (not _number(value) or value < 0):
                raise ExportError(f"{prefix}: {field} must be a nonnegative timestamp or null")

        clean = {field: row.get(field) for field in PUBLIC_FIELDS}
        # Keep default separators for compatibility with the first public export.
        canonical = json.dumps(clean, sort_keys=True, allow_nan=False).encode("utf-8")
        identity = "observation-" + hashlib.sha256(canonical).hexdigest()[:16]
        previous = occurrences.get(identity)
        if previous is None:
            occurrences[identity] = (canonical, 1)
        else:
            old_canonical, count = previous
            if old_canonical != canonical:
                raise ExportError(f"{prefix}: observation ID collision")
            count += 1
            occurrences[identity] = (canonical, count)
            identity += f"-{count}"
        clean.update(id=identity, source="local-observation")
        records.append(clean)

    return {"schema": 1, "description": DESCRIPTION,
            "record_count": len(records), "records": records}


def _separate_paths(source, destination):
    if source == destination or (destination.exists() and source.samefile(destination)):
        raise ExportError("Output must not refer to the input history file")


def export_observations(input_path, output_path):
    """Read one explicit history and atomically write its sanitized public copy."""
    source = Path(input_path).expanduser().resolve()
    destination = Path(output_path).expanduser().resolve()
    _separate_paths(source, destination)
    try:
        history = json.loads(source.read_text("utf-8-sig"), object_pairs_hook=_object,
                             parse_constant=_constant)
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ExportError):
            raise
        raise ExportError("Input cannot be decoded as supported UTF-8 JSON") from exc
    result = public_dataset(history)
    raw = (json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".observations-", suffix=".tmp",
                                         dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _separate_paths(source, destination)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Existing local history JSON to export")
    parser.add_argument("--output", required=True, type=Path, help="Separate public observation JSON destination")
    args = parser.parse_args(argv)
    try:
        result = export_observations(args.input, args.output)
    except (ExportError, OSError) as exc:
        parser.error(str(exc))
    print(f"Exported {result['record_count']} observations. No data was uploaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
