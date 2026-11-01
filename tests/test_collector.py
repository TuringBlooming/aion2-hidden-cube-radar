from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.collector import Collector, refresh_capture_handles
from aion2_redbag_map.model import MapState
from test_player_protocol import AT, anchor, enter_map, stats, status


class FakeCapture:
    def __init__(self):
        self.lib = self
        self.pending = {}
        self.filters = {}
        self.closed = []
        self.next_handle = 1

    def devices(self):
        return [dict(name="adapter", addresses={"192.0.2.10"}, loopback=False)]

    def open(self, device, expression):
        handle = self.next_handle
        self.next_handle += 1
        self.pending[handle] = []
        self.filters[handle] = expression
        return handle, 1

    def set_filter(self, handle, expression):
        # NPF/libpcap discards an existing handle's queued packets here.
        self.pending[handle].clear()
        self.filters[handle] = expression

    def pcap_close(self, handle):
        self.closed.append(handle)
        self.pending.pop(handle)


class CaptureIsolationTests(unittest.TestCase):
    def test_unrelated_connections_cannot_discard_pending_game_state(self):
        capture, handles = FakeCapture(), {}
        game = ("198.51.100.1", 12345, "192.0.2.10", 50001)
        other = ("198.51.100.2", 443, "192.0.2.10", 50002)
        replacement = ("198.51.100.3", 443, "192.0.2.10", 50003)
        refresh_capture_handles(capture, handles, {game, other})
        game_handle = handles[("adapter", game)]["handle"]
        other_handle = handles[("adapter", other)]["handle"]
        capture.pending[game_handle].extend([b"object", b"use", b"opened"])
        refresh_capture_handles(capture, handles, {game, replacement})
        refresh_capture_handles(capture, handles, {game})
        self.assertEqual(handles[("adapter", game)]["handle"], game_handle)
        self.assertEqual(capture.pending[game_handle], [b"object", b"use", b"opened"])
        self.assertIn(other_handle, capture.closed)
        self.assertNotIn(game_handle, capture.closed)
        expression = capture.filters[game_handle]
        self.assertIn("src host 198.51.100.1 and src port 12345", expression)
        self.assertIn("dst host 192.0.2.10 and dst port 50001", expression)
        self.assertNotIn(" or ", expression)

    def test_disappearing_ownership_closes_every_stale_handle(self):
        capture, handles = FakeCapture(), {}
        game = ("198.51.100.1", 12345, "192.0.2.10", 50001)
        refresh_capture_handles(capture, handles, {game})
        handle = handles[("adapter", game)]["handle"]
        refresh_capture_handles(capture, handles, set())
        self.assertEqual(handles, {})
        self.assertEqual(capture.closed, [handle])
        self.assertEqual(capture.pending, {})


class IdleRecoveryTests(unittest.TestCase):
    """Exercise the real collector loop and decoder without any OS/network calls."""
    KEY = ("198.51.100.1", 12345, "192.0.2.10", 50001)

    def run_stream(self, schedule, duration):
        clock = {"offset": 0.0}
        current_time = lambda: AT + clock["offset"]
        trace, observations, reports = [], [], []
        records = list(schedule)

        class StopAtEnd:
            def is_set(self):
                return clock["offset"] >= duration

            def wait(self, seconds):
                clock["offset"] = round(clock["offset"] + seconds, 6)
                return self.is_set()

        class OfflineCapture:
            def __init__(self):
                self.lib = self

            def next(self, _handle):
                if records and records[0][0] <= clock["offset"]:
                    when, sequence, flags, payload = records.pop(0)
                    return AT + when, (IdleRecoveryTests.KEY, sequence, flags, payload), False
                return None

            def pcap_stats(self, *_args):
                return -1

            def pcap_close(self, *_args):
                pass

            def close(self):
                pass

        def handles(_library, opened, _connections):
            opened.setdefault("offline", {"handle": 1, "linktype": 1, "keys": {self.KEY}, "drops": 0})

        with tempfile.TemporaryDirectory(prefix="aion2-collector-test-") as directory:
            with patch("aion2_redbag_map.collector.time.time", side_effect=current_time), \
                 patch("aion2_redbag_map.collector.time.monotonic", side_effect=lambda: clock["offset"]), \
                 patch("aion2_redbag_map.collector.Npcap", OfflineCapture), \
                 patch("aion2_redbag_map.collector.game_pids", return_value={123}), \
                 patch("aion2_redbag_map.collector.game_connections", return_value={self.KEY}), \
                 patch("aion2_redbag_map.collector.refresh_capture_handles", side_effect=handles), \
                 patch("aion2_redbag_map.collector.parse_tcp", side_effect=lambda packet, _link: packet):
                model = MapState(Path(directory), [{"id": 1110, "name": "Test map"}])

                def on_status(value):
                    reports.append(dict(value))
                    trace.append(("status", value["phase"], clock["offset"]))
                    model.capture_status(value)

                def on_event(value):
                    model.handle(value)
                    trace.append(("event", value["kind"], clock["offset"]))
                    if value["kind"] == "player_position":
                        observations.append({"at": value["at"], "accepted": model.player is not None,
                                             "phase": model.capture["phase"], "map_source": model.map_source})

                collector = Collector(on_event, on_status)
                collector._stop = StopAtEnd()
                collector._run()
        self.assertFalse(records, "all synthetic packets should have been consumed")
        self.assertFalse(any(row["phase"] == "error" for row in reports), reports)
        return observations, reports, trace

    def test_idle_keeps_map_and_accepts_first_resumed_position_before_periodic_status(self):
        first = anchor() + enter_map() + stats() + stats() + status()
        # No extra heartbeat, map or stats is needed: this is the same continuous
        # TCP stream and its already-confirmed identity, resuming after 12 seconds.
        later = status(xyz=(1200.0, 2300.0, 400.0))
        observations, reports, trace = self.run_stream([
            (0, 1000, 0x18, first), (12.5, 1000 + len(first), 0x18, later)], 13)
        self.assertEqual([row["accepted"] for row in observations], [True, True])
        self.assertTrue(all(row["phase"] == "observing" and row["map_source"] == "detected" for row in observations))
        self.assertTrue(any(row["phase"] == "waiting" and row["idle_connections"] == 1 for row in reports))
        resumed = next(row for row in reports if row["idle_resumes"] == 1)
        self.assertEqual(resumed["idle_periods"], 1)
        self.assertEqual(resumed["stream_resets"], 0)
        position_index = trace.index(("event", "player_position", 12.5))
        self.assertEqual(trace[position_index - 1], ("status", "observing", 12.5))

    def test_real_fin_and_sequence_gap_still_require_a_new_map(self):
        for disconnect in ("fin", "gap"):
            with self.subTest(disconnect=disconnect):
                first = anchor() + enter_map() + stats() + stats() + status()
                cursor = 1000 + len(first)
                recovered = anchor(AT + 4) + stats() + stats() + status()
                if disconnect == "fin":
                    schedule = [(0, 1000, 0x18, first), (3, cursor, 0x11, b""),
                                (4, cursor, 0x18, recovered)]
                else:
                    cursor += 17
                    schedule = [(0, 1000, 0x18, first), (4, cursor, 0x18, recovered)]
                confirmed = enter_map() + stats() + stats() + status()
                schedule.append((8, cursor + len(recovered), 0x18, confirmed))
                observations, reports, _ = self.run_stream(schedule, 9)
                self.assertEqual([row["accepted"] for row in observations], [True, False, True])
                self.assertEqual(observations[1]["map_source"], "manual")
                self.assertTrue(any(row["stream_resets"] >= 1 for row in reports))
                if disconnect == "gap":
                    self.assertTrue(any(row["gaps"] == 1 for row in reports))


if __name__ == "__main__":
    unittest.main()
