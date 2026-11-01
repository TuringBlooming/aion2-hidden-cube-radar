"""Local receive-only collector. No game memory access or packet injection.

Npcap handles live exclusively in the worker thread. Exact inbound game socket
filters are refreshed from the Windows owner-PID TCP table. Raw live packets
exist only in bounded memory and are never saved to disk.
"""
from collections import Counter
import ctypes as C
from ctypes import wintypes as W
import ipaddress
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import uuid

from .protocol import TcpStream, parse_tcp, read_pcap


class PcapIf(C.Structure):
    pass


class PcapAddr(C.Structure):
    pass


PcapAddr._fields_ = [("next", C.POINTER(PcapAddr)), ("addr", C.c_void_p),
                     ("netmask", C.c_void_p), ("broadaddr", C.c_void_p), ("dstaddr", C.c_void_p)]
PcapIf._fields_ = [("next", C.POINTER(PcapIf)), ("name", C.c_char_p),
                   ("description", C.c_char_p), ("addresses", C.POINTER(PcapAddr)), ("flags", C.c_uint)]


class BpfProgram(C.Structure):
    _fields_ = [("bf_len", C.c_uint), ("bf_insns", C.c_void_p)]


class PcapStat(C.Structure):
    _fields_ = [("recv", C.c_uint), ("drop", C.c_uint), ("ifdrop", C.c_uint), ("capt", C.c_uint)]


class Npcap:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("实时采集需要 Windows 和已安装的 Npcap")
        directory = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "Npcap"
        if not (directory / "wpcap.dll").exists():
            raise RuntimeError("未找到 Npcap；目前可查看地图和历史记录")
        self.cookie = os.add_dll_directory(str(directory))
        self.lib = C.CDLL(str(directory / "wpcap.dll"))
        declarations = {
            "pcap_findalldevs": ([C.POINTER(C.POINTER(PcapIf)), C.c_char_p], C.c_int),
            "pcap_freealldevs": ([C.POINTER(PcapIf)], None),
            "pcap_open_live": ([C.c_char_p, C.c_int, C.c_int, C.c_int, C.c_char_p], C.c_void_p),
            "pcap_open_offline": ([C.c_char_p, C.c_char_p], C.c_void_p),
            "pcap_datalink": ([C.c_void_p], C.c_int),
            "pcap_compile": ([C.c_void_p, C.POINTER(BpfProgram), C.c_char_p, C.c_int, C.c_uint], C.c_int),
            "pcap_setfilter": ([C.c_void_p, C.POINTER(BpfProgram)], C.c_int),
            "pcap_freecode": ([C.POINTER(BpfProgram)], None),
            "pcap_setnonblock": ([C.c_void_p, C.c_int, C.c_char_p], C.c_int),
            "pcap_next_ex": ([C.c_void_p, C.POINTER(C.c_void_p), C.POINTER(C.c_void_p)], C.c_int),
            "pcap_stats": ([C.c_void_p, C.POINTER(PcapStat)], C.c_int),
            "pcap_geterr": ([C.c_void_p], C.c_char_p),
            "pcap_close": ([C.c_void_p], None),
        }
        for name, (args, result) in declarations.items():
            fun = getattr(self.lib, name)
            fun.argtypes, fun.restype = args, result
        self.header_format = self._verify_header_abi()

    def _verify_header_abi(self):
        # Synthetic bytes only. The installed library parses its own packet
        # header so assumptions about 32/64-bit Windows timeval cannot corrupt
        # packet lengths. No captured packet is used or written for this test.
        sec, fraction, length = 1700001234, 345678, 17
        with tempfile.TemporaryDirectory(prefix="aion2-header-") as directory:
            path = Path(directory) / "synthetic.pcap"
            path.write_bytes(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 0)
                             + struct.pack("<IIII", sec, fraction, length, length) + bytes(length))
            error = C.create_string_buffer(256)
            handle = self.lib.pcap_open_offline(os.fsencode(path), error)
            if not handle:
                raise RuntimeError("Npcap 本地格式检查失败")
            try:
                header, data = C.c_void_p(), C.c_void_p()
                if self.lib.pcap_next_ex(handle, C.byref(header), C.byref(data)) != 1:
                    raise RuntimeError("Npcap 本地格式检查失败")
                first = C.string_at(header, 16)
                if struct.unpack("<iiII", first) == (sec, fraction, length, length):
                    return "<iiII"
                extended = C.string_at(header, 24)
                for fmt in ("<qqII", "<qiII4x", "<qi4xII"):
                    if struct.unpack(fmt, extended) == (sec, fraction, length, length):
                        return fmt
                raise RuntimeError("无法确认 Npcap 数据头格式，已停止采集")
            finally:
                self.lib.pcap_close(handle)

    def devices(self):
        head = C.POINTER(PcapIf)()
        error = C.create_string_buffer(256)
        if self.lib.pcap_findalldevs(C.byref(head), error):
            raise RuntimeError("无法读取 Npcap 网络接口：" + error.value.decode(errors="replace"))
        result = []
        try:
            node = head
            while node:
                item = node.contents
                addresses = set()
                address = item.addresses
                while address:
                    ptr = address.contents.addr
                    if ptr:
                        family = int.from_bytes(C.string_at(ptr, 2), "little")
                        if family == socket.AF_INET:
                            addresses.add(socket.inet_ntop(socket.AF_INET, C.string_at(ptr, 16)[4:8]))
                        elif family == socket.AF_INET6:
                            addresses.add(socket.inet_ntop(socket.AF_INET6, C.string_at(ptr, 28)[8:24]))
                    address = address.contents.next
                result.append(dict(name=item.name.decode(), addresses=addresses,
                                   loopback=bool(item.flags & 1) or b"Loopback" in item.name))
                node = item.next
        finally:
            self.lib.pcap_freealldevs(head)
        return result

    def set_filter(self, handle, expression):
        program = BpfProgram()
        if self.lib.pcap_compile(handle, C.byref(program), expression.encode("ascii"), 1, 0xFFFFFFFF):
            raise RuntimeError("无法建立精确游戏连接筛选")
        try:
            if self.lib.pcap_setfilter(handle, C.byref(program)):
                raise RuntimeError("无法应用精确游戏连接筛选")
        finally:
            self.lib.pcap_freecode(C.byref(program))

    def open(self, device, expression):
        error = C.create_string_buffer(256)
        handle = self.lib.pcap_open_live(device.encode(), 65535, 0, 50, error)
        if not handle:
            raise RuntimeError("无法打开接收接口：" + error.value.decode(errors="replace"))
        try:
            self.set_filter(handle, expression)
            if self.lib.pcap_setnonblock(handle, 1, error):
                raise RuntimeError("无法启用非阻塞接收")
            linktype = self.lib.pcap_datalink(handle)
            if linktype not in (0, 1, 12, 101):
                raise RuntimeError("当前网络接口格式暂不支持")
            return handle, linktype
        except BaseException:
            self.lib.pcap_close(handle)
            raise

    def next(self, handle):
        header, data = C.c_void_p(), C.c_void_p()
        result = self.lib.pcap_next_ex(handle, C.byref(header), C.byref(data))
        if result == 0:
            return None
        if result < 0:
            raise RuntimeError("接收接口已断开")
        sec, fraction, caplen, wirelen = struct.unpack(self.header_format, C.string_at(header, struct.calcsize(self.header_format)))
        if not 0 <= caplen <= 65535 or caplen > wirelen or not 0 <= fraction < 1_000_000 or abs(sec - time.time()) > 86400 * 366:
            raise RuntimeError("Npcap 数据头校验失败，已停止采集")
        return sec + fraction / 1_000_000, C.string_at(data, caplen), caplen != wirelen

    def close(self):
        self.cookie.close()


def game_pids():
    class ProcessEntry(C.Structure):
        _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                    ("th32DefaultHeapID", C.c_size_t), ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
                    ("th32ParentProcessID", W.DWORD), ("pcPriClassBase", W.LONG), ("dwFlags", W.DWORD),
                    ("szExeFile", W.WCHAR * 260)]
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = W.HANDLE
    kernel.Process32FirstW.argtypes = [W.HANDLE, C.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = W.BOOL
    kernel.Process32NextW.argtypes = [W.HANDLE, C.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = W.BOOL
    kernel.CloseHandle.argtypes = [W.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == C.c_void_p(-1).value:
        raise RuntimeError("无法读取游戏进程列表")
    result = set()
    try:
        entry = ProcessEntry()
        entry.dwSize = C.sizeof(entry)
        okay = kernel.Process32FirstW(snapshot, C.byref(entry))
        while okay:
            if entry.szExeFile.casefold() == "aion2.exe":
                result.add(entry.th32ProcessID)
            okay = kernel.Process32NextW(snapshot, C.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return result


def game_connections(pids):
    if not pids:
        return set()
    api = C.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    api.argtypes = [C.c_void_p, C.POINTER(W.DWORD), W.BOOL, W.ULONG, C.c_int, W.ULONG]
    api.restype = W.DWORD
    result = set()
    for family, row_size in ((socket.AF_INET, 24), (socket.AF_INET6, 56)):
        size = W.DWORD()
        code = api(None, C.byref(size), False, family, 5, 0)
        if code not in (0, 122):
            if family == socket.AF_INET6:
                continue
            raise RuntimeError("无法读取游戏连接列表")
        for _ in range(3):
            buffer = C.create_string_buffer(max(4, size.value))
            code = api(buffer, C.byref(size), False, family, 5, 0)
            if code != 122:
                break
        if code:
            raise RuntimeError("游戏连接列表正在变化，请稍后重试")
        count = int.from_bytes(buffer.raw[:4], "little")
        if 4 + count * row_size > len(buffer):
            raise RuntimeError("游戏连接列表格式错误")
        for index in range(count):
            row = buffer.raw[4 + index * row_size:4 + (index + 1) * row_size]
            if family == socket.AF_INET:
                state, _, _, _, _, pid = struct.unpack("<6I", row)
                local, remote = socket.inet_ntop(family, row[4:8]), socket.inet_ntop(family, row[12:16])
                local_port, remote_port = int.from_bytes(row[8:10], "big"), int.from_bytes(row[16:18], "big")
            else:
                state, pid = struct.unpack_from("<II", row, 48)
                local, remote = socket.inet_ntop(family, row[:16]), socket.inet_ntop(family, row[24:40])
                local_port, remote_port = int.from_bytes(row[20:22], "big"), int.from_bytes(row[44:46], "big")
            if pid in pids and state == 5 and not ipaddress.ip_address(remote).is_loopback:
                result.add((remote, remote_port, local, local_port))
    return result


def bpf_filter(connections):
    return "tcp and (" + " or ".join(
        f"(src host {src} and src port {sport} and dst host {dst} and dst port {dport})"
        for src, sport, dst, dport in sorted(connections)) + ")"


def refresh_capture_handles(library, handles, connections):
    """Keep each inbound flow on an unchanged capture handle.

    Changing an NPF filter discards buffered packets. One connection ending
    must not flush another connection's object-state messages. Each handle is
    therefore scoped to one verified inbound 4-tuple for its entire lifetime.
    """
    devices = library.devices() if connections else []
    local_addresses = {addr for item in devices for addr in item["addresses"]}
    local_addresses.update(key[2] for key in connections)
    desired = set()
    for key in connections:
        local_route = key[0] in local_addresses
        candidates = [d for d in devices if d["loopback"]] if local_route else [
            d for d in devices if not d["loopback"] and key[2] in d["addresses"]]
        if not candidates and not local_route:
            candidates = [d for d in devices if not d["loopback"]]
        desired.update((device["name"], key) for device in candidates)
    for identity in set(handles) - desired:
        library.lib.pcap_close(handles.pop(identity)["handle"])
    for identity in sorted(desired - set(handles)):
        device, key = identity
        expression = bpf_filter({key})
        handle, linktype = library.open(device, expression)
        handles[identity] = dict(handle=handle, linktype=linktype,
                                 expression=expression, keys={key}, drops=0)


class Collector:
    def __init__(self, on_event, on_status):
        self.on_event = on_event
        self.on_status = on_status
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="aion2-receive-only", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)

    def set_paused(self, paused):
        (self._paused.set if paused else self._paused.clear)()

    def _run(self):
        counters = Counter()
        streams, handles = {}, {}
        library = None
        session = "live-" + uuid.uuid4().hex[:12]
        flow_number = 0
        last_packet_at = None
        next_refresh = next_status = 0
        process_count = 0
        last_error = None

        def event(value):
            if value.get("kind") == "player_position":
                counters["player_positions"] += 1
            self.on_event(value)

        def clear(at, reason):
            for stream in streams.values():
                stream.close(at, reason)
            streams.clear()
            if library:
                for opened in handles.values():
                    library.lib.pcap_close(opened["handle"])
            handles.clear()

        def status(phase=None, detail=None):
            synced = sum(s.decoder.synced for s in streams.values())
            opaque = sum(s.decoder.opaque for s in streams.values())
            if phase is None:
                if self._paused.is_set():
                    phase, detail = "paused", "已暂停接收；恢复后重新确认位置状态"
                elif synced:
                    phase, detail = "observing", "正在接收并解析本机游戏对象变化"
                elif not process_count:
                    phase, detail = "waiting", "等待 AION2 运行"
                elif not streams:
                    phase, detail = "waiting", "等待游戏服务器连接"
                elif last_error:
                    phase, detail = "error", last_error
                else:
                    phase, detail = "waiting", "已找到游戏连接，等待可验证的游戏数据"
            frame_times = [s.decoder.last_frame_at for s in streams.values() if s.decoder.last_frame_at is not None]
            self.on_status(dict(phase=phase, detail=detail, packets=counters["packets"],
                frames=counters["frames"], heartbeats=counters["heartbeats"],
                last_packet_at=last_packet_at, last_frame_at=max(frame_times) if frame_times else None,
                sockets=len(streams), capture_handles=len(handles), synced_connections=synced, opaque_connections=opaque,
                process_count=process_count, gaps=counters["gaps"], malformed=counters["malformed"],
                player_positions=counters["player_positions"], player_malformed=counters["player_malformed"],
                driver_drops=counters["driver_drops"], truncated=counters["truncated"],
                retransmitted_bytes=counters["retransmitted_bytes"]))

        try:
            library = Npcap()
            while not self._stop.is_set():
                now = time.time()
                mono = time.monotonic()
                if self._paused.is_set():
                    if streams or handles:
                        clear(now, "用户暂停接收")
                    next_refresh = 0
                    if mono >= next_status:
                        status()
                        next_status = mono + 1
                    self._stop.wait(0.1)
                    continue
                if mono >= next_refresh:
                    next_refresh = mono + 3
                    try:
                        pids = game_pids()
                        process_count = len(pids)
                        connections = game_connections(pids)
                        for key in set(streams) - connections:
                            streams.pop(key).close(now, "游戏连接已更换或断开")
                        for key in connections - set(streams):
                            flow_number += 1
                            streams[key] = TcpStream(f"{session}:{flow_number}", event, counters)
                        refresh_capture_handles(library, handles, connections)
                        last_error = "未找到可读取这些游戏连接的网络接口" if connections and not handles else None
                    except (OSError, RuntimeError) as exc:
                        # An uncertain owner table cannot justify continuing an
                        # old filter. Close handles until ownership is refreshed.
                        clear(now, "连接来源暂无法核验")
                        last_error = str(exc)
                        status("error", last_error)
                received = False
                for identity, opened in list(handles.items()):
                    try:
                        for _ in range(200):
                            if self._stop.is_set() or self._paused.is_set():
                                break
                            record = library.next(opened["handle"])
                            if record is None:
                                break
                            received = True
                            at, packet, truncated = record
                            counters["packets"] += 1
                            last_packet_at = at
                            if truncated:
                                counters["truncated"] += 1
                                for key in opened["keys"]:
                                    if key in streams:
                                        streams[key].close(at, "接收数据被截断")
                                continue
                            try:
                                parsed = parse_tcp(packet, opened["linktype"])
                            except ValueError:
                                counters["malformed"] += 1
                                for key in opened["keys"]:
                                    if key in streams:
                                        streams[key].close(at, "网络分片或数据校验失败")
                                continue
                            if parsed:
                                key, sequence, flags, body = parsed
                                if key in streams:
                                    streams[key].feed(sequence, body, flags, at)
                    except (OSError, RuntimeError) as exc:
                        last_error = str(exc)
                        for key in opened["keys"]:
                            if key in streams:
                                streams[key].close(now, "接收接口断开")
                        library.lib.pcap_close(handles.pop(identity)["handle"])
                        next_refresh = 0
                for stream in streams.values():
                    stream.tick(now)
                    if stream.decoder.synced and stream.decoder.last_frame_at and now - stream.decoder.last_frame_at > 10:
                        stream.close(now, "超过 10 秒没有可确认的数据，等待恢复")
                if mono >= next_status:
                    next_status = mono + 1
                    for opened in handles.values():
                        stats = PcapStat()
                        if library.lib.pcap_stats(opened["handle"], C.byref(stats)) == 0:
                            dropped = stats.drop + stats.ifdrop
                            delta = max(0, dropped - opened["drops"])
                            opened["drops"] = dropped
                            if delta:
                                counters["driver_drops"] += delta
                                for key in opened["keys"]:
                                    if key in streams:
                                        streams[key].close(now, "接收驱动报告丢包，等待重新确认")
                    status()
                if not received:
                    self._stop.wait(0.01)
        except Exception as exc:
            status("error", "采集已停止：" + str(exc))
        finally:
            clear(time.time(), "采集已停止")
            if library:
                library.close()
            if self._stop.is_set():
                status("paused", "采集已停止")


def replay_pcap(path, on_event, on_status=None, speed=0, stop_event=None):
    """Replay an existing capture through exactly the live stream decoder.

    speed=0 is deterministic immediate replay. Positive speed uses original
    intervals divided by speed. Returned/event timestamps remain historical.
    Replay does not instantiate Npcap or open a network connection.
    """
    counters = Counter()
    streams = {}
    previous = None
    last = None
    stop_event = stop_event or threading.Event()
    session = "replay-" + uuid.uuid4().hex[:12]
    for at, packet, linktype, truncated in read_pcap(path):
        if stop_event.is_set():
            break
        if speed > 0 and previous is not None and stop_event.wait(max(0, at - previous) / speed):
            break
        previous = last = at
        counters["packets"] += 1
        if truncated:
            counters["truncated"] += 1
            for stream in streams.values():
                stream.close(at, "历史样本记录被截断")
            continue
        try:
            parsed = parse_tcp(packet, linktype)
        except ValueError:
            counters["malformed"] += 1
            for stream in streams.values():
                stream.close(at, "历史网络数据校验失败")
            continue
        if not parsed:
            continue
        key, seq, flags, body = parsed
        if key not in streams:
            streams[key] = TcpStream(f"{session}:{len(streams) + 1}", on_event, counters)
        streams[key].feed(seq, body, flags, at)
        for stream in streams.values():
            stream.tick(at)
    result = dict(counters, last_packet_at=last, sockets=len(streams),
                  last_frame_at=max((s.decoder.last_frame_at or 0 for s in streams.values()), default=0) or None)
    if on_status:
        on_status(dict(result, phase="paused", detail="历史样本回放完成；事件时间为原始记录时间"))
    return result
