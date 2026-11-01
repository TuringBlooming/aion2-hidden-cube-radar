"""Public dataset integrity and the explicit, offline privacy export boundary."""
import copy
import importlib.util
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("export_observations", ROOT / "scripts/export_observations.py")
EXPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORT)


def sample(**changes):
    row = {"id": "internal-record", "map_id": 1110, "x": 10.25, "y": -20.5, "z": 3,
           "status": "opened", "first_seen": 1000.5, "received_at": 1001.5,
           "last_seen": 1004.5, "opened_at": 1003.5, "refresh_anchor_at": 1004.5,
           "entity_id": 1234, "source_key": "private-connection-and-entity",
           "connection_id": "private-connection", "control_token": "private-token",
           "machine_path": "C:/Users/example/private/history.json", "source": "private-source"}
    row.update(changes)
    return row


class PublicDatasetTests(unittest.TestCase):
    def test_all_candidate_source_rows_are_retained(self):
        path = ROOT / "src/aion2_redbag_map/static/candidates.json"
        data = json.loads(path.read_text("utf-8"))
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["source_count"], 3722)
        self.assertEqual(data["unique_count"], 3721)
        self.assertEqual(data["source"]["name"], "A2Helper")
        self.assertEqual(data["source"]["coordinate_multiplier"], 100)
        self.assertEqual(data["source"]["data_path"], "refs[map_id][hidden-cube]")
        self.assertFalse(data["has_refresh_times"])
        allowed = {"id", "ordinal", "map_id", "x", "y", "z", "source_indices"}
        identifiers = set()
        total_points = total_source_rows = 0
        for key, group in data["maps"].items():
            points = group["points"]
            self.assertEqual(group["map_id"], int(key))
            self.assertEqual(group["unique_count"], len(points))
            indices = []
            coordinates = set()
            for point in points:
                self.assertEqual(set(point), allowed)
                self.assertEqual(point["map_id"], int(key))
                self.assertNotIn(point["id"], identifiers)
                identifiers.add(point["id"])
                for field in ("x", "y"):
                    self.assertIn(type(point[field]), (int, float))
                    self.assertTrue(math.isfinite(point[field]))
                self.assertIsNone(point["z"])
                coordinates.add((point["x"], point["y"], point["z"]))
                self.assertTrue(point["source_indices"])
                self.assertTrue(all(type(value) is int for value in point["source_indices"]))
                indices.extend(point["source_indices"])
            self.assertEqual(len(coordinates), len(points))
            self.assertEqual(len(indices), group["source_count"])
            self.assertEqual(len(set(indices)), group["source_count"])
            total_points += len(points)
            total_source_rows += len(indices)
        self.assertEqual(total_points, 3721)
        self.assertEqual(total_source_rows, 3722)

    def test_public_observations_have_only_the_allowed_fields(self):
        data = json.loads((ROOT / "data/observations.json").read_text("utf-8"))
        self.assertEqual(set(data), {"schema", "description", "record_count", "records"})
        self.assertEqual(data["schema"], 1)
        self.assertGreaterEqual(data["record_count"], 139)  # Preserve the initial public corpus as it grows.
        self.assertEqual(len(data["records"]), data["record_count"])
        self.assertEqual(len({row["id"] for row in data["records"]}), data["record_count"])
        for row in data["records"]:
            self.assertEqual(set(row), EXPORT.RECORD_FIELDS)
            self.assertEqual(row["source"], "local-observation")
        # The published IDs and all public values must reproduce without private input.
        self.assertEqual(EXPORT.public_dataset(data), data)


class ObservationExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "history.json"
        self.destination = self.root / "observations.json"

    def write_history(self, rows, **extra):
        raw = json.dumps({"schema": 1, "records": rows, "control_token": "root-secret",
                          "events": [{"text": "private event text"}], **extra}).encode("utf-8")
        self.source.write_bytes(raw)
        return raw

    def test_exports_every_record_and_all_public_times_without_private_fields(self):
        rows = [sample(), sample(status="unknown", opened_at=None, first_seen=2000.5),
                sample(map_id=1010, x=500)]
        raw = self.write_history(rows)
        result = EXPORT.export_observations(self.source, self.destination)
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertEqual(json.loads(self.destination.read_text("utf-8")), result)
        self.assertEqual(result["record_count"], len(rows))
        for original, public in zip(rows, result["records"]):
            self.assertEqual(set(public), EXPORT.RECORD_FIELDS)
            self.assertEqual({key: public[key] for key in EXPORT.PUBLIC_FIELDS},
                             {key: original[key] for key in EXPORT.PUBLIC_FIELDS})
        for secret in ("root-secret", "private-connection", "private-token", "C:/Users/", "private event"):
            self.assertNotIn(secret, self.destination.read_text("utf-8"))

    def test_private_metadata_does_not_change_stable_ids_or_mutate_input(self):
        first = {"schema": 1, "records": [sample()]}
        original = copy.deepcopy(first)
        second = copy.deepcopy(first)
        second["records"][0].update(id="different-private-id", entity_id=999,
                                     connection_id="different-private-connection", control_token="different-secret")
        self.assertEqual(EXPORT.public_dataset(first), EXPORT.public_dataset(second))
        self.assertEqual(first, original)

    def test_duplicate_coordinates_cycles_and_identical_rows_are_all_kept(self):
        rows = [sample(), sample(), sample(first_seen=2000, last_seen=2010)]
        data = EXPORT.public_dataset({"schema": 1, "records": rows})
        self.assertEqual(data["record_count"], 3)
        ids = [row["id"] for row in data["records"]]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(ids[1], ids[0] + "-2")
        self.assertEqual(EXPORT.public_dataset(data), data)

    def test_missing_historical_times_stay_null(self):
        row = {key: value for key, value in sample().items() if key not in EXPORT.TIMESTAMP_FIELDS}
        result = EXPORT.public_dataset({"schema": 1, "records": [row]})
        self.assertTrue(all(result["records"][0][key] is None for key in EXPORT.TIMESTAMP_FIELDS))

    def test_bad_records_fail_with_index_and_leave_existing_output_unchanged(self):
        self.destination.write_bytes(b"existing public export")
        invalid = [None, [], sample(map_id=True), sample(map_id=0), sample(x=True), sample(y="20"),
                   sample(z=None), sample(status="unexpected"), sample(last_seen=-1),
                   sample(opened_at="yesterday"), sample(refresh_anchor_at=True)]
        for bad in invalid:
            with self.subTest(bad_type=type(bad).__name__):
                raw = self.write_history([sample(), bad])
                with self.assertRaisesRegex(EXPORT.ExportError, "Record 2"):
                    EXPORT.export_observations(self.source, self.destination)
                self.assertEqual(self.source.read_bytes(), raw)
                self.assertEqual(self.destination.read_bytes(), b"existing public export")
        self.assertFalse(list(self.root.glob(".observations-*")))

    def test_bad_json_or_root_schema_never_produces_output(self):
        invalid = [b'{"schema":1,"records":[', b'{"schema":1,"records":[],"records":[]}',
                   b'{"schema":1,"records":[],"extra":NaN}', b'\xff',
                   b'{"schema":true,"records":[]}', b'{"schema":2,"records":[]}',
                   b'{"schema":1,"records":{}}', b'{"schema":1,"records":[],"record_count":2}']
        for raw in invalid:
            with self.subTest(raw=raw[:35]):
                self.source.write_bytes(raw)
                with self.assertRaises(EXPORT.ExportError):
                    EXPORT.export_observations(self.source, self.destination)
                self.assertEqual(self.source.read_bytes(), raw)
                self.assertFalse(self.destination.exists())

    def test_nonfinite_coordinates_and_times_are_rejected(self):
        for field in ("x", "y", "z", *EXPORT.TIMESTAMP_FIELDS):
            for value in (math.nan, math.inf, -math.inf, 10 ** 400):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(EXPORT.ExportError, "Record 1"):
                        EXPORT.public_dataset({"schema": 1, "records": [sample(**{field: value})]})

    def test_output_cannot_be_the_input_or_a_hardlink_alias(self):
        raw = self.write_history([sample()])
        with self.assertRaisesRegex(EXPORT.ExportError, "must not refer"):
            EXPORT.export_observations(self.source, self.source.parent / "." / self.source.name)
        os.link(self.source, self.destination)
        with self.assertRaisesRegex(EXPORT.ExportError, "must not refer"):
            EXPORT.export_observations(self.source, self.destination)
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertEqual(self.destination.read_bytes(), raw)

    def test_failed_replace_preserves_both_files_and_removes_temporary_copy(self):
        raw = self.write_history([sample()])
        self.destination.write_bytes(b"previous export")
        with patch.object(EXPORT.os, "replace", side_effect=PermissionError("destination busy")):
            with self.assertRaises(PermissionError):
                EXPORT.export_observations(self.source, self.destination)
        self.assertEqual(self.source.read_bytes(), raw)
        self.assertEqual(self.destination.read_bytes(), b"previous export")
        self.assertFalse(list(self.root.glob(".observations-*")))


if __name__ == "__main__":
    unittest.main()
