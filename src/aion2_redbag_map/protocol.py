"""Receive-only AION2 stream decoding, independently implemented.

Only red-cube events and self-associated reference positions leave the stream
decoder. Player names, chat, actor IDs and raw payloads are neither returned nor
persisted. Positions are observations from status events, not continuous motion.
The wire layout was cross-checked with the statically unpacked A2Helper 0.1.25
bundle and local offline samples.
"""
from collections import Counter
import math
import socket
import struct

RED_IDS = frozenset({1200039, 1200040, 1200041, 1201503, 1201504,
                     1301199, 1301200, 1301244, 1301245})
MAX_FRAME = 65535
MAX_BUFFER = 262144
MAX_BUNDLE = 8 * 1024 * 1024
MAX_EXPANSION = 16 * 1024 * 1024
MAX_PLAYER_COORDINATE = 1_000_000


class Incomplete(ValueError):
    pass


def varint(data, offset=0):
    value = 0
    for i in range(5):
        if offset + i >= len(data):
            raise Incomplete("incomplete varint")
        byte = data[offset + i]
        if i == 4 and byte > 15:
            raise ValueError("oversized varint")
        value |= (byte & 127) << (i * 7)
        if not byte & 128:
            return value, i + 1
    raise ValueError("oversized varint")


def frame_at(data, offset=0):
    encoded, width = varint(data, offset)
    size = encoded + width - 4
    if size < width + 2 or size > MAX_FRAME:
        raise ValueError("invalid frame length")
    if offset + size > len(data):
        raise Incomplete("incomplete frame")
    opcode = struct.unpack_from("<H", data, offset + width)[0]
    return size, opcode, bytes(data[offset + width + 2:offset + size])


def decompress_lz4(data, expected):
    """Bounded raw LZ4 block decoder; no native dependencies or target code."""
    if not 0 < expected <= MAX_BUNDLE:
        raise ValueError("bundle size outside limit")
    out = bytearray()
    pos = 0
    while pos < len(data):
        token = data[pos]
        pos += 1
        literals = token >> 4
        if literals == 15:
            while True:
                if pos >= len(data):
                    raise ValueError("short LZ4 literal length")
                extra = data[pos]
                pos += 1
                literals += extra
                if extra != 255:
                    break
        if pos + literals > len(data) or len(out) + literals > expected:
            raise ValueError("invalid LZ4 literals")
        out.extend(data[pos:pos + literals])
        pos += literals
        if pos == len(data):
            break
        if pos + 2 > len(data):
            raise ValueError("short LZ4 match")
        distance = data[pos] | data[pos + 1] << 8
        pos += 2
        if not distance or distance > len(out):
            raise ValueError("invalid LZ4 distance")
        length = (token & 15) + 4
        if token & 15 == 15:
            while True:
                if pos >= len(data):
                    raise ValueError("short LZ4 match length")
                extra = data[pos]
                pos += 1
                length += extra
                if extra != 255:
                    break
        if len(out) + length > expected:
            raise ValueError("LZ4 expansion exceeds declared size")
        if length <= distance:
            out.extend(out[-distance:-distance + length] if length != distance else out[-distance:])
        else:
            pattern = bytes(out[-distance:])
            repeats, remainder = divmod(length, distance)
            out.extend(pattern * repeats)
            out.extend(pattern[:remainder])
    if len(out) != expected:
        raise ValueError("LZ4 length mismatch")
    return bytes(out)


class _StatusReader:
    """Bounds-checked status fields; bool bits share a cursor across a message."""
    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.bit_index = 0

    def take(self, count):
        if count < 0 or self.pos + count > len(self.data):
            raise ValueError("short player status field")
        start = self.pos
        self.pos += count
        return start

    def u8(self):
        return self.data[self.take(1)]

    def vi(self):
        value, width = varint(self.data, self.pos)
        self.take(width)
        return value

    def boolean(self):
        # The bit byte stays active while intervening non-bool fields are read.
        # The ninth bool, not the next field or record, consumes another byte.
        if self.bit_index == 0:
            self.take(1)
        self.bit_index = (self.bit_index + 1) % 8

    def finish(self):
        if self.pos != len(self.data):
            raise ValueError("trailing player status fields")


def _self_stats_key(body):
    reader = _StatusReader(body)
    key = reader.vi()
    count = reader.vi()
    reader.take(count * 6)  # list<u16 attribute, i32 value>
    reader.take(8)
    reader.finish()
    if not key:
        raise ValueError("invalid player status key")
    return key


def _status_positions(opcode, body):
    reader = _StatusReader(body)
    target = reader.vi()
    if not target:
        raise ValueError("invalid status target")
    count = reader.vi() if opcode == 0x382A else 1
    # A record uses at least 23 bytes before optional fields and packed bools.
    if count > (len(body) - reader.pos) // 23:
        raise ValueError("invalid status list length")
    events = []
    for _ in range(count):
        flags = reader.u8()
        if flags & ~0x1F:
            raise ValueError("unknown status layout flags")
        reader.vi()  # status instance, intentionally discarded
        reader.take(4 + 8 + 8)  # type, duration and expiry
        caster = reader.vi() if flags & 1 else 0
        reader.take(1)
        if flags & 2:
            reader.take(4)
        reader.boolean()
        if flags & 4:
            reader.vi()
        if flags & 8:
            reader.take(4)
        if flags & 16:
            x, y, z = struct.unpack_from("<fff", body, reader.take(12))
            if not all(math.isfinite(v) and abs(v) <= MAX_PLAYER_COORDINATE for v in (x, y, z)):
                raise ValueError("invalid status position")
            if x or y:
                events.append(dict(kind="_self_status_position", target=target,
                                   caster=caster, x=x, y=y, z=z,
                                   source_opcode=f"0x{opcode:04x}"))
    if opcode == 0x382B:
        reader.boolean()
    reader.finish()
    return events


def decode_frame(opcode, body, budget=None, depth=0, track_objects=False):
    """Public frame decoding never exposes player identity or status records."""
    return _decode_frame(opcode, body, budget, depth, track_objects)


def _decode_frame(opcode, body, budget=None, depth=0, track_objects=False, track_player=False):
    """Decode a whole frame atomically, including bounded nested bundles."""
    if budget is None:
        budget = [0, 0]
    budget[1] += 1
    if budget[1] > 65536 or depth > 4:
        raise ValueError("bundle nesting or message limit")
    events = []
    if opcode == 0xFFFF:
        if len(body) < 4:
            raise ValueError("short bundle")
        declared = struct.unpack_from("<I", body)[0]
        budget[0] += declared
        if budget[0] > MAX_EXPANSION:
            raise ValueError("nested bundle expansion limit")
        raw = decompress_lz4(body[4:], declared)
        pos = 0
        while pos < len(raw):
            if raw[pos] == 0:
                pos += 1
                continue
            size, child_opcode, child = frame_at(raw, pos)
            events.extend(_decode_frame(child_opcode, child, budget, depth + 1, track_objects, track_player))
            pos += size
    elif opcode == 0x3600:
        if len(body) != 8:
            raise ValueError("invalid heartbeat")
    elif opcode == 0x3634:
        entity, width = varint(body)
        fields = width + 2
        if len(body) < fields + 35:
            raise ValueError("short object")
        template, x, y, z = struct.unpack_from("<Ifff", body, fields)
        if not entity or not template or not all(math.isfinite(v) and abs(v) <= 10_000_000 for v in (x, y, z)):
            raise ValueError("invalid object fields")
        if template in RED_IDS:
            events.append(dict(kind="object", entity_id=entity, template_id=template,
                               x=x, y=y, z=z, states=list(body[fields + 24:fields + 27]),
                               spawned_at=struct.unpack_from("<q", body, fields + 27)[0]))
        elif track_objects:
            # Private tracking signal, consumed by FrameStream and never sent
            # to consumers. A server can reuse an entity ID for another object.
            events.append(dict(kind="_other_object", entity_id=entity))
    elif opcode == 0x3635:
        entity, width = varint(body)
        if not entity or len(body) < width + 3:
            raise ValueError("short state")
        events.append(dict(kind="state", entity_id=entity, a=body[width], b=body[width + 1]))
    elif opcode == 0x3638:
        entity, width = varint(body)
        _, actor_width = varint(body, width)
        if not entity or len(body) < width + actor_width + 1:
            raise ValueError("short use")
        events.append(dict(kind="use", entity_id=entity))
    elif opcode == 0x3647:
        entity, width = varint(body)
        if not entity or len(body) < width + 2:
            raise ValueError("short gone")
        varint(body, width + 1)
        events.append(dict(kind="gone", entity_id=entity))
    elif opcode == 0x3621:
        if len(body) < 46:
            raise ValueError("short map transition")
        map_id = struct.unpack_from("<i", body, 4)[0]
        if 0 < map_id < 10_000_000:
            events.append(dict(kind="map", map_id=map_id, reset=True, map_source="enter_map"))
    elif opcode == 0x9101:
        if len(body) < 6:
            raise ValueError("short world map")
        result, map_id = struct.unpack_from("<Hi", body)
        if result == 0 and 0 < map_id < 10_000_000:
            events.append(dict(kind="map", map_id=map_id, reset=False, map_source="world_map_response"))
    elif track_player and opcode in (0x364A, 0x382A, 0x382B):
        try:
            if opcode == 0x364A:
                events.append(dict(kind="_self_stats", key=_self_stats_key(body)))
            else:
                events.extend(_status_positions(opcode, body))
        except (ValueError, struct.error):
            # This optional layout is not allowed to interrupt red-bag events.
            # Frame/compression boundary errors above remain atomic failures.
            events.append(dict(kind="_player_invalid"))
    if depth == 0 and any(event["kind"] == "_player_invalid" for event in events):
        # No identity vote or position from any child of this outer bundle may
        # escape when a player-status layout inside it is not understood.
        private = {"_self_stats", "_self_status_position", "_player_invalid"}
        events = [event for event in events if event["kind"] not in private]
        events.append(dict(kind="_player_invalid"))
    return events


class FrameStream:
    """Framing starts only after two credible heartbeats plus frame evidence.

    Unaligned bytes are never searched for an object opcode. A lost byte or
    malformed frame invalidates prior availability, then quarantines the stream
    until a new heartbeat-anchored boundary is established.
    """
    def __init__(self, connection_id, on_event, counters=None):
        self.connection_id = connection_id
        self.on_event = on_event
        self.counters = counters if counters is not None else Counter()
        self.buffer = bytearray()
        self.synced = False
        self.opaque = False
        self.last_frame_at = None
        self.last_heartbeat_at = None
        self.red_entities = set()
        self._clear_player_identity()

    def _clear_player_identity(self):
        self._self_candidate = None
        self._self_votes = 0
        self._self_key = None
        self._player_disabled = False

    def _observe_self_stats(self, key, at):
        if self._player_disabled:
            return
        if key != self._self_candidate:
            had_identity = self._self_key is not None
            self._clear_player_identity()
            self._self_candidate = key
            self._self_votes = 1
            if had_identity:
                self.on_event(dict(kind="player_position_reset", reason="identity_changed",
                                   at=at, connection_id=self.connection_id))
        else:
            self._self_votes = min(2, self._self_votes + 1)
            if self._self_votes >= 2:
                self._self_key = key

    def invalidate(self, at, reason):
        self._clear_player_identity()
        if self.synced:
            self.on_event(dict(kind="disconnect", at=at, connection_id=self.connection_id, reason=reason))
        self.synced = False
        self.buffer.clear()
        self.red_entities.clear()

    def _anchor(self, at):
        # TLS records are deliberately opaque; this is not a TLS decoder.
        if len(self.buffer) >= 5 and self.buffer[0] in (20, 21, 22, 23) and self.buffer[1] == 3 and 1 <= self.buffer[2] <= 4:
            length = int.from_bytes(self.buffer[3:5], "big")
            if 0 < length <= 18432:
                self.opaque = True
                self.buffer.clear()
                return False
        start = 0
        pending = None
        while True:
            start = self.buffer.find(b"\x0e\x00\x36", start)
            if start < 0:
                if pending is not None:
                    del self.buffer[:pending]
                elif len(self.buffer) > MAX_FRAME:
                    del self.buffer[:-MAX_FRAME]
                return False
            pos = start
            heartbeats = []
            frames = 0
            try:
                while frames < 16 and pos - start <= MAX_FRAME:
                    if pos >= len(self.buffer):
                        raise Incomplete("anchor pending")
                    if self.buffer[pos] == 0:
                        pos += 1
                        continue
                    size, opcode, body = frame_at(self.buffer, pos)
                    if opcode == 0x3600:
                        if len(body) != 8:
                            raise ValueError("bad anchor heartbeat")
                        stamp = struct.unpack("<q", body)[0] / 1000
                        if abs(stamp - at) > 86400 or (heartbeats and not 0 <= stamp - heartbeats[-1] <= 30):
                            raise ValueError("implausible heartbeat clock")
                        heartbeats.append(stamp)
                    frames += 1
                    pos += size
                    if frames >= 3 and len(heartbeats) >= 2:
                        del self.buffer[:start]
                        self.synced = True
                        self.counters["syncs"] += 1
                        return True
            except Incomplete:
                pending = start if pending is None else pending
                # A later complete anchor can supersede an incomplete candidate.
            except (ValueError, struct.error):
                pass
            start += 1

    def feed(self, data, at):
        if self.opaque:
            return
        self.buffer.extend(data)
        if len(self.buffer) > MAX_BUFFER:
            self.counters["malformed"] += 1
            tail = bytes(self.buffer[-MAX_FRAME:])
            self.invalidate(at, "协议缓冲超过限制，等待重新同步")
            self.buffer.extend(tail)
        while self.buffer:
            if not self.synced and not self._anchor(at):
                return
            if self.buffer[0] == 0:
                del self.buffer[0]
                continue
            try:
                size, opcode, body = frame_at(self.buffer)
            except Incomplete:
                return
            except (ValueError, struct.error):
                self.counters["malformed"] += 1
                tail = bytes(self.buffer[1:])
                self.invalidate(at, "协议校验失败，等待重新同步")
                self.buffer.extend(tail)
                continue
            try:
                events = _decode_frame(opcode, body, track_objects=True, track_player=True)
            except (ValueError, struct.error):
                # The outer frame is complete: an incomplete field or nested
                # frame is malformed data, never a reason to wait for more TCP.
                self.counters["malformed"] += 1
                tail = bytes(self.buffer[size:])
                self.invalidate(at, "对象数据校验失败，等待重新同步")
                self.buffer.extend(tail)
                continue
            del self.buffer[:size]
            self.counters["frames"] += 1
            self.last_frame_at = at
            if opcode == 0x3600:
                self.last_heartbeat_at = at
                self.counters["heartbeats"] += 1
            for event in events:
                kind = event["kind"]
                entity = event.get("entity_id")
                if kind == "_player_invalid":
                    self.counters["player_malformed"] += 1
                    if not self._player_disabled:
                        self._clear_player_identity()
                        self._player_disabled = True
                        self.on_event(dict(kind="player_position_reset", reason="unsupported_status_layout",
                                           at=at, connection_id=self.connection_id))
                    continue
                if kind == "_self_stats":
                    self._observe_self_stats(event["key"], at)
                    continue
                if kind == "_self_status_position":
                    if self._self_key is not None and event["target"] == event["caster"] == self._self_key:
                        self.on_event(dict(kind="player_position", x=event["x"], y=event["y"],
                                           z=event["z"], at=at, connection_id=self.connection_id,
                                           source="self_status", source_opcode=event["source_opcode"],
                                           identity_source="stats_consensus"))
                    continue
                if kind == "_other_object":
                    if entity in self.red_entities:
                        self.red_entities.remove(entity)
                        self.on_event(dict(kind="gone", entity_id=entity, at=at,
                                           connection_id=self.connection_id,
                                           reason="对象标识已被非红色背包重新使用"))
                    continue
                if kind == "object":
                    self.red_entities.add(entity)
                    if len(self.red_entities) > 8192:
                        self.counters["malformed"] += 1
                        self.invalidate(at, "对象跟踪超过限制，等待重新确认")
                        return
                elif kind in ("state", "use", "gone"):
                    if entity not in self.red_entities:
                        continue
                    if kind == "gone":
                        self.red_entities.remove(entity)
                elif kind == "map" and event.get("map_source") == "enter_map":
                    self.red_entities.clear()
                    self._clear_player_identity()
                self.on_event(dict(event, at=at, connection_id=self.connection_id))


def seq_distance(a, b):
    return ((a - b + 2**31) % 2**32) - 2**31


class TcpStream:
    def __init__(self, connection_id, on_event, counters=None):
        self.counters = counters if counters is not None else Counter()
        self.decoder = FrameStream(connection_id, on_event, self.counters)
        self.next_seq = None
        self.pending = []
        self.gap_at = None
        self.last_packet_at = None

    def close(self, at, reason="连接已结束"):
        self.decoder.invalidate(at, reason)
        self.pending.clear()
        self.gap_at = None

    def _drain(self, at):
        while self.pending:
            self.pending.sort(key=lambda p: seq_distance(p[0], self.next_seq))
            seq, data = self.pending[0]
            distance = seq_distance(seq, self.next_seq)
            if distance > 0:
                break
            self.pending.pop(0)
            skip = max(0, -distance)
            self.counters["retransmitted_bytes"] += min(skip, len(data))
            if skip < len(data):
                fresh = data[skip:]
                self.decoder.feed(fresh, at)
                self.next_seq = (self.next_seq + len(fresh)) & 0xFFFFFFFF
        self.gap_at = self.gap_at if self.pending else None

    def tick(self, at):
        if self.pending and self.gap_at is not None and (at - self.gap_at >= 2 or len(self.pending) > 256 or sum(len(p[1]) for p in self.pending) > MAX_BUFFER):
            self.pending.sort(key=lambda p: seq_distance(p[0], self.next_seq))
            self.counters["gaps"] += 1
            self.counters["gap_bytes"] += max(0, seq_distance(self.pending[0][0], self.next_seq))
            self.decoder.invalidate(at, "TCP 数据缺口，先前位置状态暂不可确认")
            self.next_seq = self.pending[0][0]
            self.gap_at = None
            self._drain(at)
            if self.pending:
                self.gap_at = at

    def feed(self, seq, payload, flags, at):
        self.last_packet_at = at
        if flags & 2:
            seq = (seq + 1) & 0xFFFFFFFF
        if payload:
            if self.next_seq is None:
                self.next_seq = seq
            if not any(s == seq and p == payload for s, p in self.pending):
                self.pending.append((seq, bytes(payload)))
            self._drain(at)
            if self.pending and self.gap_at is None:
                self.gap_at = at
            self.tick(at)
        if flags & 5:  # FIN or RST, including a final segment carrying payload.
            self.close(at)


def parse_tcp(packet, linktype):
    """Return inbound 4-tuple/sequence/flags/payload; reject truncation/fragments."""
    if linktype == 0:
        if len(packet) < 4:
            raise ValueError("short loopback header")
        family = int.from_bytes(packet[:4], "little")
        if family not in (2, 23, 24, 28, 30):
            return None
        ip = packet[4:]
    elif linktype == 1:
        if len(packet) < 14:
            raise ValueError("short ethernet header")
        offset = 14
        ether = int.from_bytes(packet[12:14], "big")
        while ether in (0x8100, 0x88A8):
            if len(packet) < offset + 4:
                raise ValueError("short VLAN header")
            ether = int.from_bytes(packet[offset + 2:offset + 4], "big")
            offset += 4
        if ether not in (0x0800, 0x86DD):
            return None
        ip = packet[offset:]
    elif linktype in (12, 101):
        ip = packet
    else:
        raise ValueError("unsupported link type")
    if not ip:
        raise ValueError("missing IP header")
    version = ip[0] >> 4
    if version == 4:
        if len(ip) < 20:
            raise ValueError("short IPv4 header")
        ihl = (ip[0] & 15) * 4
        total = int.from_bytes(ip[2:4], "big")
        if ihl < 20 or total < ihl or len(ip) < total:
            raise ValueError("truncated IPv4 packet")
        if ip[9] != 6:
            return None
        if int.from_bytes(ip[6:8], "big") & 0x3FFF:
            raise ValueError("fragmented IPv4 packet")
        src, dst = socket.inet_ntop(socket.AF_INET, ip[12:16]), socket.inet_ntop(socket.AF_INET, ip[16:20])
    elif version == 6:
        if len(ip) < 40:
            raise ValueError("short IPv6 header")
        total = 40 + int.from_bytes(ip[4:6], "big")
        if len(ip) < total:
            raise ValueError("truncated IPv6 packet")
        src, dst = socket.inet_ntop(socket.AF_INET6, ip[8:24]), socket.inet_ntop(socket.AF_INET6, ip[24:40])
        nxt, ihl = ip[6], 40
        for _ in range(8):
            if nxt == 6:
                break
            if nxt == 44:
                raise ValueError("fragmented IPv6 packet")
            if nxt not in (0, 43, 60) or ihl + 2 > total:
                return None
            nxt, length = ip[ihl], (ip[ihl + 1] + 1) * 8
            ihl += length
        if nxt != 6:
            return None
    else:
        return None
    if total < ihl + 20:
        raise ValueError("short TCP header")
    segment = ip[ihl:total]
    sport, dport, seq = struct.unpack_from("!HHI", segment)
    thl = (segment[12] >> 4) * 4
    if thl < 20 or len(segment) < thl:
        raise ValueError("invalid TCP header")
    return (src, sport, dst, dport), seq, segment[13], bytes(segment[thl:])


def read_pcap(path):
    with open(path, "rb") as stream:
        header = stream.read(24)
        formats = {b"\xd4\xc3\xb2\xa1": ("<", 1e6), b"\xa1\xb2\xc3\xd4": (">", 1e6),
                   b"\x4d\x3c\xb2\xa1": ("<", 1e9), b"\xa1\xb2\x3c\x4d": (">", 1e9)}
        if len(header) != 24 or header[:4] not in formats:
            raise ValueError("unsupported PCAP header")
        endian, divisor = formats[header[:4]]
        linktype = struct.unpack_from(endian + "I", header, 20)[0]
        while True:
            record = stream.read(16)
            if not record:
                return
            if len(record) != 16:
                raise ValueError("truncated PCAP record header")
            sec, fraction, caplen, wirelen = struct.unpack(endian + "IIII", record)
            if caplen > 16 * 1024 * 1024:
                raise ValueError("PCAP record exceeds limit")
            packet = stream.read(caplen)
            if len(packet) != caplen:
                raise ValueError("truncated PCAP packet")
            yield sec + fraction / divisor, packet, linktype, caplen != wirelen
