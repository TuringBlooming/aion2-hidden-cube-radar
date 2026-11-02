"""Independent topmost map. Uses only the local map API and tool-owned files.

No game process/window discovery, capture, input injection, or sound playback.
All Win32 window calls target the Tk window created by this module.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import ctypes
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .history_store import atomic_write
from .localization import map_name, restored_language
from .overlay_text import text
from .resources import load_maps
from .map_markers import marker_image, MARKER_COLORS
from .ui_theme import enable_dpi_awareness, ui_scale
from . import __version__

ROOT = Path(__file__).resolve().parent
OVERLAY_VERSION = __version__
FOLLOW_CENTERING_VERSION = 1
BACKGROUND = "#101722"
PANEL = "#182331"
BORDER = "#293747"
MUTED = "#91a2b5"
TRANSPARENT = "#010203"
COLORS = {key: MARKER_COLORS[key] for key in ("seen", "used", "opened", "unknown", "soon", "candidate")}
PLAYER_COLOR = "#54d8ea"
PLAYER_MAX_AGE = 10
POLL_INTERVAL = .2
UI_TICK_MS = 100
TILE_CACHE_LIMIT = 24
DEFAULT_FOLLOW_ZOOM = 20
BAG_FOCUS_SPAN = 24000  # About 240 metres across the smaller viewport dimension.
OVERLAY_WIDTH = 400
HEADER_HEIGHT = 52
EXPANDED_HEIGHT = 616


def positive(value):
    return float(value) if type(value) in (float, int) and math.isfinite(value) and value > 0 else None


def latest_time(record):
    values = [value for key in ("last_seen", "opened_at", "received_at", "refresh_anchor_at", "first_seen")
              if (value := positive(record.get(key))) is not None]
    return max(values) if values else None


def display_status(record, now):
    if record.get("kind") == "historical_candidate":
        return "candidate"
    anchor = latest_time(record)
    if anchor is not None and not record.get("superseded_by"):
        cycle = max(1, math.floor((now - anchor - 300) / 3600) + 1)
        expected = anchor + cycle * 3600
        if expected - 300 <= now < expected + 300:
            return "soon"
    return record.get("status") if record.get("status") in COLORS else "unknown"


def visible_records(snapshot, map_id):
    return [row for row in snapshot.get("records", [])
            if isinstance(row, dict) and isinstance(row.get("id"), str)
            and row.get("map_id") == map_id and not row.get("superseded_by")
            and all(type(row.get(c)) in (int, float) and math.isfinite(row[c]) for c in ("x", "y"))]


def load_candidates(path=None):
    """Load historical locations locally. They never enter live records or alerts."""
    try:
        data = json.loads(Path(path or ROOT / "static/candidates.json").read_text("utf-8"))
        groups = data.get("maps", {})
        if not isinstance(groups, dict):
            return {}
        result = {}
        for group in groups.values():
            if not isinstance(group, dict) or type(group.get("map_id")) is not int:
                continue
            rows = group.get("points")
            if not isinstance(rows, list):
                continue
            map_id = group["map_id"]
            result[map_id] = [dict(row, kind="historical_candidate") for row in rows
                              if isinstance(row, dict) and isinstance(row.get("id"), str)
                              and row["id"].startswith(f"candidate-{map_id}-") and row.get("map_id") == map_id
                              and type(row.get("ordinal")) is int and row["ordinal"] > 0
                              and all(type(row.get(c)) in (int, float) and math.isfinite(row[c]) for c in ("x", "y"))]
        return result
    except (OSError, ValueError, TypeError):
        return {}


def local_clock(value):
    return datetime.fromtimestamp(value, timezone(timedelta(hours=8))).strftime("%H:%M:%S") if positive(value) else "—"


def player_reference(snapshot, connected, at):
    """Return a bounded observation, never extrapolated movement or facing."""
    row = snapshot.get("player")
    if (not isinstance(row, dict) or type(row.get("map_id")) is not int
            or not all(type(row.get(axis)) in (int, float) and math.isfinite(row[axis]) for axis in ("x", "y", "z"))
            or positive(row.get("observed_at")) is None
            or row.get("source") != "self_status" or row.get("identity_source") != "stats_consensus"):
        return None
    result = dict(row)
    observed = row["observed_at"]
    expires = min(positive(row.get("expires_at")) or observed, observed + PLAYER_MAX_AGE)
    result["age_seconds"] = max(0, int(at - observed))
    result["fresh"] = bool(connected and snapshot.get("mode") == "live" and not snapshot.get("paused")
                           and snapshot.get("capture", {}).get("phase") == "observing"
                           and snapshot.get("current_map_id") == row["map_id"]
                           and not row.get("stale") and observed <= at < expires)
    return result


def approximate_distance(player, record):
    if (not player or not player.get("fresh") or not record or record.get("map_id") != player["map_id"]
            or not all(type(record.get(axis)) in (int, float) and math.isfinite(record[axis]) for axis in ("x", "y"))):
        return None
    return math.hypot(record["x"] - player["x"], record["y"] - player["y"]) / 100


def bag_focus_scale(width, height, fit_scale, current_scale):
    """Reveal local terrain without pulling an existing close view backwards."""
    close_scale = min(max(1, min(width, height)) / BAG_FOCUS_SPAN, fit_scale * 40)
    return max(current_scale, close_scale)


def target_indicator(x, y, width, height, margin=34):
    """Project a selected point onto this viewport, not a character's facing."""
    cx, cy = width / 2, height / 2
    rx, ry = max(1, cx - margin), max(1, cy - margin)
    dx, dy = x - cx, y - cy
    outside = not (0 <= x < width and 0 <= y < height)
    factor = max(abs(dx) / rx, abs(dy) / ry, 1) if outside else 1
    length = math.hypot(dx, dy)
    return {"x": max(cx - rx, min(cx + rx, cx + dx / factor)) if outside else x,
            "y": max(cy - ry, min(cy + ry, cy + dy / factor)) if outside else y, "outside": outside,
            "ux": dx / length if length else 0, "uy": dy / length if length else -1}


def visible_tiles(bounds, cx, cy, scale, width, height):
    """Yield only visible tile bounds and their clipped destination rectangles."""
    grid = bounds.get("tiles")
    if (not isinstance(grid, (list, tuple)) or len(grid) != 2
            or not all(type(size) is int and size > 0 for size in grid)):
        return
    cols, rows = grid
    tw, th = bounds["w"] / cols, bounds["h"] / rows
    left, top = cx - width / (2 * scale), cy - height / (2 * scale)
    right, bottom = cx + width / (2 * scale), cy + height / (2 * scale)
    c0, c1 = max(0, math.floor((left - bounds["x"]) / tw)), min(cols, math.ceil((right - bounds["x"]) / tw))
    r0, r1 = max(0, math.floor((top - bounds["y"]) / th)), min(rows, math.ceil((bottom - bounds["y"]) / th))
    for row in range(r0, r1):
        for col in range(c0, c1):
            tx, ty = bounds["x"] + col * tw, bounds["y"] + row * th
            # Partition by destination pixel centres, avoiding holes at fractional tile edges.
            box = (max(0, math.ceil((tx - left) * scale - .5)),
                   max(0, math.ceil((ty - top) * scale - .5)),
                   min(width, math.ceil((tx + tw - left) * scale - .5)),
                   min(height, math.ceil((ty + th - top) * scale - .5)))
            if box[2] > box[0] and box[3] > box[1]:
                yield col, row, {"x": tx, "y": ty, "w": tw, "h": th}, box


def render_map_frame(base, bounds, cx, cy, scale, width, height, tile_loader=None):
    """Rasterize a viewport from the overview plus cached native-detail tiles."""
    from PIL import Image

    def raster(source, region, box, resample):
        left, top, right, bottom = box
        affine = (source.width / (region["w"] * scale), 0,
                  (cx + (left - width / 2) / scale - region["x"]) * source.width / region["w"],
                  0, source.height / (region["h"] * scale),
                  (cy + (top - height / 2) / scale - region["y"]) * source.height / region["h"])
        return source.transform((right - left, bottom - top), Image.Transform.AFFINE, affine,
                                resample, fillcolor="#d4c8b2")

    frame = raster(base, bounds, (0, 0, width, height), Image.Resampling.BICUBIC)
    # The overview is sufficient at small scales; tiles matter as its source pixels become visible.
    if tile_loader and max(bounds["w"] * scale / base.width, bounds["h"] * scale / base.height) >= .5:
        for col, row, region, box in visible_tiles(bounds, cx, cy, scale, width, height):
            tile = tile_loader(col, row)
            if tile is not None:
                frame.paste(raster(tile, region, box, Image.Resampling.BICUBIC), box[:2])
    return frame


def queue_latest_event(events, item):
    """Coalesce visual snapshots, retaining the last success before disconnection.

    Successful snapshots retain the service's monotonic alert_seq/latest_alert,
    so a position-only update does not erase a pending new-bag notification.
    """
    previous = []
    try:
        while True:
            previous.append(events.get_nowait())
    except queue.Empty:
        pass
    if not item[0]:
        last_success = next((entry for entry in reversed(previous) if entry[0]), None)
        if last_success:
            events.put_nowait(last_success)
    events.put_nowait(item)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("The overlay only uses its local API")


class OverlayState:
    """UI-only state: never alters observations or creates a new bag alert."""
    def __init__(self, follow=True, *, baseline_session=None, baseline_seq=None):
        self.snapshot = {}
        self.baseline_supplied = bool(isinstance(baseline_session, str) and baseline_session
                                      and type(baseline_seq) is int and baseline_seq >= 0)
        self.session = baseline_session if self.baseline_supplied else None
        self.seq = baseline_seq if self.baseline_supplied else 0
        self.map_id = 1110
        self.selected = None
        self.connected = False
        self.new_alert = None
        self.alert_until = 0
        self.follow = bool(follow)
        self.last_player = None

    def player(self, at=None):
        at = at if at is not None else time.time()
        current_map = self.snapshot.get("current_map_id")
        if type(current_map) is not int:
            return None
        current = player_reference(self.snapshot, self.connected, at)
        if current and current["map_id"] == current_map:
            return current
        if self.last_player and self.last_player["map_id"] == current_map:
            return player_reference({**self.snapshot, "player": {**self.last_player, "stale": True}}, False, at)
        return None

    def follow_target(self, at=None):
        player = self.player(at)
        # Keep the last observation centred even while stale; never extrapolate it.
        return player if self.follow and player else None

    def set_follow(self, enabled):
        self.follow = bool(enabled)

    def clear_target(self):
        self.selected = None
        self.new_alert = None
        self.alert_until = 0

    def accept(self, snapshot, at=None):
        if not isinstance(snapshot, dict) or snapshot.get("app") != "AION2 RedBag Map":
            raise ValueError("unexpected local application")
        at = at if at is not None else time.time()
        session = snapshot.get("session_id")
        seq = snapshot.get("alert_seq", 0)
        seq = seq if type(seq) is int and seq >= 0 else 0
        new_session = session != self.session or (not self.snapshot and not self.baseline_supplied)
        self.baseline_supplied = False
        previous_map = self.snapshot.get("current_map_id")
        current_map = snapshot.get("current_map_id")
        current_map = current_map if type(current_map) is int else None
        self.snapshot = snapshot
        self.connected = True
        self.new_alert = None
        if new_session or current_map != previous_map:
            self.last_player = None
            self.alert_until = 0
        player = player_reference(snapshot, True, at)
        if player and player["map_id"] == current_map:
            self.last_player = dict(snapshot["player"])
        following = self.follow_target(at)
        if new_session:
            if current_map is not None:
                self.map_id = current_map
            self.selected = None
            self.session, self.seq = session, seq
            return {"fit": True, "alert": None, "player": following}
        map_changed = current_map is not None and current_map != previous_map
        if map_changed:
            self.map_id = current_map
            self.selected = None
        alert = snapshot.get("latest_alert")
        if seq > self.seq and snapshot.get("mode") == "live" and isinstance(alert, dict):
            row = next((r for r in snapshot.get("records", []) if isinstance(r, dict) and r.get("id") == alert.get("record_id")), None)
            if row and type(row.get("map_id")) is int and all(type(row.get(c)) in (int, float) and math.isfinite(row[c]) for c in ("x", "y")):
                if not self.follow:
                    self.map_id = row["map_id"]
                self.selected = row["id"]
                self.new_alert = row
                self.alert_until = at + 20
        self.seq = max(self.seq, seq)
        if following:
            self.map_id = following["map_id"]
        return {"fit": map_changed, "alert": self.new_alert, "player": following}

    def disconnect(self):
        self.connected = False


class AlertVisibility:
    """Own-window visibility, with an explicit live-alert baseline, never history."""
    def __init__(self, waiting=False, *, baseline_session=None, baseline_seq=None):
        self.enabled = bool(waiting)
        self.visible = not waiting
        self.initialized = bool(isinstance(baseline_session, str) and baseline_session
                                and type(baseline_seq) is int and baseline_seq >= 0)
        self.session = baseline_session if self.initialized else None
        self.seq = baseline_seq if self.initialized else 0
        self.snapshot_time = None

    def arm(self):
        self.enabled = True
        self.visible = False

    def manual_show(self):
        self.visible = True

    def accept(self, snapshot, alert):
        session, seq = snapshot.get("session_id"), snapshot.get("alert_seq", 0)
        seq = seq if type(seq) is int and seq >= 0 else 0
        now = positive(snapshot.get("now"))
        if self.initialized and session == self.session and now and self.snapshot_time and now < self.snapshot_time:
            return None
        enabled = snapshot.get("overlay_on_alert") is True
        first = not self.initialized
        new_session = first or session != self.session
        old_visible, old_enabled = self.visible, self.enabled
        if new_session:
            # A manual initial open remains visible, even when the saved mode is on.
            # Subsequent service replacement resets an armed window to waiting.
            if not first or old_enabled:
                self.visible = not enabled
            self.session, self.seq = session, seq
            self.initialized = True
        else:
            if enabled != old_enabled:
                self.visible = not enabled
            current = snapshot.get("latest_alert")
            is_live_alert = (seq > self.seq and snapshot.get("mode") == "live"
                             and isinstance(session, str) and bool(session)
                             and not snapshot.get("paused")
                             and snapshot.get("capture", {}).get("phase") == "observing"
                             and isinstance(current, dict) and current.get("seq") == seq
                             and current.get("test") is not True and isinstance(alert, dict)
                             and alert.get("id") == current.get("record_id")
                             and alert.get("source") not in ("history", "replay"))
            if enabled and is_live_alert:
                self.visible = True
            self.seq = max(self.seq, seq)
        self.enabled = enabled
        self.snapshot_time = now
        return ("show" if self.visible else "hide") if self.visible != old_visible else None


def consume_show_command(path, *, pid, api, started_at, now):
    """Consume a fresh command only for this exact tool-owned overlay instance."""
    path = Path(path)
    try:
        if path.stat().st_size > 4096:
            return False
        data = json.loads(path.read_text("utf-8"))
        if (not isinstance(data, dict) or type(data.get("pid")) is not int
                or data.get("pid") != pid or data.get("api") != api):
            return False
        requested = positive(data.get("requested_at"))
        if not requested or not max(started_at, now - 10) <= requested <= now + 1:
            return False
        path.unlink()
        return True
    except (OSError, ValueError, UnicodeError):
        return False


class OverlayLock:
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        if self.stream.seek(0, os.SEEK_END) == 0:
            self.stream.write(b"\0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise OSError("The desktop map is already open")
        return self

    def __exit__(self, *_args):
        if self.stream:
            self.stream.close()
            self.stream = None


def load_settings(path):
    try:
        data = json.loads(Path(path).read_text("utf-8"))
        if not isinstance(data, dict):
            return {}
        result = {}
        for key in ("x", "y"):
            if type(data.get(key)) is int and -100000 <= data[key] <= 100000:
                result[key] = data[key]
        result["alpha"] = data.get("alpha") if type(data.get("alpha")) in (int, float) and .45 <= data["alpha"] <= 1 else 1.0
        result["collapsed"] = data.get("collapsed") is True
        result["layer"] = data.get("layer") is True
        result["show_candidates"] = data.get("show_candidates") is not False
        version = data.get("follow_centering_version")
        result["follow_centering_version"] = version if type(version) is int else 0
        result["follow"] = (data.get("follow") is not False
                            if result["follow_centering_version"] >= FOLLOW_CENTERING_VERSION else True)
        zoom = data.get("zoom_ratio")
        if type(zoom) in (int, float) and math.isfinite(zoom) and .5 <= zoom <= 40:
            result["zoom_ratio"] = float(zoom)
        return result
    except (OSError, ValueError, UnicodeError):
        return {}


def own_window_style(tk_root, *, show=True):
    """Only obtain/style this Tk window's own wrapper. Never inspect another app."""
    if os.name != "nt":
        return {"topmost": True, "no_activate": False}
    from ctypes import wintypes as W
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetAncestor.argtypes = [W.HWND, W.UINT]
    user.GetAncestor.restype = W.HWND
    user.GetWindowLongW.argtypes = [W.HWND, ctypes.c_int]
    user.GetWindowLongW.restype = W.LONG
    user.SetWindowLongW.argtypes = [W.HWND, ctypes.c_int, W.LONG]
    user.SetWindowLongW.restype = W.LONG
    user.SetWindowPos.argtypes = [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT]
    user.SetWindowPos.restype = W.BOOL
    hwnd = user.GetAncestor(tk_root.winfo_id(), 2) or tk_root.winfo_id()
    style = user.GetWindowLongW(hwnd, -20)
    user.SetWindowLongW(hwnd, -20, (style | 0x08000000 | 0x00000080) & ~0x00040000)
    # Topmost, same rectangle, show without activating.
    flags = 0x0001 | 0x0002 | 0x0010 | 0x0020
    user.SetWindowPos(hwnd, W.HWND(-1), 0, 0, 0, 0, flags | (0x0040 if show else 0))
    actual = user.GetWindowLongW(hwnd, -20)
    return {"hwnd": int(hwnd), "topmost": bool(actual & 0x00000008), "no_activate": bool(actual & 0x08000000),
            "tool_window": bool(actual & 0x00000080)}


def show_without_activation(tk_root):
    """Map our Tk wrapper without activating it or inspecting another window."""
    flags = own_window_style(tk_root, show=False)
    if os.name != "nt":
        tk_root.deiconify()
        return flags
    from ctypes import wintypes as W
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.EnableWindow.argtypes = [W.HWND, W.BOOL]
    user.EnableWindow.restype = W.BOOL
    user.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
    user.ShowWindow.restype = W.BOOL
    hwnd = flags["hwnd"]
    # Tk must update its own withdrawn state. Temporarily disabling this owned
    # wrapper prevents its ordinary deiconify from activating it on Windows.
    user.EnableWindow(hwnd, False)
    try:
        tk_root.deiconify()
        user.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE, never SW_SHOWNORMAL.
    finally:
        user.EnableWindow(hwnd, True)
    return own_window_style(tk_root, show=True)  # Includes SWP_NOACTIVATE.


class DesktopOverlay:
    def __init__(self, port, settings_path, status_path, stop_path=None, *, hidden_until_alert=False,
                 alert_baseline_session=None, alert_baseline_seq=None):
        import tkinter as tk
        from PIL import Image, ImageTk
        self.tk, self.Image, self.ImageTk = tk, Image, ImageTk
        self.settings_path, self.status_path = Path(settings_path), Path(status_path)
        self.stop_path = Path(stop_path) if stop_path else self.status_path.with_name("overlay.stop")
        self.show_path = self.status_path.with_name("overlay.show")
        self.started_at = time.time()
        self.settings = load_settings(self.settings_path)
        self.state = OverlayState(follow=self.settings.get("follow", True),
                                  baseline_session=alert_baseline_session, baseline_seq=alert_baseline_seq)
        self.visibility = AlertVisibility(hidden_until_alert, baseline_session=alert_baseline_session,
                                          baseline_seq=alert_baseline_seq)
        self.hide_pending = False
        self.visibility_error = False
        self.control_events = queue.Queue()
        self.maps = load_maps(self.settings_path.parent)
        self.candidates = load_candidates()
        self.show_candidates = self.settings.get("show_candidates", True)
        self.candidate_render_key = None
        self.candidate_click_points = []
        self.map_assets = self.settings_path.parent / "assets/maps"
        self.language = "zh-CN"
        self.port = port
        self.events = queue.Queue(maxsize=2)
        self.stopping = threading.Event()
        self.photos = {}
        self.tile_photos = OrderedDict()
        self.raster_mode = "waiting"
        self.render_key = None
        self.photo = None
        self.cx = self.cy = 0
        self.scale = 1
        self.fit_scale = 1
        self.zoom_ratio = self.settings.get("zoom_ratio")
        self.width, self.height = 360, 300
        self.collapsed = self.settings.get("collapsed", False)
        self.layer = self.settings.get("layer", False)
        self.alpha = self.settings.get("alpha", 1.0)
        self.drag_origin = None
        self.map_drag = None
        self.map_dragged = False
        self.click_points = []
        self.window_flags = {}
        self.marker_photos = {}
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.withdraw()
        self.dpi_scale = ui_scale(self.root)
        p = self.px
        self.root.title(text("title"))
        self.root.overrideredirect(True)
        self.root.configure(bg=BORDER, padx=1, pady=1)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self.alpha)
        self.root.minsize(p(OVERLAY_WIDTH), p(HEADER_HEIGHT))
        # Older DPI-unaware windows stored 96-DPI coordinates. Keep that format
        # so an upgrade preserves the apparent position at 125/150/200 percent.
        x = round(self.settings["x"] * self.dpi_scale) if "x" in self.settings else max(20, self.root.winfo_screenwidth() - p(OVERLAY_WIDTH + 30))
        y = round(self.settings.get("y", 60) * self.dpi_scale)
        # Saved locations from a disconnected monitor still leave a reachable title.
        x = max(0, min(x, max(0, self.root.winfo_screenwidth() - p(OVERLAY_WIDTH))))
        y = max(0, min(y, max(0, self.root.winfo_screenheight() - p(HEADER_HEIGHT if self.collapsed else EXPANDED_HEIGHT))))
        self.root.geometry(f"{p(OVERLAY_WIDTH)}x{p(HEADER_HEIGHT if self.collapsed else EXPANDED_HEIGHT)}+{x}+{y}")
        self.root.bind("<Escape>", lambda _event: self.clear_target() if self.state.selected else self.toggle_collapse())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.header = tk.Frame(self.root, bg=PANEL, height=p(HEADER_HEIGHT))
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        self.brand_icon = tk.Label(self.header, image=self.marker_photo("seen", 28), bg=PANEL)
        self.brand_icon.pack(side="left", padx=(p(12), p(7)))
        self.title_label = tk.Label(self.header, text=text("title"), bg=PANEL, fg="#edf2f7",
                                    font=("Microsoft YaHei UI", 12, "bold"), anchor="w")
        self.title_label.pack(side="left", fill="x", expand=True)
        self.fold_button = self.button(self.header, "+" if self.collapsed else "−", self.toggle_collapse, width=3)
        self.fold_button.pack(side="right", padx=(p(3), p(8)), pady=p(10))
        self.close_button = self.button(self.header, "×", self.close, width=3)
        self.close_button.pack(side="right", padx=p(2), pady=p(10))
        self.hide_button = self.button(self.header, text("hide_until_alert"), self.hide_until_alert)
        self.hide_button.pack(side="right", padx=p(2), pady=p(10))
        for widget in (self.header, self.title_label, self.brand_icon):
            widget.bind("<ButtonPress-1>", self.begin_drag)
            widget.bind("<B1-Motion>", self.drag_window)
            widget.bind("<ButtonRelease-1>", lambda _event: self.save_settings())
        self.body = tk.Frame(self.root, bg=BACKGROUND)
        self.status_label = tk.Label(self.body, text=text("waiting"), anchor="w", bg=BACKGROUND, fg=MUTED,
                                     font=("Microsoft YaHei UI", 9), padx=p(13), pady=p(7))
        self.status_label.pack(fill="x")
        self.player_row = tk.Frame(self.body, bg=BACKGROUND)
        self.player_row.pack(fill="x", padx=p(12), pady=(0, p(7)))
        self.player_label = tk.Label(self.player_row, text=text("player_waiting"), anchor="w", bg=BACKGROUND,
                                    fg=MUTED, font=("Microsoft YaHei UI", 9))
        self.player_label.pack(side="left", fill="x", expand=True)
        self.follow_button = self.button(self.player_row, text("follow_waiting"), self.toggle_follow)
        self.follow_button.pack(side="right")
        self.toolbar = tk.Frame(self.body, bg=BACKGROUND)
        self.toolbar.pack(fill="x", padx=p(10), pady=(0, p(6)))
        self.fit_button = self.button(self.toolbar, text("fit"), self.fit)
        self.fit_button.pack(side="left", padx=p(2))
        self.latest_button = self.button(self.toolbar, text("latest"), self.locate_latest)
        self.latest_button.pack(side="left", padx=p(2))
        self.layer_button = self.button(self.toolbar, text("layer"), self.toggle_layer)
        self.layer_button.pack(side="left", padx=p(2))
        self.alpha_button = self.button(self.toolbar, f"{self.alpha:.0%}", self.change_alpha, width=4)
        self.alpha_button.pack(side="right", padx=p(2))
        self.point_toolbar = tk.Frame(self.body, bg=BACKGROUND)
        self.point_toolbar.pack(fill="x", padx=p(10), pady=(0, p(10)))
        self.candidate_button = self.button(self.point_toolbar, text("candidates", count=0), self.toggle_candidates)
        self.candidate_button.pack(side="left", padx=p(2))
        self.clear_button = self.button(self.point_toolbar, text("clear_target"), self.clear_target)
        self.clear_button.pack(side="right", padx=p(2))
        self.canvas = tk.Canvas(self.body, width=p(OVERLAY_WIDTH), height=p(330), bg="#d4c8b2", highlightthickness=0, cursor="fleur")
        self.canvas.bind("<Configure>", self.resize)
        self.canvas.bind("<MouseWheel>", lambda event: self.zoom(1.3 if event.delta > 0 else 1 / 1.3, event.x, event.y))
        self.canvas.bind("<ButtonPress-1>", self.begin_pan)
        self.canvas.bind("<B1-Motion>", self.pan)
        self.canvas.bind("<ButtonRelease-1>", self.end_pan)
        self.canvas.bind("<Button-3>", lambda _event: self.clear_target())
        zoom_controls = tk.Frame(self.canvas, bg=BORDER, padx=1, pady=1)
        zoom_controls.place(relx=1, x=-p(10), y=p(10), anchor="ne")
        self.button(zoom_controls, "+", lambda: self.zoom(1.5), width=2).pack()
        self.button(zoom_controls, "−", lambda: self.zoom(1 / 1.5), width=2).pack(pady=(1, 0))
        self.detail_label = tk.Label(self.body, text=text("empty"), anchor="w", justify="left", bg=PANEL,
                                     fg="#d7e2ed", font=("Microsoft YaHei UI", 9), padx=p(13), pady=p(8), wraplength=p(370), height=3)
        self.hint_label = tk.Label(self.body, text=text("hint"), bg=BACKGROUND, fg=MUTED,
                                   font=("Microsoft YaHei UI", 8), padx=p(8), pady=p(6))
        # Reserve the footer first; a requested canvas height must not clip the target details.
        self.hint_label.pack(side="bottom", fill="x")
        self.detail_label.pack(side="bottom", fill="x")
        self.legend = tk.Frame(self.body, bg=BACKGROUND, padx=p(9), pady=p(6))
        self.legend.pack(side="bottom", fill="x")
        self.legend_labels = {}
        for kind in ("seen", "soon", "opened", "candidate"):
            label = tk.Label(self.legend, image=self.marker_photo(kind, 16), compound="left", text=text("legend_" + kind),
                             bg=BACKGROUND, fg="#c0ccda", font=("Microsoft YaHei UI", 8), padx=p(4))
            label.pack(side="left", expand=True)
            self.legend_labels[kind] = label
        self.canvas.pack(fill="both", expand=True)
        if not self.collapsed:
            self.body.pack(fill="both", expand=True)
        self.apply_layer()
        self.root.update_idletasks()
        if self.settings.get("follow_centering_version", 0) < FOLLOW_CENTERING_VERSION:
            self.save_settings()
        self.window_flags = own_window_style(self.root, show=self.visibility.visible)
        if self.visibility.visible:
            self.root.deiconify()
        self.root.after_idle(self.style_window)
        self.worker = threading.Thread(target=self.poll, daemon=True, name="overlay-local-map-reader")
        self.worker.start()
        self.root.after(UI_TICK_MS, self.tick)

    def button(self, parent, label, command, width=None):
        return self.tk.Button(parent, text=label, command=command, width=width, takefocus=False,
                              bg="#253446", fg="#edf2f7", activebackground="#34485f", activeforeground="#ffffff",
                              disabledforeground="#63758a", cursor="hand2", relief="flat", borderwidth=0,
                              padx=self.px(9), pady=self.px(5), font=("Segoe UI", 11) if label in ("+", "−", "×") else ("Microsoft YaHei UI", 9))

    def px(self, value):
        return max(1, round(value * getattr(self, "dpi_scale", 1)))

    def marker_photo(self, kind, size, selected=False):
        pixels = max(8, min(96, self.px(size)))
        key = (kind, pixels, selected)
        if key not in self.marker_photos:
            self.marker_photos[key] = self.ImageTk.PhotoImage(marker_image(kind, pixels, selected))
        return self.marker_photos[key]

    def style_window(self):
        self.window_flags = own_window_style(self.root, show=self.visibility.visible)
        self.write_status()

    def set_visible(self, visible):
        if visible:
            self.window_flags = show_without_activation(self.root)
        else:
            self.root.withdraw()

    def hide_until_alert(self):
        if self.hide_pending or not self.state.connected:
            return
        snapshot = self.state.snapshot
        token = snapshot.get("control_token")
        if not isinstance(token, str) or not token:
            self.visibility_error = True
            return
        self.hide_pending = True
        self.visibility_error = False
        session, process_id = snapshot.get("session_id"), snapshot.get("process_id")
        threading.Thread(target=self._save_alert_only, args=(token, session, process_id),
                         daemon=True, name="overlay-alert-settings").start()

    def _save_alert_only(self, token, session, process_id):
        try:
            opener = build_opener(ProxyHandler({}), NoRedirect())
            base = f"http://127.0.0.1:{self.port}"
            request = Request(base + "/api/control", method="POST",
                              data=json.dumps({"action": "alert_settings", "overlay_on_alert": True}).encode("utf-8"),
                              headers={"Content-Type": "application/json", "Origin": base, "X-Redbag-Key": token})
            with opener.open(request, timeout=2) as response:
                result = json.loads(response.read(65536))
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError("overlay preference was not saved")
            with opener.open(base + "/api/state", timeout=2) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("local snapshot too large")
            data = json.loads(raw)
            if (not isinstance(data, dict) or data.get("session_id") != session
                    or data.get("process_id") != process_id or data.get("overlay_on_alert") is not True):
                raise ValueError("local service or preference changed")
            self.control_events.put((True, data))
        except (OSError, ValueError, TypeError):
            self.control_events.put((False, None))

    def current_map(self):
        found = next((item for item in self.maps if item["id"] == self.state.map_id), None)
        if found:
            return found
        rows = visible_records(self.state.snapshot, self.state.map_id)
        if not rows:
            return {"id": self.state.map_id, "x": -50000, "y": -50000, "w": 100000, "h": 100000}
        low_x, high_x = min(r["x"] for r in rows), max(r["x"] for r in rows)
        low_y, high_y = min(r["y"] for r in rows), max(r["y"] for r in rows)
        span = max(high_x - low_x, high_y - low_y, 60000) * 1.3
        return {"id": self.state.map_id, "x": (low_x + high_x - span) / 2,
                "y": (low_y + high_y - span) / 2, "w": span, "h": span}

    def fit(self, manual=True):
        if manual and self.state.follow:
            self.state.set_follow(False)
        player = self.state.follow_target()
        if self.state.follow and player:
            if player:
                self.center_on_player(player)
            self.paint()
            return
        bounds = self.current_map()
        self.cx = bounds["x"] + bounds["w"] / 2
        self.cy = bounds["y"] + bounds["h"] / 2
        self.fit_scale = min(max(self.width, 1) / bounds["w"], max(self.height, 1) / bounds["h"]) * .97
        self.scale = self.fit_scale
        if manual:
            self.zoom_ratio = 1.0
            self.save_settings()
        self.render_key = None
        self.paint()

    def resize(self, event):
        if event.width <= 1 or event.height <= 1:
            return
        was_fit = abs(self.scale - self.fit_scale) < self.fit_scale * .03
        zoom = self.scale / self.fit_scale if self.fit_scale > 0 else 1
        self.width, self.height = event.width, event.height
        bounds = self.current_map()
        self.fit_scale = min(self.width / bounds["w"], self.height / bounds["h"]) * .97
        player = self.state.follow_target()
        if player:
            self.scale = self.fit_scale * zoom
            self.center_on_player(player)
            self.render_key = None
            self.paint()
        elif was_fit:
            self.fit(manual=False)
        else:
            self.render_key = None
            self.paint()

    def screen(self, x, y):
        return (x - self.cx) * self.scale + self.width / 2, (y - self.cy) * self.scale + self.height / 2

    def world(self, x, y):
        return (x - self.width / 2) / self.scale + self.cx, (y - self.height / 2) / self.scale + self.cy

    def zoom(self, factor, x=None, y=None):
        if self.state.follow:
            player = self.state.follow_target()
            if player:
                self.center_on_player(player)
            x, y = self.width / 2, self.height / 2
        x, y = (self.width / 2 if x is None else x), (self.height / 2 if y is None else y)
        before = self.world(x, y)
        self.scale = max(self.fit_scale * .5, min(self.fit_scale * 40, self.scale * factor))
        after = self.world(x, y)
        self.cx += before[0] - after[0]
        self.cy += before[1] - after[1]
        self.zoom_ratio = self.scale / self.fit_scale
        self.save_settings()
        self.render_key = None
        self.paint()

    def begin_drag(self, event):
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def drag_window(self, event):
        if self.drag_origin:
            sx, sy, x, y = self.drag_origin
            self.root.geometry(f"+{max(0, x + event.x_root - sx)}+{max(0, y + event.y_root - sy)}")

    def begin_pan(self, event):
        self.map_drag = (event.x, event.y, self.cx, self.cy)
        self.map_dragged = False

    def pan(self, event):
        if self.map_drag:
            x, y, cx, cy = self.map_drag
            if not self.map_dragged:
                if math.hypot(event.x - x, event.y - y) < 5:
                    return
                self.map_dragged = True
                self.state.set_follow(False)
                self.save_settings()
            self.cx, self.cy = cx - (event.x - x) / self.scale, cy - (event.y - y) / self.scale
            self.render_key = None
            self.paint()

    def end_pan(self, event):
        if self.map_drag and not self.map_dragged and math.hypot(event.x - self.map_drag[0], event.y - self.map_drag[1]) < 5:
            nearby = [(math.hypot(event.x - x, event.y - y), row) for x, y, row in self.click_points]
            if nearby:
                distance, row = min(nearby, key=lambda pair: (pair[0], pair[1].get("kind") == "historical_candidate"))
                if distance < self.px(16):
                    self.state.clear_target()
                    self.state.selected = row["id"]
                    self.paint()
        self.map_drag = None
        self.map_dragged = False

    def clear_target(self):
        self.state.clear_target()
        self.paint()

    def toggle_candidates(self):
        self.show_candidates = not self.show_candidates
        if not self.show_candidates and any(row["id"] == self.state.selected for group in self.candidates.values() for row in group):
            self.state.clear_target()
        self.save_settings()
        self.paint()

    def selected_target(self):
        found = next((row for row in self.state.snapshot.get("records", [])
                      if isinstance(row, dict) and row.get("id") == self.state.selected), None)
        if found:
            return found
        if self.show_candidates:
            return next((row for group in self.candidates.values() for row in group if row["id"] == self.state.selected), None)
        return None

    def paint_candidates(self):
        key = (self.state.map_id, self.width, self.height, self.cx, self.cy, self.scale, self.show_candidates)
        if self.candidate_render_key != key:
            self.canvas.delete("candidate")
            self.candidate_click_points = []
            if self.show_candidates:
                for row in self.candidates.get(self.state.map_id, []):
                    x, y = self.screen(row["x"], row["y"])
                    if not -10 < x < self.width + 10 or not -10 < y < self.height + 10:
                        continue
                    size = 10 if self.scale < self.fit_scale * 4 else 13
                    self.canvas.create_image(round(x), round(y), image=self.marker_photo("candidate", size), tags="candidate")
                    self.candidate_click_points.append((x, y, row))
            self.candidate_render_key = key
        self.click_points = list(self.candidate_click_points)

    def locate(self, row, manual=True):
        self.state.selected = row["id"]
        player = self.state.follow_target()
        if player:
            self.center_on_player(player)
        if self.state.follow and row["map_id"] != self.state.map_id:
            self.paint()
            return
        previous_scale = self.scale
        self.state.map_id = row["map_id"]
        bounds = self.current_map()
        self.fit_scale = min(self.width / bounds["w"], self.height / bounds["h"]) * .97
        if not player:
            self.cx, self.cy = row["x"], row["y"]
        self.scale = bag_focus_scale(self.width, self.height, self.fit_scale, previous_scale)
        self.zoom_ratio = self.scale / self.fit_scale
        self.save_settings()
        self.render_key = None
        self.paint()

    def locate_latest(self):
        rows = visible_records(self.state.snapshot, self.state.map_id)
        if rows:
            selected = next((row for row in rows if row["id"] == self.state.selected), None)
            self.locate(selected or max(rows, key=lambda row: latest_time(row) or 0))

    def toggle_follow(self):
        self.state.set_follow(not self.state.follow)
        self.save_settings()
        player = self.state.follow_target()
        if player:
            self.center_on_player(player)
        elif self.state.follow:
            current_map = self.state.snapshot.get("current_map_id")
            if type(current_map) is int and current_map != self.state.map_id:
                self.state.map_id = current_map
                self.fit(manual=False)
        self.paint()

    def center_on_player(self, player):
        ratio = self.zoom_ratio if self.zoom_ratio is not None else DEFAULT_FOLLOW_ZOOM
        self.zoom_ratio = ratio
        self.state.map_id = player["map_id"]
        bounds = self.current_map()
        self.fit_scale = min(max(self.width, 1) / bounds["w"], max(self.height, 1) / bounds["h"]) * .97
        self.scale = self.fit_scale * ratio
        if self.cx != player["x"] or self.cy != player["y"]:
            self.cx, self.cy = player["x"], player["y"]
            self.render_key = None

    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.body.pack_forget()
            self.root.geometry(f"{self.px(OVERLAY_WIDTH)}x{self.px(HEADER_HEIGHT)}")
        else:
            self.body.pack(fill="both", expand=True)
            self.root.geometry(f"{self.px(OVERLAY_WIDTH)}x{self.px(EXPANDED_HEIGHT)}")
        self.fold_button.configure(text="+" if self.collapsed else "−")
        self.save_settings()
        self.root.after_idle(self.style_window)

    def change_alpha(self):
        levels = [.55, .75, .94, 1.0]
        self.alpha = next((value for value in levels if value > self.alpha + .01), levels[0])
        self.root.attributes("-alpha", self.alpha)
        self.alpha_button.configure(text=f"{self.alpha:.0%}")
        self.save_settings()
        self.root.after_idle(self.style_window)

    def apply_layer(self):
        if os.name == "nt":
            self.root.attributes("-transparentcolor", TRANSPARENT if self.layer else "")
        self.canvas.configure(bg=TRANSPARENT if self.layer else "#d4c8b2")
        self.layer_button.configure(text=text("background" if self.layer else "layer", self.language))
        self.hint_label.configure(text=text("layer_hint" if self.layer else "hint", self.language))
        self.render_key = None

    def toggle_layer(self):
        self.layer = not self.layer
        self.apply_layer()
        self.save_settings()
        self.paint()
        self.root.after_idle(self.style_window)

    def tile_image(self, col, row):
        key = (self.state.map_id, col, row)
        if key in self.tile_photos:
            self.tile_photos.move_to_end(key)
            tile = self.tile_photos[key]
            if tile is not None:
                self.raster_mode = "tiles"
            return tile
        relative = Path(str(self.state.map_id)) / f"{col}_{row}.webp"
        path = getattr(self, "map_assets", ROOT / "static/maps") / relative
        if not path.is_file():
            path = ROOT / "static/maps" / relative
        try:
            with self.Image.open(path) as image:
                tile = image.convert("RGB")
        except OSError:
            tile = None
        self.tile_photos[key] = tile
        if len(self.tile_photos) > TILE_CACHE_LIMIT:
            self.tile_photos.popitem(last=False)
        if tile is not None:
            self.raster_mode = "tiles"
        return tile

    def poll(self):
        opener = build_opener(ProxyHandler({}), NoRedirect())
        while not self.stopping.is_set():
            try:
                with opener.open(f"http://127.0.0.1:{self.port}/api/state", timeout=2) as response:
                    raw = response.read(8 * 1024 * 1024 + 1)
                    if len(raw) > 8 * 1024 * 1024:
                        raise ValueError("local snapshot too large")
                    data = json.loads(raw)
                item = (True, data)
            except (OSError, ValueError):
                item = (False, None)
            queue_latest_event(self.events, item)
            self.stopping.wait(POLL_INTERVAL if item[0] else 1)

    def tick(self):
        try:
            if self.stop_path.exists() and self.stop_path.stat().st_mtime >= self.started_at:
                self.close()
                return
        except OSError:
            pass
        try:
            while True:
                connected, data = self.events.get_nowait()
                if connected:
                    try:
                        self.accept_snapshot(data)
                    except (ValueError, TypeError, KeyError):
                        self.state.disconnect()
                        continue
                else:
                    self.state.disconnect()
        except queue.Empty:
            pass
        try:
            while True:
                success, data = self.control_events.get_nowait()
                self.hide_pending = False
                if success:
                    self.visibility.arm()
                    self.set_visible(False)
                    # The fresh response time prevents an older in-flight poll
                    # from undoing a successfully saved preference.
                    self.accept_snapshot(data)
                else:
                    self.visibility_error = True
        except queue.Empty:
            pass
        if consume_show_command(self.show_path, pid=os.getpid(),
                                api=f"http://127.0.0.1:{self.port}/api/state",
                                started_at=self.started_at, now=time.time()):
            self.visibility.manual_show()
            self.set_visible(True)
        self.paint()
        self.write_status()
        if not self.stopping.is_set():
            self.root.after(UI_TICK_MS, self.tick)

    def accept_snapshot(self, data):
        result = self.state.accept(data)
        self.language = restored_language(data.get("language"))
        if self.visibility_error and data.get("overlay_on_alert") is True:
            self.visibility_error = False
        action = self.visibility.accept(data, result["alert"])
        self.apply_update(result)
        if action is not None:
            self.set_visible(action == "show")

    def apply_update(self, result):
        local_alert = result["alert"] and (not self.state.follow or result["alert"]["map_id"] == self.state.map_id)
        if result["player"]:
            self.center_on_player(result["player"])
        elif result["fit"] and not local_alert:
            self.fit(manual=False)
        if result["alert"]:
            if self.collapsed:
                self.toggle_collapse()
            self.locate(result["alert"], manual=False)

    def paint(self):
        if self.stopping.is_set():
            return
        language, now = self.language, time.time()
        player = self.state.player(now)
        rows = visible_records(self.state.snapshot, self.state.map_id)
        name = map_name(self.maps, self.state.map_id, language)
        alert_active = now < self.state.alert_until
        title = f"● {name}" if alert_active else name
        self.title_label.configure(text=title, fg="#ffb0a7" if alert_active else "#edf2f7")
        self.fit_button.configure(text=text("fit", language), state="normal")
        self.latest_button.configure(text=text("latest", language))
        self.hide_button.configure(text=text("hide_saving" if self.hide_pending else "hide_until_alert", language),
                                   state="normal" if self.state.connected and not self.hide_pending else "disabled")
        self.candidate_button.configure(text=text("candidates" if self.show_candidates else "candidates_hidden", language,
                                                  count=f"{len(self.candidates.get(self.state.map_id, [])):,}"),
                                        bg="#234153" if self.show_candidates else "#253446")
        self.clear_button.configure(text=text("clear_target", language), state="normal" if self.state.selected else "disabled")
        self.layer_button.configure(text=text("background" if self.layer else "layer", language))
        self.hint_label.configure(text=text("locked_hint" if self.state.follow else "layer_hint" if self.layer else "hint", language))
        for kind, label in self.legend_labels.items():
            label.configure(text=text("legend_" + kind, language))
        if not self.state.snapshot:
            connection = text("waiting", language)
        elif not self.state.connected:
            connection = text("offline", language)
        elif self.state.snapshot.get("mode") != "live":
            connection = text("history", language)
        elif self.state.snapshot.get("paused") or self.state.snapshot.get("capture", {}).get("phase") != "observing":
            connection = text("paused", language)
        else:
            connection = text("live", language)
        if self.visibility_error:
            connection = text("hide_failed", language)
        receiving = self.state.connected and self.state.snapshot.get("mode") == "live" and not self.state.snapshot.get("paused") and self.state.snapshot.get("capture", {}).get("phase") == "observing"
        self.status_label.configure(text=f"●  {connection}   ·   {text('count', language, count=len(rows))}",
                                    fg="#68d6ae" if receiving else MUTED)
        position_phase = self.state.snapshot.get("position_diagnostics", {}).get("state")
        if position_phase == "awaiting_map":
            self.player_label.configure(text=text("player_map_waiting", language), fg="#e6ba79")
        elif position_phase == "awaiting_stream":
            self.player_label.configure(text=text("player_syncing", language), fg="#e6ba79")
        elif player:
            key = "player_age" if player["fresh"] else "player_expired"
            self.player_label.configure(text=text(key, language, seconds=player["age_seconds"]),
                                        fg=PLAYER_COLOR if player["fresh"] else "#899bb3")
        else:
            self.player_label.configure(text=text("player_waiting", language), fg="#899bb3")
        follow_key = "following" if self.state.follow else "follow"
        self.follow_button.configure(text=text(follow_key, language),
                                     bg="#234153" if self.state.follow else "#253446")
        selected = self.selected_target()
        if selected:
            status = display_status(selected, now)
            candidate = status == "candidate"
            heading = text("candidate_number", language, count=selected["ordinal"]) if candidate else text("new", language) if alert_active else text(status, language)
            if selected["map_id"] != self.state.map_id:
                heading = f"{heading} · {map_name(self.maps, selected['map_id'], language)}"
            distance = approximate_distance(player, selected)
            distance_label = "" if distance is None else f" · {text('distance_short', language, meters=round(distance))}"
            timing = text("candidate_info", language) if candidate else f"{text('updated', language)} {local_clock(latest_time(selected))}"
            self.detail_label.configure(text=f"{heading}\nX {selected['x']:.0f} / Y {selected['y']:.0f}\n{timing}{distance_label}",
                                       fg=COLORS[status])
        else:
            hint = "browse_candidates" if self.show_candidates and self.candidates.get(self.state.map_id) else "empty" if not rows else "locked_hint" if self.state.follow else "hint"
            self.detail_label.configure(text=text(hint, language), fg="#d7e2ed")
        if self.collapsed:
            return
        bounds = self.current_map()
        key = (self.state.map_id, self.width, self.height, self.cx, self.cy, self.scale, self.layer, language)
        if self.render_key != key:
            self.canvas.delete("base")
            self.raster_mode = "transparent" if self.layer else "unavailable"
            path = self.map_assets / f"{self.state.map_id}.webp"
            if not path.is_file():
                path = ROOT / "static/maps" / f"{self.state.map_id}.webp"
            if not self.layer and path.is_file():
                if self.state.map_id not in self.photos:
                    with self.Image.open(path) as image:
                        self.photos[self.state.map_id] = image.convert("RGB")
                image = self.photos[self.state.map_id]
                self.raster_mode = "overview"
                frame = render_map_frame(image, bounds, self.cx, self.cy, self.scale,
                                         max(1, self.width), max(1, self.height), self.tile_image)
                self.photo = self.ImageTk.PhotoImage(frame)
                self.canvas.create_image(0, 0, image=self.photo, anchor="nw", tags="base")
            elif not self.layer:
                self.raster_mode = "coordinate_grid"
                step = 10 ** math.ceil(math.log10(70 / self.scale))
                left, top = self.world(0, 0)
                right, bottom = self.world(self.width, self.height)
                for x in range(math.floor(left / step), math.ceil(right / step) + 1):
                    sx, _ = self.screen(x * step, 0)
                    self.canvas.create_line(sx, 0, sx, self.height, fill="#b5aa96", tags="base")
                for y in range(math.floor(top / step), math.ceil(bottom / step) + 1):
                    _, sy = self.screen(0, y * step)
                    self.canvas.create_line(0, sy, self.width, sy, fill="#b5aa96", tags="base")
                self.canvas.create_text(self.width / 2, 16, text=text("no_map", language), fill="#665d4d", tags="base")
            self.canvas.tag_lower("base")
            self.render_key = key
        self.canvas.delete("marker")
        self.paint_candidates()
        target = selected if selected and selected["map_id"] == self.state.map_id else None
        indicator = target_indicator(*self.screen(target["x"], target["y"]), self.width, self.height, self.px(34)) if target else None
        if indicator and player and player["map_id"] == self.state.map_id:
            px, py = self.screen(player["x"], player["y"])
            if math.hypot(indicator["x"] - px, indicator["y"] - py) > 28:
                self.canvas.create_line(px, py, indicator["x"], indicator["y"], fill=COLORS["candidate"] if target.get("kind") == "historical_candidate" else "#ffcab4",
                                        width=self.px(2), dash=(self.px(5), self.px(5)), tags="marker")
        for row in rows:
            if row["id"] == self.state.selected and indicator and indicator["outside"]:
                continue
            x, y = self.screen(row["x"], row["y"])
            if not -20 < x < self.width + 20 or not -20 < y < self.height + 20:
                continue
            self.click_points.append((x, y, row))
            status = display_status(row, now)
            if row["id"] != self.state.selected:
                size = 18 if status in ("opened", "unknown") else 23
                self.canvas.create_image(round(x), round(y), image=self.marker_photo(status, size), tags="marker")
        if target:
            self.paint_target(target, indicator, player, alert_active, now)
        if player and player["map_id"] == self.state.map_id:
            x, y = self.screen(player["x"], player["y"])
            if -20 < x < self.width + 20 and -20 < y < self.height + 20:
                color = PLAYER_COLOR if player["fresh"] else "#899bb3"
                if player["fresh"]:
                    self.canvas.create_image(round(x), round(y), image=self.marker_photo("player", 27), tags="marker")
                else:
                    radius = self.px(10)
                    self.canvas.create_oval(x-radius, y-radius, x+radius, y+radius, outline=color,
                                            width=self.px(2), dash=(self.px(3), self.px(3)), tags="marker")
                label = self.canvas.create_text(x, y - self.px(21), text=text("player_short" if player["fresh"] else "expired_short", language),
                                               fill=PLAYER_COLOR if player["fresh"] else "#c9d3df",
                                               font=("Microsoft YaHei UI", 9, "bold"), tags="marker")
                box = self.canvas.bbox(label)
                if box:
                    backing = self.canvas.create_rectangle(box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2,
                                                           fill=BACKGROUND, outline="", tags="marker")
                    self.canvas.tag_lower(backing, label)
        self.paint_scale()

    def paint_scale(self):
        # World coordinates use centimetres. Select a readable 1/2/5 distance.
        maximum_meters = self.px(80) / self.scale / 100
        magnitude = 10 ** math.floor(math.log10(maximum_meters))
        meters = max(value * magnitude for value in (1, 2, 5) if value * magnitude <= maximum_meters)
        length = meters * 100 * self.scale
        left, bottom = self.px(12), self.height - self.px(14)
        self.canvas.create_rectangle(left-self.px(5), bottom-self.px(24), left+length+self.px(6), bottom+self.px(5),
                                     fill=BACKGROUND, outline="", tags="marker")
        self.canvas.create_line(left, bottom-self.px(4), left, bottom, left+length, bottom, left+length, bottom-self.px(4),
                                fill="#edf2f7", width=self.px(1), tags="marker")
        value = f"{meters/1000:g} km" if meters >= 1000 else f"{meters:g} m"
        self.canvas.create_text(left, bottom-self.px(9), text=value, anchor="sw", fill="#edf2f7", font=("Segoe UI", 8), tags="marker")

    def paint_target(self, target, indicator, player, alert_active, now):
        x, y = indicator["x"], indicator["y"]
        status = display_status(target, now)
        color = COLORS[status]
        if indicator["outside"]:
            ux, uy = indicator["ux"], indicator["uy"]
            tip, back, wing, inset = (self.px(value) for value in (15, 11, 9, 5))
            points = (x + ux * tip, y + uy * tip,
                      x - ux * back - uy * wing, y - uy * back + ux * wing,
                      x - ux * inset, y - uy * inset,
                      x - ux * back + uy * wing, y - uy * back - ux * wing)
            self.canvas.create_polygon(*points, fill=color, outline="#fff4df", width=self.px(2), tags="marker")
            self.click_points.append((x, y, target))
            label_key = "candidate_outside" if status == "candidate" else "bag_outside"
        else:
            self.canvas.create_image(round(x), round(y), image=self.marker_photo(status, 32, True), tags="marker")
            label_key = "candidate_target" if status == "candidate" else "bag_target"
        label = text(label_key, self.language)
        if target.get("status") == "opened":
            label += " · " + text("opened", self.language)
        distance = approximate_distance(player, target)
        if distance is not None:
            label += "\n" + text("distance_short", self.language, meters=round(distance))
        # Keep labels inside the panel even at its top, side or bottom edges.
        above = y >= self.height / 2 if indicator["outside"] else y < self.height / 2
        ty = y - self.px(23) if above else y + self.px(23)
        anchor = "s" if above else "n"
        item = self.canvas.create_text(x, ty, text=label, fill="#fff4df", anchor=anchor,
                                       justify="center", font=("Microsoft YaHei UI", 9, "bold"), tags="marker")
        box = self.canvas.bbox(item)
        if box:
            dx = max(8 - box[0], 0) - max(box[2] - (self.width - 8), 0)
            dy = max(6 - box[1], 0) - max(box[3] - (self.height - 6), 0)
            self.canvas.move(item, dx, dy)
            box = self.canvas.bbox(item)
            backing = self.canvas.create_rectangle(box[0] - 5, box[1] - 3, box[2] + 5, box[3] + 3,
                                                   fill=BACKGROUND, outline=color, width=1, tags="marker")
            self.canvas.tag_lower(backing, item)

    def save_settings(self):
        density = getattr(self, "dpi_scale", 1)
        data = {"x": round(self.root.winfo_x() / density), "y": round(self.root.winfo_y() / density), "alpha": self.alpha,
                "collapsed": self.collapsed, "layer": self.layer, "follow": self.state.follow,
                "show_candidates": self.show_candidates,
                "follow_centering_version": FOLLOW_CENTERING_VERSION}
        if self.zoom_ratio is not None:
            data["zoom_ratio"] = self.zoom_ratio
        try:
            atomic_write(self.settings_path, json.dumps(data).encode("utf-8"))
        except OSError:
            pass

    def write_status(self):
        now = time.time()
        player = self.state.player(now)
        fresh = bool(player and player["fresh"])
        target = self.selected_target()
        target = target if target and target["map_id"] == self.state.map_id else None
        indicator = target_indicator(*self.screen(target["x"], target["y"]), self.width, self.height, self.px(34)) if target else None
        data = {"pid": os.getpid(), "running": not self.stopping.is_set(), "connected": self.state.connected,
                "visible": getattr(getattr(self, "visibility", None), "visible", True),
                "overlay_on_alert": getattr(getattr(self, "visibility", None), "enabled", False),
                "waiting_for_alert": bool(getattr(getattr(self, "visibility", None), "enabled", False)
                                          and not getattr(getattr(self, "visibility", None), "visible", True)),
                "overlay_version": OVERLAY_VERSION, "poll_interval_ms": round(POLL_INTERVAL * 1000),
                "ui_tick_ms": UI_TICK_MS, "base_map_mode": self.raster_mode,
                "dpi_scale": getattr(self, "dpi_scale", 1), "marker_style": "outlined-icons",
                "tile_cache_count": len(self.tile_photos),
                "api": f"http://127.0.0.1:{self.port}/api/state", "map_id": self.state.map_id,
                "record_count": len(visible_records(self.state.snapshot, self.state.map_id)),
                "candidate_count": len(self.candidates.get(self.state.map_id, [])),
                "candidates_visible": self.show_candidates, "selected_target": self.state.selected,
                "selected_record": self.state.selected if not (target and target.get("kind") == "historical_candidate") else None,
                "selected_candidate": self.state.selected if target and target.get("kind") == "historical_candidate" else None,
                "collapsed": self.collapsed, "transparent_layer": self.layer,
                "target_offscreen": indicator["outside"] if indicator else None,
                "zoom_ratio": self.scale / self.fit_scale,
                "following": self.state.follow, "follow_enabled": self.state.follow,
                "center_locked": self.state.follow_target(now) is not None,
                "follow_centering_version": FOLLOW_CENTERING_VERSION,
                "following_fresh_position": self.state.follow and fresh,
                "player_available": player is not None, "player_fresh": fresh,
                "player_observed_at": player["observed_at"] if player else None,
                "player_map_id": player["map_id"] if player else None,
                "player_reference": {key: player[key] for key in ("x", "y", "z", "age_seconds")} if player else None,
                "cx": self.cx, "cy": self.cy, "view_scale": self.scale,
                "view_width": self.width, "view_height": self.height,
                "alpha": self.alpha, "language": self.language, "updated_at": now,
                "geometry": self.root.geometry(), **self.window_flags}
        try:
            atomic_write(self.status_path, json.dumps(data, ensure_ascii=False).encode("utf-8"))
        except OSError:
            pass

    def close(self):
        self.save_settings()
        self.stopping.set()
        self.write_status()
        self.root.destroy()

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.stopping.set()


def main():
    parser = argparse.ArgumentParser(description="Independent AION2 desktop mini map")
    parser.add_argument("--port", type=int, default=18726)
    parser.add_argument("--data-dir")
    parser.add_argument("--settings-path")
    parser.add_argument("--status-path")
    parser.add_argument("--hidden-until-alert", action="store_true")
    parser.add_argument("--alert-baseline-session")
    parser.add_argument("--alert-baseline-seq", type=int)
    parser.add_argument("--stop", action="store_true", help="Request this tool's overlay to close normally")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("invalid local port")
    if ((args.alert_baseline_session is None) != (args.alert_baseline_seq is None)
            or args.alert_baseline_seq is not None and args.alert_baseline_seq < 0
            or args.alert_baseline_session == ""):
        parser.error("invalid alert baseline")
    data = Path(args.data_dir).resolve() if args.data_dir else Path.home() / "Documents" / "AION2-RedBag-Map" / "data"
    settings = Path(args.settings_path) if args.settings_path else data / "overlay-settings.json"
    status = Path(args.status_path) if args.status_path else data / "overlay-status.json"
    stop = data / "overlay.stop"
    if args.stop:
        atomic_write(stop, json.dumps({"requested_at": time.time()}).encode("utf-8"))
        return
    try:
        with OverlayLock(data / "overlay.lock"):
            if stop.exists():
                stop.unlink()
            DesktopOverlay(args.port, settings, status, stop, hidden_until_alert=args.hidden_until_alert,
                           alert_baseline_session=args.alert_baseline_session,
                           alert_baseline_seq=args.alert_baseline_seq).run()
    except OSError as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
