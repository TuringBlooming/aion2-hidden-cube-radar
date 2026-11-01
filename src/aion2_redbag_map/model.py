"""Local observation state. Receiving an object is not proof of a new spawn."""
from __future__ import annotations

import json
import math
from pathlib import Path
import threading
import time
import uuid

from .localization import (DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, map_name as localized_map_name,
                          restored_language, text as localized_text, validate_language)
from .history_store import HistoryStore

RED_TYPES = {1200039, 1200040, 1200041, 1201503, 1201504,
             1301199, 1301200, 1301244, 1301245}
STATUS_NAMES = {"seen": "刚收到", "used": "有人交互", "opened": "已开启", "unknown": "状态未知"}
REFRESH_PERIOD_SECONDS = 60 * 60
REFRESH_WINDOW_SECONDS = 5 * 60
PLAYER_POSITION_SECONDS = 10


def _timestamp(value):
    return float(value) if (isinstance(value, (int, float)) and not isinstance(value, bool)
                            and math.isfinite(value) and value > 0) else None


def _refresh_anchor(record):
    values = [_timestamp(record.get(key)) for key in
              ("last_seen", "opened_at", "received_at", "refresh_anchor_at", "first_seen")]
    return max((value for value in values if value is not None), default=None)


def _nearby(left, right):
    # Include elevation so separate floors do not share a refresh estimate.
    return math.dist([left[c] for c in ("x", "y", "z")],
                     [right[c] for c in ("x", "y", "z")]) <= 300


def _cycle_order(record):
    # Server spawn stamps are milliseconds; observation stamps are seconds.
    return (_timestamp(record.get("spawned_at")) or (_refresh_anchor(record) or 0) * 1000,
            _timestamp(record.get("received_at")) or _timestamp(record.get("first_seen")) or 0)


def _valid_record(record):
    return (isinstance(record, dict) and isinstance(record.get("id"), str)
            and type(record.get("map_id")) is int and type(record.get("template_id")) is int
            and isinstance(record.get("status"), str)
            and record["template_id"] in RED_TYPES and record["status"] in STATUS_NAMES
            and _timestamp(record.get("last_seen")) is not None
            and all(type(record.get(c)) in (int, float) and math.isfinite(record[c]) for c in ("x", "y", "z")))


class MapState:
    def __init__(self, directory: Path, maps: list, seed: dict | None = None, mode="live", readonly=False):
        self.lock = threading.RLock()
        self.store = HistoryStore(directory, readonly=readonly)
        self.directory = self.store.directory
        self.path = self.store.path
        self.maps = maps
        self.mode = mode
        self.session_id = uuid.uuid4().hex
        self.records = {}
        self.events = []
        self.bindings = {}
        self.flow_maps = {}
        self.generations = {}
        self.alert_seq = 0
        self.latest_alert = None
        self.selected_map_id = 1110
        self.current_map_id = 1110
        self.map_source = "manual"
        self.map_connection = None
        self.player = None
        self._player_connection = None
        self._player_reset_at = {}
        self._player_reset_all_at = 0
        self._map_observed_at = 0
        self.position_diagnostics = {"received": 0, "accepted": 0, "rejected": 0,
                                     "last_received_at": None, "last_accepted_at": None,
                                     "last_rejection": None, "map_invalidated_at": None,
                                     "map_invalidation_reason": None}
        self.paused = False
        self.sound = True
        self.language = DEFAULT_LANGUAGE
        self.capture = {"phase": "waiting", "detail": "正在寻找 AION2 游戏连接", "packets": 0,
                        "frames": 0, "last_packet_at": None, "last_frame_at": None,
                        "sockets": 0, "gaps": 0}
        self.storage_error = None
        self.storage_warning = None
        self._load()
        if seed and not any(r.get("source_key") == seed["source_key"] for r in self.records.values()):
            self.records[seed["id"]] = dict(seed)
        self._restore_refresh_tracking()
        self._save()

    def _same_location(self, left, right):
        return (left["map_id"] == right["map_id"]
                and left["template_id"] == right["template_id"] and _nearby(left, right))

    def _track_location(self, record, previous=None):
        nearby = [old for old in (self.records.values() if previous is None else previous)
                  if old["id"] != record["id"] and self._same_location(old, record)]
        winner = max([*nearby, record], key=_cycle_order)
        for old in [*nearby, record]:
            if old["id"] == winner["id"]:
                old.pop("superseded_by", None)
            else:
                old["superseded_by"] = winner["id"]

    def _touch(self, record, at):
        record["last_seen"] = max(_timestamp(record.get("last_seen")) or at, at)
        same_cycle = [old for old in self.records.values()
                      if _timestamp(record.get("spawned_at")) is not None
                      and old.get("spawned_at") == record.get("spawned_at")
                      and self._same_location(old, record)]
        group = same_cycle or [record]
        anchor = max(_refresh_anchor(old) or at for old in group)
        for old in group:
            old["refresh_anchor_at"] = anchor
        # Several entity IDs may refer to one confirmed spawn. Keep the visible
        # observation exhausted as soon as any of those IDs confirms an open.
        opened = [old for old in group if old["status"] == "opened"]
        if opened:
            times = [value for old in opened if (value := _timestamp(old.get("opened_at"))) is not None]
            first_open = min(times) if times else None
            for old in group:
                if old["status"] != "opened":
                    old.update(status="opened", reason="同一刷新记录此前已开启")
                old["opened_at"] = first_open

    def _restore_refresh_tracking(self):
        """Migrate existing observations without inventing timers for static map points."""
        previous = []
        for record in sorted(self.records.values(), key=_cycle_order):
            record["refresh_anchor_at"] = _refresh_anchor(record)
            same_cycle = [old for old in previous if self._same_location(old, record)
                          and _timestamp(record.get("spawned_at")) is not None
                          and old.get("spawned_at") == record["spawned_at"]]
            anchors = [value for old in [*same_cycle, record]
                       if (value := _refresh_anchor(old)) is not None]
            first_seen = [value for old in [*same_cycle, record]
                          if (value := _timestamp(old.get("first_seen"))) is not None]
            if anchors:
                for old in [*same_cycle, record]:
                    old["refresh_anchor_at"] = max(anchors)
                    if first_seen:
                        old["first_seen"] = min(first_seen)
            # Different installs may have assigned different IDs to the same spawn.
            # An already-confirmed open must survive choosing its newest observation.
            opened = [old for old in [*same_cycle, record] if old["status"] == "opened"] if same_cycle else []
            if opened:
                times = [stamp for old in opened if (stamp := _timestamp(old.get("opened_at"))) is not None]
                for old in [*same_cycle, record]:
                    old.update(status="opened", opened_at=min(times) if times else None,
                               reason="同一刷新记录此前已开启")
            self._track_location(record, previous)
            previous.append(record)

    def _display_record(self, record, now):
        row = dict(record)
        anchor = _refresh_anchor(record)
        cycle = max(1, math.floor((now - anchor - REFRESH_WINDOW_SECONDS)
                                 / REFRESH_PERIOD_SECONDS) + 1) if anchor is not None else None
        expected_at = anchor + cycle * REFRESH_PERIOD_SECONDS if anchor is not None else None
        soon_at = expected_at - REFRESH_WINDOW_SECONDS if expected_at is not None else None
        end_at = expected_at + REFRESH_WINDOW_SECONDS if expected_at is not None else None
        row.update(refresh_anchor_at=anchor, refresh_soon_at=soon_at,
                   refresh_expected_at=expected_at, refresh_window_end_at=end_at,
                   refresh_cycle=cycle,
                   refresh_estimated=anchor is not None,
                   display_status="soon" if (soon_at is not None and soon_at <= now < end_at
                                              and not record.get("superseded_by")) else record["status"])
        return row

    def _load(self, keep_preferences=False):
        data = self.store.load()
        self.storage_error = self.store.error
        if data is None:
            return
        if not keep_preferences:
            selected = data.get("selected_map_id", 1110)
            self.selected_map_id = selected if type(selected) is int and selected in {m["id"] for m in self.maps} else 1110
            self.current_map_id = self.selected_map_id
            self.sound = bool(data.get("sound", True))
            self.language = restored_language(data.get("language"))
        records = {}
        rows = data.get("records", [])
        damaged = [row for row in rows if not _valid_record(row)]
        repaired = {}
        if damaged:
            for backup_path, backup in self.store.backup_histories():
                for row in backup.get("records", []):
                    if _valid_record(row):
                        repaired.setdefault(row["id"], (row, backup_path))
        unresolved = 0
        for record in rows:
            if not _valid_record(record):
                replacement = repaired.get(record.get("id")) if isinstance(record, dict) and isinstance(record.get("id"), str) else None
                if replacement:
                    record, backup_path = replacement
                    self.store.recovered_from = str(backup_path)
                else:
                    unresolved += 1
                    continue
            record = dict(record)
            if record["id"] in records:
                continue
            record["source"] = "history"
            if record["status"] != "opened":
                record["status"] = "unknown"
                record["reason"] = "历史记录，当前状态尚未重新收到"
            records[record["id"]] = record
        if damaged and not self.store.readonly and self.path.exists():
            try:
                self.store.archive(self.path.read_bytes(), "unreadable-records")
            except OSError:
                self.store.blocked = True
                self.store.error = "部分历史记录无法读取，原文件已保留。"
                self.storage_error = self.store.error
        self.storage_warning = f"部分历史记录格式异常（{unresolved} 条），原始副本已保留。" if unresolved else None
        self.storage_error = self.store.error or self.storage_warning
        self.records = records
        self.events = [event for event in data.get("events", []) if isinstance(event, dict)][-300:]

    def _history(self):
        return {"schema": 1, "selected_map_id": self.selected_map_id, "sound": self.sound,
                "language": self.language,
                "records": [dict(row, refresh_anchor_at=_refresh_anchor(row)) for row in
                            sorted(self.records.values(), key=lambda r: r["last_seen"])],
                "events": self.events[-300:]}

    def _refresh_archive(self):
        if self.store.readonly and self.store.version() != self.store.disk_version:
            self._load(keep_preferences=True)
            self._restore_refresh_tracking()

    def export_history(self):
        with self.lock:
            self._refresh_archive()
            return json.loads(json.dumps(self._history(), ensure_ascii=False, allow_nan=False))

    def _save(self):
        try:
            self.store.save(self._history())
            self.storage_error = self.store.error or self.storage_warning
        except (OSError, ValueError, TypeError) as exc:
            self.storage_error = self.store.error or f"历史记录暂时无法保存：{type(exc).__name__}"

    def flush(self):
        with self.lock:
            self._save()

    def _event(self, kind, record, at, text):
        self.events.append({"id": uuid.uuid4().hex[:16], "kind": kind,
                            "record_id": record["id"] if record else None, "at": at, "text": text})
        self.events = self.events[-300:]

    def map_name(self, map_id):
        return next((m["name"] for m in self.maps if m["id"] == map_id), f"地图 {map_id}")

    def _invalidate_player(self, connection_id=None, at=None):
        at = _timestamp(at) or time.time()
        if connection_id is None:
            self._player_reset_all_at = max(self._player_reset_all_at, at)
        else:
            self._player_reset_at[connection_id] = max(self._player_reset_at.get(connection_id, 0), at)
        if connection_id is None or connection_id == self._player_connection:
            self.player = None
            self._player_connection = None

    def _player_position(self, event, connection, at):
        # Position is transient and belongs only to a confirmed current map/flow.
        # A manually browsed map is never evidence of the character's location.
        diagnostics = self.position_diagnostics
        diagnostics["received"] += 1
        diagnostics["last_received_at"] = max(diagnostics["last_received_at"] or 0, at)

        def reject(reason):
            diagnostics["rejected"] += 1
            diagnostics["last_rejection"] = reason

        if self.paused or self.mode != "live":
            return reject("inactive")
        if self.map_source != "detected" or self.map_connection is None:
            return reject("map_unconfirmed")
        if self.map_connection != connection or self.flow_maps.get(connection) != self.current_map_id:
            return reject("different_connection")
        if self.capture.get("phase") != "observing":
            return reject("awaiting_stream")
        if at < max(self._map_observed_at, self._player_reset_all_at, self._player_reset_at.get(connection, 0)):
            return reject("before_reset")
        if event.get("source") != "self_status" or event.get("identity_source") != "stats_consensus":
            return reject("unconfirmed_identity")
        if not all(type(event.get(c)) in (int, float) and math.isfinite(event[c])
                   and abs(event[c]) <= 1_000_000 for c in ("x", "y", "z")):
            return reject("invalid_coordinates")
        if self.player and at < self.player["observed_at"]:
            return reject("out_of_order")
        # Several ordered subframes can share one packet timestamp.
        self.player = {c: float(event[c]) for c in ("x", "y", "z")}
        self.player.update(map_id=self.current_map_id, observed_at=at,
                           expires_at=at + PLAYER_POSITION_SECONDS,
                           source="self_status", identity_source="stats_consensus")
        self._player_connection = connection
        diagnostics["accepted"] += 1
        diagnostics["last_accepted_at"] = at
        diagnostics["last_rejection"] = None

    def _player_snapshot(self, now):
        if (not self.player or self.paused or self.map_source != "detected"
                or self._player_connection != self.map_connection
                or self.player["map_id"] != self.current_map_id):
            return None
        return dict(self.player, stale=now >= self.player["expires_at"])

    def _position_diagnostics_snapshot(self, now):
        player = self._player_snapshot(now)
        if self.mode != "live" or self.paused:
            phase = "inactive"
        elif self.map_source != "detected":
            phase = "awaiting_map"
        elif self.capture.get("phase") != "observing":
            phase = "awaiting_stream"
        elif player:
            phase = "stale" if player["stale"] else "tracking"
        else:
            phase = "awaiting_position"
        return dict(self.position_diagnostics, state=phase)

    def _invalidate(self, connection_id=None, reason="连接中断，当前状态未知", at=None):
        at = at or time.time()
        self._invalidate_player(connection_id, at)
        changed = False
        for record in self.records.values():
            if record.get("session_id") != self.session_id:
                continue
            if connection_id is not None and record.get("connection_id") != connection_id:
                continue
            if record["status"] in ("seen", "used"):
                record["status"] = "unknown"
                record["reason"] = reason
                self._event("unknown", record, at, f"{self.map_name(record['map_id'])} · {reason}")
                changed = True
        if connection_id is None:
            self.bindings.clear()
        else:
            self.bindings = {k: v for k, v in self.bindings.items() if k[0] != connection_id}
        if changed:
            self._save()

    def capture_status(self, status):
        with self.lock:
            self.capture.update(status)
            phase = self.capture.get("phase")
            if phase in ("paused", "error", "waiting", "stopped"):
                self._invalidate(reason="接收暂停或连接中断，当前状态未知")

    def set_paused(self, paused):
        with self.lock:
            self.paused = bool(paused)
            if paused:
                self._invalidate(reason="接收已暂停，当前状态未知")
                self.flow_maps.clear()
                self.map_source = "manual"
                self.map_connection = None
                self.capture.update(phase="paused", detail="已暂停接收")
            else:
                self.capture.update(phase="waiting", detail="正在重新同步游戏连接")

    def select_map(self, map_id):
        with self.lock:
            if map_id not in {m["id"] for m in self.maps}:
                raise ValueError("未知地图")
            self.selected_map_id = map_id
            if self.map_source == "manual" and self.current_map_id != map_id:
                self._invalidate(reason="手动地图已变更，旧记录状态未知")
                self.current_map_id = map_id
            self._save()

    def set_sound(self, enabled):
        with self.lock:
            self.sound = bool(enabled)
            self._save()

    def set_language(self, language):
        language = validate_language(language)
        with self.lock:
            if self.language != language:
                self.language = language
                self._save()

    def handle(self, event):
        """Return an alert only for a newly observed unopened red object."""
        with self.lock:
            kind = event.get("kind")
            connection = str(event.get("connection_id", "default"))
            at = _timestamp(event.get("at", time.time()))
            if at is None:
                return
            if kind == "player_position_reset":
                self._invalidate_player(connection, at)
                return
            if kind in ("disconnect", "gap", "stream_reset", "map_reset"):
                reason = str(event.get("reason") or "连接或数据同步中断，当前状态未知")[:200]
                self._invalidate(connection, reason, at)
                self.generations[connection] = self.generations.get(connection, 0) + 1
                self.flow_maps.pop(connection, None)
                if self.map_connection == connection:
                    self.map_source = "manual"
                    self.map_connection = None
                    self.position_diagnostics.update(map_invalidated_at=at, map_invalidation_reason=reason)
                return
            if kind == "map":
                # A response about a browsed map does not locate the player's character.
                if self.paused or event.get("map_source") == "world_map_response":
                    return
                if at < self._map_observed_at:
                    return
                map_id = int(event["map_id"])
                previous = self.flow_maps.get(connection)
                if previous != map_id or event.get("reset"):
                    self._invalidate(connection, "换图或切换场景，旧记录状态未知", at)
                    self.generations[connection] = self.generations.get(connection, 0) + 1
                if self.map_connection != connection or self.current_map_id != map_id:
                    self._invalidate_player(at=at)
                self.flow_maps[connection] = map_id
                self._map_observed_at = at
                # Follow the actual game map when the view was following the previous one.
                if self.selected_map_id == self.current_map_id:
                    self.selected_map_id = map_id
                self.current_map_id = map_id
                self.map_source = "detected"
                self.map_connection = connection
                self.position_diagnostics.update(map_invalidated_at=None, map_invalidation_reason=None)
                self._save()
                return
            if self.paused:
                return
            if kind == "player_position":
                self._player_position(event, connection, at)
                return
            if "entity_id" not in event:
                return
            entity = int(event["entity_id"])
            map_id = self.flow_maps.get(connection, self.current_map_id)
            key = (connection, self.generations.get(connection, 0), map_id, entity)
            record = self.records.get(self.bindings.get(key))
            if kind == "object":
                if event.get("template_id") not in RED_TYPES:
                    return
                if not all(isinstance(event.get(c), (int, float)) and math.isfinite(event[c]) and abs(event[c]) <= 10_000_000 for c in ("x", "y", "z")):
                    return
                spawned = int(event.get("spawned_at") or 0)
                initial_opened = list(event.get("states", []))[:2] == [4, 3]
                same = (record and record["template_id"] == event["template_id"]
                        and record.get("spawned_at", 0) == spawned
                        and _nearby(record, event))
                if same:
                    if initial_opened and record["status"] != "opened":
                        record.update(status="opened", opened_at=at, reason="收到开启 / 耗尽状态")
                        self._event("opened", record, at, "红色背包已开启 / 耗尽")
                    elif not initial_opened and record["status"] == "unknown":
                        record.update(status="seen", reason="再次收到同一对象，未收到新的开启状态")
                        self._event("seen", record, at, "再次收到同一红色背包记录")
                    self._touch(record, at)
                    self._save()
                    return
                if record and record["status"] in ("seen", "used"):
                    record.update(status="unknown", reason="对象标识被重新使用，原记录状态未知")
                # Re-observation after moving out of range is not necessarily a new spawn.
                duplicate = None
                for old in reversed(list(self.records.values())):
                    if old["map_id"] != map_id or old["template_id"] != event["template_id"]:
                        continue
                    if not _nearby(old, event):
                        continue
                    if spawned > 0 and old.get("spawned_at") == spawned:
                        duplicate = old
                        break
                    if spawned <= 0 and at - old["last_seen"] < 900:
                        duplicate = old
                        break
                record_id = uuid.uuid4().hex[:16]
                anchor = max(_refresh_anchor(duplicate) or at, at) if duplicate else at
                first_seen = min(_timestamp(duplicate.get("first_seen")) or at, at) if duplicate else at
                record = {"id": record_id, "map_id": map_id, "entity_id": entity,
                          "template_id": int(event["template_id"]),
                          "x": round(event["x"], 3), "y": round(event["y"], 3), "z": round(event["z"], 3),
                          "spawned_at": spawned, "status": "opened" if initial_opened else "seen",
                          "first_seen": first_seen,
                          "refresh_anchor_at": anchor, "received_at": at, "last_seen": at,
                          "opened_at": at if initial_opened else None,
                          "source": self.mode, "session_id": self.session_id,
                          "connection_id": connection, "map_source": "detected" if connection in self.flow_maps else "manual",
                          "reason": "收到开启 / 耗尽状态" if initial_opened else "本机刚收到对象记录，尚未收到开启状态"}
                # Keep an observed exhausted object exhausted when it is merely re-sent.
                if duplicate and duplicate["status"] == "opened":
                    record.update(status="opened", opened_at=duplicate.get("opened_at"), reason="同一刷新记录此前已开启")
                self.records[record_id] = record
                self._touch(record, at)
                self._track_location(record)
                self.bindings[key] = record_id
                self._event(record["status"], record, at, f"{self.map_name(map_id)} · 红色背包{STATUS_NAMES[record['status']]}")
                alert = None
                if record["status"] == "seen" and duplicate is None and not record.get("superseded_by"):
                    self.alert_seq += 1
                    title = localized_text("new_bag", self.language)
                    name = localized_map_name(self.maps, map_id, self.language)
                    alert = {"seq": self.alert_seq, "record_id": record_id, "at": at,
                             "text": f"{name} · {title} · X {event['x']:.0f} / Y {event['y']:.0f}",
                             "title": title, "language": self.language, "map_id": map_id,
                             "x": event["x"], "y": event["y"], "z": event["z"]}
                    self.latest_alert = alert
                self._save()
                return alert
            if not record:
                return
            if kind == "state" and (event.get("a"), event.get("b")) == (4, 3):
                if record["status"] != "opened":
                    record.update(status="opened", opened_at=at, reason="收到开启 / 耗尽状态")
                    self._event("opened", record, at, "红色背包已开启 / 耗尽")
                self._touch(record, at)
                self._save()
            elif kind == "use":
                if record["status"] == "seen":
                    record.update(status="used", reason="收到交互记录，等待开启状态")
                    self._event("used", record, at, "有人与红色背包交互")
                self._touch(record, at)
                self._save()
            elif kind == "gone":
                if record["status"] in ("seen", "used"):
                    record.update(status="unknown", reason="对象离开本机接收范围，是否开启未知")
                    self._event("unknown", record, at, "背包离开接收范围，状态未知")
                self._touch(record, at)
                self.bindings.pop(key, None)
                self._save()

    def snapshot(self):
        with self.lock:
            self._refresh_archive()
            now = time.time()
            last_frame = self.capture.get("last_frame_at")
            if self.mode == "live" and last_frame and now - last_frame > 20:
                self._invalidate(reason="超过 20 秒未收到有效游戏记录，当前状态未知")
            counts = {}
            for record in self.records.values():
                key = str(record["map_id"])
                counts[key] = counts.get(key, 0) + 1
            storage = {"directory": str(self.directory), "history_file": str(self.path),
                       "record_count": len(self.records), "map_counts": counts,
                       "last_saved_at": self.store.last_saved_at, "backup_count": self.store.backup_count(),
                       "recovered_from": self.store.recovered_from, "error": self.storage_error,
                       "readonly": self.store.readonly}
            result = {"app": "AION2 RedBag Map", "version": "1.2.0", "mode": self.mode,
                      "now": now, "session_id": self.session_id, "maps": self.maps,
                      "selected_map_id": self.selected_map_id, "current_map_id": self.current_map_id,
                      "map_source": self.map_source, "capture": dict(self.capture),
                      "player": self._player_snapshot(now),
                      "position_diagnostics": self._position_diagnostics_snapshot(now),
                      "paused": self.paused, "sound": self.sound, "language": self.language,
                      "supported_languages": list(SUPPORTED_LANGUAGES),
                      "records": [self._display_record(r, now) for r in
                                  sorted(self.records.values(), key=lambda r: r["last_seen"], reverse=True)],
                      "events": list(reversed(self.events[-60:])), "alert_seq": self.alert_seq,
                      "latest_alert": self.latest_alert, "storage_error": self.storage_error, "storage": storage,
                      "coverage": "只显示本机实际收到的记录；全图广播尚未验证；服务器和线路尚未识别"}
            # The HTTP thread must not iterate dictionaries being mutated by capture.
            return json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
