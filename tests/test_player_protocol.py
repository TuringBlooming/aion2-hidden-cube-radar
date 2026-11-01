"""Reference-position observations, never an assumption of continuous movement."""
from collections import Counter
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.protocol import FrameStream, TcpStream, decode_frame, parse_tcp, read_pcap

AT = 1793516260.0
SELF = 17341
OTHER = 92087
XYZ = (106518.4375, 71755.4140625, 4347.0)
POSITION_KEYS = {"kind", "x", "y", "z", "at", "connection_id", "source",
                 "source_opcode", "identity_source"}


def vi(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def frame(opcode, body):
    return vi(len(body) + 6) + struct.pack("<H", opcode) + body


def anchor(at=AT):
    return b"".join(frame(0x3600, struct.pack("<q", int(at * 1000) + n * 50)) for n in range(3))


def stats(key=SELF, values=()):
    body = vi(key) + vi(len(values))
    body += b"".join(struct.pack("<Hi", kind, value) for kind, value in values)
    return frame(0x364A, body + bytes(8))


def status_body(target=SELF, entries=None, changed=False):
    """Encode bool bits across records, as opposed to padding each bool."""
    entries = entries if entries is not None else [{}]
    body = bytearray(vi(target) + (b"" if changed else vi(len(entries))))
    bit_index = 0
    bit_byte = None

    def boolean():
        nonlocal bit_index, bit_byte
        if bit_index == 0:
            bit_byte = len(body)
            body.append(0)
        body[bit_byte] |= 1 << bit_index
        bit_index = (bit_index + 1) % 8

    for n, entry in enumerate(entries):
        flags = entry.get("flags", 0x11)
        body.append(flags)
        body.extend(vi(n + 100))
        body.extend(struct.pack("<iqq", 40001, 2000, 3000))
        if flags & 1:
            body.extend(vi(entry.get("caster", SELF)))
        body.append(1)
        if flags & 2:
            body.extend(struct.pack("<i", 50001))
        boolean()
        if flags & 4:
            body.extend(vi(2))
        if flags & 8:
            body.extend(struct.pack("<i", 4))
        if flags & 16:
            body.extend(struct.pack("<fff", *entry.get("xyz", XYZ)))
    if changed:
        boolean()
    return bytes(body)


def status(target=SELF, caster=SELF, xyz=XYZ, changed=False):
    return frame(0x382B if changed else 0x382A,
                 status_body(target, [{"caster": caster, "xyz": xyz}], changed))


def bundle(raw):
    length = len(raw)
    block = bytearray([min(length, 15) << 4])
    if length >= 15:
        extra = length - 15
        while extra >= 255:
            block.append(255)
            extra -= 255
        block.append(extra)
    return frame(0xFFFF, struct.pack("<I", length) + bytes(block) + raw)


def red_object(key):
    body = vi(key) + bytes(2) + struct.pack("<Ifff", 1301199, *XYZ)
    return frame(0x3634, body + bytes(8) + bytes((1, 1, 6)) + struct.pack("<q", int(AT * 1000)))


def enter_map():
    return frame(0x3621, bytes(4) + struct.pack("<i", 1110) + bytes(38))


class PlayerProtocolTests(unittest.TestCase):
    def stream(self, connection="player-test"):
        self.events = []
        decoder = FrameStream(connection, self.events.append)
        decoder.feed(anchor(), AT)
        return decoder

    def positions(self):
        return [e for e in self.events if e["kind"] == "player_position"]

    def test_requires_two_consistent_stats_and_does_not_replay_old_positions(self):
        decoder = self.stream()
        decoder.feed(status() + stats() + status(), AT)
        self.assertEqual(self.events, [])
        decoder.feed(stats(values=((2, 300),)), AT + 1)
        self.assertEqual(self.events, [])
        decoder.feed(status(), AT + 2)
        self.assertEqual(len(self.positions()), 1)
        self.assertEqual(self.positions()[0], dict(
            kind="player_position", x=XYZ[0], y=XYZ[1], z=XYZ[2], at=AT + 2,
            connection_id="player-test", source="self_status", source_opcode="0x382a",
            identity_source="stats_consensus"))

    def test_both_target_and_caster_must_match_and_private_fields_do_not_escape(self):
        decoder = self.stream()
        decoder.feed(stats() + stats() + status(OTHER, SELF) + status(SELF, OTHER)
                     + status(OTHER, OTHER) + status(changed=True), AT)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(set(self.events[0]), POSITION_KEYS)
        self.assertEqual(self.events[0]["source_opcode"], "0x382b")
        self.assertNotIn(str(SELF), repr(self.events))
        self.assertNotIn(str(OTHER), repr(self.events))
        self.assertEqual(decode_frame(0x364A, vi(SELF) + b"\0" + bytes(8)), [])
        self.assertEqual(decode_frame(0x382A, status_body()), [])

    def test_interleaved_stats_do_not_combine_votes_across_keys_or_connections(self):
        decoder = self.stream()
        decoder.feed(stats() + stats(OTHER) + stats() + status(), AT)
        self.assertEqual(self.events, [])
        separate = FrameStream("different-connection", self.events.append)
        separate.feed(anchor() + stats() + status(), AT)
        self.assertEqual(self.events, [])
        decoder.feed(stats() + status(), AT + 1)
        self.assertEqual(len(self.positions()), 1)

    def test_identity_change_within_bundle_clears_old_key_before_new_positions(self):
        decoder = self.stream()
        decoder.feed(stats() + stats(), AT)
        new_xyz = (XYZ[0] + 10, XYZ[1], XYZ[2])
        raw = (status() + stats(OTHER) + status() + status(OTHER, OTHER)
               + stats(OTHER) + status(OTHER, OTHER, new_xyz) + status())
        decoder.feed(bundle(raw), AT + 1)
        self.assertEqual([e["kind"] for e in self.events],
                         ["player_position", "player_position_reset", "player_position"])
        self.assertEqual([e["x"] for e in self.positions()], [XYZ[0], new_xyz[0]])
        self.assertEqual(self.events[1]["reason"], "identity_changed")
        self.assertEqual({e["at"] for e in self.events}, {AT + 1})
        self.assertNotIn(str(SELF), repr(self.events))
        self.assertNotIn(str(OTHER), repr(self.events))

    def test_packed_bool_cursor_spans_optional_fields_and_nine_list_entries(self):
        decoder = self.stream()
        decoder.feed(stats() + stats(), AT)
        entries = [{"flags": 0x1F, "xyz": (XYZ[0] + i, XYZ[1], XYZ[2])} for i in range(9)]
        decoder.feed(frame(0x382A, status_body(entries=entries)), AT + 1)
        decoder.feed(frame(0x382B, status_body(entries=[{"flags": 0x1F}], changed=True)), AT + 2)
        self.assertEqual(len(self.positions()), 10)
        self.assertEqual([e["x"] for e in self.positions()[:9]], [XYZ[0] + i for i in range(9)])
        self.assertEqual(decoder.counters["malformed"], 0)

    def test_missing_caster_missing_position_and_zero_origin_are_not_observations(self):
        decoder = self.stream()
        decoder.feed(stats() + stats(), AT)
        for entry in ({"flags": 16}, {"flags": 1}, {"xyz": (0, 0, 0)}, {"xyz": (0, 0, 20)}):
            decoder.feed(frame(0x382A, status_body(entries=[entry])), AT)
        self.assertEqual(self.events, [])

    def test_malformed_status_fields_suspend_positions_without_interrupting_stream(self):
        valid = status_body(entries=[{}, {}])
        bad = [valid[:-1], valid + b"\0", vi(SELF) + vi(0xFFFFFFFF),
               status_body(entries=[{"flags": 0x91}]),
               status_body(entries=[{}, {"xyz": (float("nan"), 1, 2)}]),
               status_body(entries=[{}, {"xyz": (1, float("inf"), 2)}]),
               status_body(entries=[{}, {"xyz": (1, 2, 1_000_001)}])]
        for body in bad:
            with self.subTest(body_size=len(body)):
                decoder = self.stream()
                decoder.feed(stats() + stats() + frame(0x382A, body), AT)
                self.assertEqual([e["kind"] for e in self.events], ["player_position_reset"])
                self.assertEqual(self.events[0]["reason"], "unsupported_status_layout")
                self.assertEqual(decoder.counters["player_malformed"], 1)
                self.assertEqual(decoder.counters["malformed"], 0)
                self.assertTrue(decoder.synced)
                decoder.feed(stats() + stats() + status(), AT + 1)
                self.assertEqual(self.positions(), [])

    def test_malformed_stats_do_not_establish_consensus(self):
        bodies = [vi(SELF) + b"\0" + bytes(7), vi(SELF) + b"\0" + bytes(9),
                  vi(SELF) + vi(0xFFFFFFFF), b"\0\0" + bytes(8), b"\x80"]
        for body in bodies:
            with self.subTest(body_size=len(body)):
                decoder = self.stream()
                decoder.feed(stats() + frame(0x364A, body), AT)
                decoder.feed(stats() + stats() + status(), AT + 1)
                self.assertEqual(self.positions(), [])
                self.assertEqual(decoder.counters["player_malformed"], 1)
                self.assertEqual(decoder.counters["malformed"], 0)

    def test_malformed_nested_bundle_applies_neither_votes_nor_positions(self):
        decoder = self.stream()
        decoder.feed(stats(), AT)
        raw = stats() + status() + frame(0x382B, status_body(changed=True)[:-1])
        decoder.feed(bundle(bundle(raw)), AT + 1)
        self.assertEqual([e["kind"] for e in self.events], ["player_position_reset"])
        decoder.feed(stats() + stats() + status(), AT + 2)
        self.assertEqual(self.positions(), [])
        decoder.feed(enter_map() + stats() + stats() + status(), AT + 3)
        self.assertEqual(len(self.positions()), 1)

    def test_bad_player_layout_keeps_independent_red_bags_and_states_in_valid_bundle(self):
        decoder = self.stream()
        decoder.feed(stats() + stats() + red_object(1), AT)
        raw = (status() + stats(OTHER) + stats(OTHER)
               + status(OTHER, OTHER) + red_object(2)
               + bundle(frame(0x382B, status_body(changed=True)[:-1])))
        decoder.feed(bundle(raw), AT + 1)
        decoder.feed(frame(0x3635, vi(1) + bytes((4, 3, 0))), AT + 2)
        self.assertEqual([e["kind"] for e in self.events],
                         ["object", "object", "player_position_reset", "state"])
        self.assertEqual([e["entity_id"] for e in self.events if e["kind"] == "object"], [1, 2])
        self.assertEqual(self.positions(), [])
        self.assertTrue(decoder.synced)
        self.assertEqual(decoder.counters["malformed"], 0)
        self.assertEqual(decoder.counters["player_malformed"], 1)

    def test_broken_nested_frame_boundary_remains_an_atomic_stream_failure(self):
        decoder = self.stream()
        decoder.feed(stats(), AT)
        raw = stats() + status() + red_object(1) + vi(2000) + b"\x2a\x38"
        decoder.feed(bundle(bundle(raw)), AT + 1)
        self.assertEqual([e["kind"] for e in self.events], ["disconnect"])
        self.assertEqual(decoder.counters["malformed"], 1)
        self.assertEqual(decoder.counters["player_malformed"], 0)
        decoder.feed(anchor() + stats() + status(), AT + 2)
        self.assertEqual(self.positions(), [])

    def test_enter_map_and_disconnect_clear_identity_but_browsing_does_not(self):
        decoder = self.stream()
        decoder.feed(stats() + stats() + frame(0x9101, struct.pack("<Hi", 0, 1110)) + status(), AT)
        self.assertEqual(len(self.positions()), 1)
        decoder.feed(enter_map() + status() + stats() + status(), AT + 1)
        self.assertEqual(len(self.positions()), 1)
        decoder.feed(stats() + status(), AT + 2)
        self.assertEqual(len(self.positions()), 2)
        decoder.invalidate(AT + 3, "test reset")
        decoder.feed(anchor() + status() + stats() + status(), AT + 4)
        self.assertEqual(len(self.positions()), 2)

    def test_known_movement_warp_and_self_spawn_are_not_enabled(self):
        decoder = self.stream()
        decoder.feed(stats() + stats(), AT)
        for opcode in (0x371A, 0x371B, 0x371C, 0x373C, 0x3633):
            decoder.feed(frame(opcode, vi(SELF) + bytes(2) + struct.pack("<fff", *XYZ)), AT)
        self.assertEqual(self.events, [])

    def test_tcp_retransmission_does_not_count_as_second_identity_vote(self):
        self.events = []
        tcp = TcpStream("retransmit", self.events.append)
        first = anchor() + stats()
        tcp.feed(1000, first, 0x18, AT)
        tcp.feed(1000, first, 0x18, AT + .1)
        probe = status()
        offset = 1000 + len(first)
        tcp.feed(offset, probe, 0x18, AT + .2)
        self.assertEqual(self.events, [])
        tail = stats() + status()
        offset += len(probe)
        for n, byte in enumerate(tail):
            tcp.feed(offset + n, bytes([byte]), 0x18, AT + 1)
        tcp.feed(offset, tail, 0x18, AT + 2)
        self.assertEqual(len(self.positions()), 1)
        self.assertGreater(tcp.counters["retransmitted_bytes"], 0)

    def test_tcp_gap_and_fin_remove_identity_until_new_consensus(self):
        self.events = []
        tcp = TcpStream("gap", self.events.append)
        first = anchor() + stats() + stats() + status()
        tcp.feed(1000, first, 0x18, AT)
        after = anchor(AT + 1) + status() + stats() + status()
        start = 1000 + len(first) + 17
        tcp.feed(start, after, 0x18, AT + 1)
        tcp.tick(AT + 3.1)
        self.assertEqual(len(self.positions()), 1)
        self.assertEqual(tcp.counters["gaps"], 1)
        tail = stats() + status()
        tcp.feed(start + len(after), tail, 0x18, AT + 4)
        self.assertEqual(len(self.positions()), 2)
        tcp.feed(start + len(after) + len(tail), b"", 0x11, AT + 5)
        self.assertEqual(self.events[-1]["kind"], "disconnect")
        tcp.decoder.feed(anchor() + status(), AT + 6)
        self.assertEqual(len(self.positions()), 2)

    def replay_private_sample(self, filename, source_port, destination_port):
        path = Path(__file__).resolve().parent / "private" / filename
        if not path.exists():
            self.skipTest("Private diagnostic PCAP is intentionally excluded from the distribution")
        self.events = []
        tcp = TcpStream("offline-fixture", self.events.append)
        for at, packet, linktype, truncated in read_pcap(path):
            self.assertFalse(truncated)
            parsed = parse_tcp(packet, linktype)
            if parsed is not None:
                key, sequence, flags, payload = parsed
                if key[1] == source_port and key[3] == destination_port:
                    tcp.feed(sequence, payload, flags, at)
        return tcp

    def test_real_reference_position_capture(self):
        tcp = self.replay_private_sample("movement-20261101-walking-current.pcap", 63717, 52563)
        positions = self.positions()
        # Earlier candidate observations are deliberately not replayed after
        # the second consistent stats message establishes identity.
        self.assertEqual(len(positions), 20)
        self.assertEqual(len({(e["x"], e["y"], e["z"]) for e in positions}), 10)
        self.assertEqual(Counter(e["source_opcode"] for e in positions), {"0x382a": 11, "0x382b": 9})
        self.assertAlmostEqual(positions[0]["at"], 1793516288.874051, places=5)
        self.assertAlmostEqual(positions[-1]["at"], 1793516297.127556, places=5)
        self.assertTrue(all(set(e) == POSITION_KEYS for e in positions))
        self.assertEqual(tcp.counters["gaps"], 0)
        self.assertEqual(tcp.counters["malformed"], 0)

    def test_old_capture_has_no_unconfirmed_player_position(self):
        tcp = self.replay_private_sample("red-focus-01.pcap", 54536, 53238)
        self.assertEqual(self.positions(), [])
        self.assertEqual(len([e for e in self.events if e["kind"] == "object"]), 1)
        self.assertEqual(tcp.counters["malformed"], 0)


if __name__ == "__main__":
    unittest.main()
