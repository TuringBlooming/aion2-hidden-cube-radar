from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.collector import refresh_capture_handles


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


if __name__ == "__main__":
    unittest.main()
