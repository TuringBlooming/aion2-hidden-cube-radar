import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.collector import replay_pcap
from aion2_redbag_map.protocol import FrameStream, TcpStream, decode_frame, decompress_lz4, parse_tcp, read_pcap

AT = 1792749938.0


def vi(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def frame(opcode, body):
    return vi(len(body) + 6) + struct.pack("<H", opcode) + body


def anchor(at=AT):
    return b"".join(frame(0x3600, struct.pack("<q", int(at * 1000) + i * 50)) for i in range(3))


def object_frame(entity=42895, template=1301199):
    body = vi(entity) + bytes(2) + struct.pack("<Ifffff", template, 106518.4375, 71755.4140625, 4347, 0, 0)
    return frame(0x3634, body + bytes((1, 1, 6)) + struct.pack("<q", 1792749938682))


def literal_block(raw):
    length = len(raw)
    result = bytearray([min(length, 15) << 4])
    if length >= 15:
        extra = length - 15
        while extra >= 255:
            result.append(255)
            extra -= 255
        result.append(extra)
    return bytes(result) + raw


class ProtocolTests(unittest.TestCase):
    def stream(self):
        self.events = []
        self.tcp = TcpStream("test:1", self.events.append)
        return self.tcp

    def test_byte_fragmentation(self):
        tcp = self.stream()
        data = anchor() + object_frame()
        for offset, byte in enumerate(data):
            tcp.feed(1000 + offset, bytes([byte]), 0x18, AT + offset / 1000)
        self.assertEqual([e["kind"] for e in self.events], ["object"])
        self.assertEqual(self.events[0]["x"], 106518.4375)
        self.assertEqual(self.events[0]["spawned_at"], 1792749938682)

    def test_retransmit_overlap_and_out_of_order(self):
        tcp = self.stream()
        start = anchor()
        tcp.feed(1000, start, 0x18, AT)
        offset = 1000 + len(start)
        obj = object_frame()
        tcp.feed(offset, obj[:7], 0x18, AT + .1)
        tcp.feed(offset + 20, obj[20:], 0x18, AT + .2)
        self.assertFalse(self.events)
        tcp.feed(offset + 5, obj[5:23], 0x18, AT + .3)
        tcp.feed(offset, obj, 0x18, AT + .4)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["kind"], "object")
        self.assertEqual(tcp.counters["gaps"], 0)
        self.assertGreater(tcp.counters["retransmitted_bytes"], 0)

    def test_sequence_number_wrap(self):
        tcp = self.stream()
        data = anchor() + object_frame()
        for offset in range(0, len(data), 9):
            tcp.feed((0xFFFFFFF0 + offset) & 0xFFFFFFFF, data[offset:offset + 9], 0x18, AT)
        self.assertEqual(len(self.events), 1)

    def test_gap_invalidates_and_never_scans_for_red_object(self):
        tcp = self.stream()
        initial = anchor() + object_frame(1)
        tcp.feed(1000, initial, 0x18, AT)
        after_gap = object_frame(777) + anchor(AT + 1) + object_frame(2)
        tcp.feed(1000 + len(initial) + 17, after_gap, 0x18, AT + 1)
        self.assertEqual([e.get("entity_id") for e in self.events], [1])
        tcp.tick(AT + 3.1)
        self.assertEqual([e["kind"] for e in self.events], ["object", "disconnect", "object"])
        self.assertEqual([e["entity_id"] for e in self.events if e["kind"] == "object"], [1, 2])
        self.assertEqual(tcp.counters["gaps"], 1)

    def test_midstream_false_object_before_anchor_is_ignored(self):
        tcp = self.stream()
        tcp.feed(200, b"\x87\xffgarbage" + object_frame(777) + anchor() + object_frame(2), 0x18, AT)
        self.assertEqual([e.get("entity_id") for e in self.events], [2])

    def test_tls_payload_is_not_decoded(self):
        tcp = self.stream()
        body = anchor() + object_frame()
        tls = b"\x17\x03\x03" + len(body).to_bytes(2, "big") + body
        tcp.feed(100, tls, 0x18, AT)
        self.assertFalse(self.events)
        self.assertTrue(tcp.decoder.opaque)

    def test_incomplete_object_fields_in_complete_frame_are_rejected(self):
        tcp = self.stream()
        data = anchor() + frame(0x3634, vi(1) + bytes(5)) + anchor() + object_frame(2)
        tcp.feed(100, data, 0x18, AT)
        self.assertEqual([e["kind"] for e in self.events], ["disconnect", "object"])
        self.assertEqual(self.events[-1]["entity_id"], 2)
        self.assertEqual(tcp.counters["malformed"], 1)

    def test_malformed_bundle_emits_no_partial_events(self):
        events = []
        decoder = FrameStream("bundle", events.append)
        raw = object_frame(1) + frame(0x3635, b"\x80")
        bundle = frame(0xFFFF, struct.pack("<I", len(raw)) + literal_block(raw))
        decoder.feed(anchor() + bundle + anchor() + object_frame(2), AT)
        self.assertEqual([e["entity_id"] for e in events if e["kind"] == "object"], [2])
        self.assertEqual(decoder.counters["malformed"], 1)

    def test_lz4_overlap_and_expansion_limits(self):
        self.assertEqual(decompress_lz4(b"\x32abc\x03\x00", 9), b"abcabcabc")
        with self.assertRaises(ValueError):
            decompress_lz4(b"\x32abc\x03\x00", 8)
        with self.assertRaises(ValueError):
            decompress_lz4(b"\x10a\x00\x00", 5)
        with self.assertRaises(ValueError):
            decompress_lz4(b"\x00", 8 * 1024 * 1024 + 1)

    def test_only_red_templates_and_actor_id_is_discarded(self):
        events = []
        decoder = FrameStream("test", events.append)
        decoder.feed(anchor() + object_frame(1, 1300081) + object_frame(2)
                     + frame(0x3638, vi(2) + vi(999999) + b"\x01"), AT)
        self.assertEqual([e["kind"] for e in events], ["object", "use"])
        self.assertNotIn("999999", repr(events))

    def test_map_transition_resets_even_if_same_map(self):
        body = bytes(4) + struct.pack("<i", 1110) + bytes(38)
        event = decode_frame(0x3621, body)[0]
        self.assertEqual(event, dict(kind="map", map_id=1110, reset=True, map_source="enter_map"))
        response = decode_frame(0x9101, struct.pack("<Hi", 0, 1110))[0]
        self.assertFalse(response["reset"])
        self.assertEqual(response["map_source"], "world_map_response")

    def test_nonred_entity_reuse_invalidates_red_and_suppresses_later_states(self):
        events = []
        decoder = FrameStream("reuse", events.append)
        decoder.feed(anchor() + object_frame(7) + object_frame(7, 1300081)
                     + frame(0x3635, vi(7) + bytes((4, 3, 0)))
                     + frame(0x3638, vi(7) + vi(999999) + b"\x01"), AT)
        self.assertEqual([e["kind"] for e in events], ["object", "gone"])
        self.assertNotIn(7, decoder.red_entities)
        self.assertNotIn("_other_object", repr(events))
        decoder.feed(object_frame(7) + frame(0x3638, vi(7) + vi(999999) + b"\x01")
                     + frame(0x3635, vi(7) + bytes((4, 3, 0))), AT + 1)
        self.assertEqual([e["kind"] for e in events[-3:]], ["object", "use", "state"])

    def test_gone_and_enter_map_clear_ids_but_browsed_map_does_not(self):
        events = []
        decoder = FrameStream("scope", events.append)
        state = frame(0x3635, vi(7) + bytes((4, 3, 0)))
        decoder.feed(anchor() + object_frame(7)
                     + frame(0x9101, struct.pack("<Hi", 0, 1010)) + state, AT)
        self.assertEqual([e["kind"] for e in events], ["object", "map", "state"])
        decoder.feed(frame(0x3647, vi(7) + bytes((0, 0))) + state, AT)
        self.assertEqual(events[-1]["kind"], "gone")
        decoder.feed(object_frame(7) + frame(0x3621, bytes(4) + struct.pack("<i", 1110) + bytes(38)) + state, AT)
        self.assertEqual(events[-1]["kind"], "map")
        self.assertFalse(decoder.red_entities)

    def test_nonred_id_reuse_inside_lz4_bundle_is_tracked(self):
        events = []
        decoder = FrameStream("bundle-reuse", events.append)
        raw = object_frame(7) + object_frame(7, 1300081) + frame(0x3635, vi(7) + bytes((4, 3, 0)))
        decoder.feed(anchor() + frame(0xFFFF, struct.pack("<I", len(raw)) + literal_block(raw)), AT)
        self.assertEqual([e["kind"] for e in events], ["object", "gone"])

    def test_truncated_ip_and_pcap_records_rejected(self):
        with self.assertRaises(ValueError):
            parse_tcp(bytes.fromhex("020000004500"), 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "short.pcap"
            path.write_bytes(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 0) + bytes(7))
            with self.assertRaises(ValueError):
                list(read_pcap(path))

    def test_fin_invalidates_live_availability(self):
        tcp = self.stream()
        data = anchor() + object_frame()
        tcp.feed(500, data, 0x18, AT)
        tcp.feed(500 + len(data), b"", 0x11, AT + 1)
        self.assertEqual(self.events[-1]["kind"], "disconnect")

    def test_real_positive_capture_replay(self):
        sample = Path(__file__).resolve().parent / "private" / "red-focus-01.pcap"
        if not sample.exists():
            self.skipTest("Private historical capture is intentionally not included in the distributable")
        events = []
        result = replay_pcap(sample, events.append)
        target = [event for event in events if event.get("entity_id") == 42895]
        self.assertEqual([event["kind"] for event in target], ["object", "use", "state"])
        self.assertEqual(target[0]["template_id"], 1301199)
        self.assertEqual((target[0]["x"], target[0]["y"], target[0]["z"]), (106518.4375, 71755.4140625, 4347.0))
        self.assertAlmostEqual(target[0]["at"], 1792749938.926657, places=5)
        self.assertAlmostEqual(target[1]["at"], 1792749947.876736, places=5)
        self.assertEqual((target[2]["a"], target[2]["b"]), (4, 3))
        self.assertAlmostEqual(target[2]["at"], 1792749952.829333, places=5)
        self.assertEqual(result.get("gaps", 0), 0)
        self.assertEqual(result.get("malformed", 0), 0)
        self.assertEqual(len([e for e in events if e["kind"] == "object"]), 1)


if __name__ == "__main__":
    unittest.main()
