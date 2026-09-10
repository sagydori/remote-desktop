"""
remote_desktop.py  —  The one app, on both PCs.

Two clear buttons:
  * "Control <other PC>"  — view + control the other computer.
  * "Allow this PC to be controlled" — turn on so the other PC can control this one.

Same app on both PCs, so either can control the other. Settings (this PC's
name, the other PC's name, the shared password) come from config.py, written
by the Setup Wizard. Connections travel over Tailscale.
"""

import asyncio
import concurrent.futures
import json
import os
import queue
import sys
import threading
import time
from collections import deque

import cv2
import mss
import numpy as np
import customtkinter as ctk
from PIL import Image, ImageTk
from pynput.keyboard import Controller as KeyboardController, Key, KeyCode
from pynput.mouse import Button, Controller as MouseController
import websockets

# When packaged as an .exe (PyInstaller), the app's own files — config.py,
# settings.json, icon.ico, VERSION — live next to the executable, not inside
# the temporary unpack dir. Resolve paths against the exe in that case.
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "icon.ico")

if HERE not in sys.path:
    sys.path.insert(0, HERE)                # so `import config` finds config.py beside the exe
try:
    import config
except Exception:
    config = type("cfg", (), {})()          # no config.py yet (before Setup Wizard runs)
NAME = getattr(config, "NAME", "") or "this PC"
PEER = getattr(config, "PEER", "") or getattr(config, "HOST", "")
PORT = getattr(config, "PORT", 8765)
SECRET = getattr(config, "SECRET", "")

try:
    with open(os.path.join(HERE, "VERSION"), encoding="utf-8") as _vf:
        APP_VERSION = _vf.read().strip() or "?"
except Exception:
    APP_VERSION = "?"

# ---- Windows raw relative mouse motion (for in-game camera / mouse-look) ----
# Games (Minecraft, FPS titles) grab the pointer and read RAW motion deltas.
# Setting an absolute cursor position doesn't turn the camera; injecting a
# relative MOUSEEVENTF_MOVE via SendInput does, for both "Raw Input" on and off.
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _MOUSEEVENTF_MOVE = 0x0001

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    def _move_relative(dx, dy):
        extra = ctypes.c_ulong(0)
        mi = _MOUSEINPUT(int(dx), int(dy), 0, _MOUSEEVENTF_MOVE, 0, ctypes.pointer(extra))
        inp = _INPUT(0, _INPUTUNION(mi))
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class _CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hCursor", ctypes.c_void_p), ("ptScreenPos", _POINT)]

    _CURSOR_SHOWING = 0x00000001

    def cursor_screen_pos():
        """Screen (x, y) of the mouse cursor if it is currently VISIBLE, else None.
        Games hide the cursor during play (returns None) and show it in menus/
        inventories (returns the position) — exactly when the controller needs it."""
        x, y, _hc = cursor_state() if True else (None, None, None)
        return (x, y) if x is not None else None

    def cursor_state():
        """(screen_x, screen_y, hCursor) if the cursor is visible, else (None, None, None).
        hCursor identifies the shape (arrow / I-beam / hand / …) so we can render the REAL
        cursor rather than a generic drawn arrow."""
        ci = _CURSORINFO()
        ci.cbSize = ctypes.sizeof(_CURSORINFO)
        try:
            if ctypes.windll.user32.GetCursorInfo(ctypes.byref(ci)) and (ci.flags & _CURSOR_SHOWING):
                return ci.ptScreenPos.x, ci.ptScreenPos.y, ci.hCursor
        except Exception:
            pass
        return None, None, None

    # ---- render the ACTUAL system cursor bitmap (any shape) --------------------
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32
    # Declare handle-returning calls as pointer-wide so 64-bit handles aren't truncated.
    _gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    _gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    _gdi32.CreateDIBSection.restype = ctypes.c_void_p
    _gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                        wintypes.DWORD]
    _gdi32.SelectObject.restype = ctypes.c_void_p
    _gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    _gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    _user32.GetDC.restype = ctypes.c_void_p
    _user32.GetDC.argtypes = [ctypes.c_void_p]
    _user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.DrawIconEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    _DI_NORMAL = 0x0003
    _CURSOR_PX = 32                       # standard cursor size we render at (native pixels)
    _cursor_cache = {}                    # hCursor -> (color_bgr float32, transparency float32) or None

    def _draw_cursor_on(hcursor, bg):
        """Draw the cursor onto a solid `bg` (0 or 255) 32x32 DIB; return (32,32,3) BGR float32."""
        import numpy as _np
        w = h = _CURSOR_PX
        hdc_screen = _user32.GetDC(0)
        hdc = _gdi32.CreateCompatibleDC(hdc_screen)
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(bmi); bmi.biWidth = w; bmi.biHeight = -h
        bmi.biPlanes = 1; bmi.biBitCount = 32; bmi.biCompression = 0
        ppv = ctypes.c_void_p()
        hbm = _gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0, ctypes.byref(ppv), None, 0)
        old = _gdi32.SelectObject(hdc, hbm)
        try:
            ctypes.memset(ppv, bg, w * h * 4)
            _user32.DrawIconEx(hdc, 0, 0, hcursor, w, h, 0, None, _DI_NORMAL)
            _gdi32.GdiFlush()
            raw = (ctypes.c_ubyte * (w * h * 4)).from_address(ppv.value)
            arr = _np.frombuffer(bytes(raw), _np.uint8).reshape(h, w, 4)[:, :, :3].astype(_np.float32)
        finally:
            _gdi32.SelectObject(hdc, old); _gdi32.DeleteObject(hbm)
            _gdi32.DeleteDC(hdc); _user32.ReleaseDC(0, hdc_screen)
        return arr

    def cursor_layers(hcursor):
        """(color_bgr, transparency) 32x32 float arrays for an hCursor (cached), or None.
        Drawing the cursor on black and on white recovers true color + alpha:
        transparency=(white-black)/255 (0=opaque,1=clear); color=black (premultiplied)."""
        if not hcursor:
            return None
        key = int(hcursor)
        if key in _cursor_cache:
            return _cursor_cache[key]
        result = None
        try:
            black = _draw_cursor_on(hcursor, 0)
            white = _draw_cursor_on(hcursor, 255)
            transp = np.clip((white - black) / 255.0, 0.0, 1.0)
            result = (black, transp)
        except Exception:
            result = None
        if len(_cursor_cache) > 32:
            _cursor_cache.clear()
        _cursor_cache[key] = result
        return result
else:
    def _move_relative(dx, dy):
        pass  # patched to pynput's relative move per-controller on non-Windows

    def cursor_screen_pos():
        return None

    def cursor_state():
        return None, None, None

    def cursor_layers(hcursor):
        return None

# capture / quality
MIN_QUALITY = 60                      # quality floor when shedding load
SCALE = 1.0                           # capture at native resolution (ceiling)
SMOOTH_MIN_SCALE = 0.5                # "Smoothest": may drop to half-res to hold the frame rate
SHARP_MIN_SCALE = 1.0                 # "Sharpest": never downscale (fps may suffer on big screens)
BIG_FRAME_PIXELS = 1_300_000          # above ~900p (incl. 1080p) use fast 4:2:0 chroma; 4:4:4 only for small frames
SMOOTH_TARGET_W = 1920                 # "Smoothest" starts capped near this width on big screens
DIFF_THRESHOLD = 1.2
KEYFRAME_EVERY = 2.0
USE_BETTERCAM = True
DEFAULT_QUALITY = 90                  # high quality; edges/text stay crisp
DEFAULT_FPS = 60                      # smooth 60 fps
# Client-side draw cap. Blitting a full 1080p frame to the Tk canvas costs ~50 ms
# (~20 fps) no matter how fast the network and encoder are — the cost scales with the
# number of pixels drawn. In "smooth" priority we cap the *drawn* width so the frame
# rate stays high (~45 fps at 1440); the image is centered and a small side border may
# show on a bigger window. "sharp" priority draws at full canvas size for max crispness.
RENDER_MAX_W_SMOOTH = 1280

# palette  —  cyan-on-deep-navy "command console" (dark base + one neon accent)
BG          = "#0A0E14"   # deepest window background
BG_LAYER    = "#0F1520"   # behind cards
CARD        = "#151C28"   # card / toolbar surface
SURF_HOVER  = "#1B2433"   # elevated / hover surface
SURF_INSET  = "#0C121B"   # inputs, HUD bar, icon discs
BORDER      = "#232D3D"   # default 1px border
DIVIDER     = "#1A2230"   # hairline separators
ACCENT      = "#22D3EE"   # primary accent (cyan)
ACCENT_HOVER= "#4FE0F5"   # accent hover
ACCENT_LO   = "#0FB4D0"   # accent pressed
ACCENT_DIM  = "#155E75"   # glow ring / faint accent
ON_ACCENT   = "#08141B"   # dark text to sit on a solid-accent button
GREEN       = "#34D399"   # ON / connected
GREEN_HALO  = "#10704F"   # pulsing halo behind the ON dot
RED         = "#F87171"   # danger / disconnect (used as ghost text)
RED_DIM     = "#7F2A2A"   # danger hover fill
WARNING     = "#FBBF24"   # connecting / degraded
TEXT        = "#E6EDF6"   # headings / primary text
TEXT_BODY   = "#9FB0C3"   # body text
MUTED       = "#64748B"   # captions / eyebrows / disabled
GRAD = ("#22D3EE", "#38BDF8", "#3B82F6")   # cyan -> sky -> blue accent gradient
# deep-space starfield palette (home-screen background)
SPACE_BG      = "#05060C"   # the void at the bottom of the sky
SPACE_TOP     = "#0C1230"   # indigo glow at the top of the sky
SPACE_NEBULA1 = (86, 52, 158)    # violet nebula cloud
SPACE_NEBULA2 = (24, 74, 110)    # teal nebula cloud
SPACE_STAR    = "#DDE8FF"   # star tint (pale blue-white)
FONT_UI = "Segoe UI"      # native on Windows 11
FONT_MONO = "Consolas"    # native monospace for the stat readout

_SPECIAL = {
    "Return": "enter", "KP_Enter": "enter", "Escape": "esc", "BackSpace": "backspace",
    "Tab": "tab", "space": "space", "Delete": "delete", "Insert": "insert",
    "Home": "home", "End": "end", "Prior": "page_up", "Next": "page_down",
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "Shift_L": "shift", "Shift_R": "shift_r", "Control_L": "ctrl", "Control_R": "ctrl_r",
    "Alt_L": "alt", "Alt_R": "alt_gr", "Super_L": "cmd", "Super_R": "cmd_r",
    "Caps_Lock": "caps_lock", **{f"F{i}": f"f{i}" for i in range(1, 13)},
}
_MOUSE = {1: "left", 2: "middle", 3: "right"}


# ---- user-customizable settings (persisted; survives auto-update) ----------
SETTINGS_FILE = os.path.join(HERE, "settings.json")
DEFAULT_SETTINGS = {
    "appearance": "Dark",       # Dark | Light | System
    "theme": "blue",            # blue | green | dark-blue
    "fps": DEFAULT_FPS,         # streaming frame rate when this PC is shared
    "quality": DEFAULT_QUALITY, # streaming JPEG quality when this PC is shared
    "priority": "smooth",       # smooth (hold fps, may drop resolution) | sharp (lock full res)
    "sensitivity": 1.0,         # mouse-look turn speed multiplier
    "ui_scale": 1.0,            # overall GUI size (widget scaling)
    "conn_preset": "balanced",  # last quality preset picked before controlling
    "accent": "#22D3EE",        # customizable accent color
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            s.update({k: data[k] for k in DEFAULT_SETTINGS if k in data})
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


def _gradient_image(width, height, stops=GRAD):
    """A horizontal accent gradient as a CTkImage (used for thin accent underlines)."""
    w = max(int(width), 1)
    cols = [tuple(int(s[i:i + 2], 16) for i in (1, 3, 5)) for s in stops]
    segs = len(cols) - 1
    row = Image.new("RGB", (w, 1))
    for x in range(w):
        fpos = (x / max(w - 1, 1)) * segs
        i = min(int(fpos), segs - 1)
        f = fpos - i
        c0, c1 = cols[i], cols[i + 1]
        row.putpixel((x, 0), tuple(int(c0[j] + (c1[j] - c0[j]) * f) for j in range(3)))
    return ctk.CTkImage(row, size=(w, max(int(height), 1)))


def _blend(a, b, t):
    """Blend two #rrggbb colors; t in [0,1]."""
    ca = tuple(int(a[i:i + 2], 16) for i in (1, 3, 5))
    cb = tuple(int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(int(ca[j] + (cb[j] - ca[j]) * t) for j in range(3))


def _luma(hex_):
    r, g, b = (int(hex_[i:i + 2], 16) for i in (1, 3, 5))
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


# Accent swatches offered in Settings (name -> hex).
ACCENT_SWATCHES = [
    ("Cyan", "#22D3EE"), ("Sky", "#38BDF8"), ("Violet", "#A78BFA"),
    ("Emerald", "#34D399"), ("Amber", "#FBBF24"), ("Rose", "#FB7185"),
    ("Orange", "#FB923C"), ("Pink", "#F472B6"),
]


def apply_theme(accent):
    """Reassign the accent-derived colors from one chosen accent hex."""
    global ACCENT, ACCENT_HOVER, ACCENT_LO, ACCENT_DIM, ON_ACCENT, GRAD
    try:
        int(accent[1:], 16)
        assert accent.startswith("#") and len(accent) == 7
    except Exception:
        accent = "#22D3EE"
    ACCENT = accent
    ACCENT_HOVER = _blend(accent, "#ffffff", 0.20)
    ACCENT_LO = _blend(accent, "#000000", 0.18)
    ACCENT_DIM = _blend(accent, BG, 0.60)
    ON_ACCENT = "#08141B" if _luma(accent) > 0.55 else "#F2F7FF"
    GRAD = (ACCENT_HOVER, accent, _blend(accent, "#000000", 0.28))


# ===========================================================================
#  SHARED: input replay, adaptive encoder, capture
# ===========================================================================
class InputController:
    _BUTTONS = {"left": Button.left, "right": Button.right, "middle": Button.middle}

    def __init__(self, left, top, width, height):
        self.mouse = MouseController()
        self.keyboard = KeyboardController()
        self.left, self.top, self.width, self.height = left, top, width, height
        self._keys, self._btns = set(), set()

    def _abs(self, rx, ry):
        return self.left + int(rx * self.width), self.top + int(ry * self.height)

    def handle(self, e):
        t = e.get("type")
        if t == "move":
            self.mouse.position = self._abs(e["x"], e["y"])
        elif t == "rmove":
            # relative motion for in-game camera / mouse-look (no absolute repositioning)
            dx, dy = e.get("dx", 0), e.get("dy", 0)
            if os.name == "nt":
                _move_relative(dx, dy)
            else:
                self.mouse.move(int(dx), int(dy))
        elif t == "click":
            # In game mode the pointer is locked; don't reposition it (would jerk the camera)
            if not e.get("game"):
                self.mouse.position = self._abs(e["x"], e["y"])
            b = self._BUTTONS.get(e.get("button"), Button.left)
            if e.get("pressed"):
                self.mouse.press(b); self._btns.add(b)
            else:
                self.mouse.release(b); self._btns.discard(b)
        elif t == "scroll":
            if not e.get("game"):
                self.mouse.position = self._abs(e["x"], e["y"])
            self.mouse.scroll(e.get("dx", 0), e.get("dy", 0))
        elif t == "key":
            if e.get("special"):
                key = getattr(Key, e["key"], None)
            elif e.get("vk") is not None:
                # Physical key by virtual-key code: lets Shift/Ctrl/Alt compose
                # naturally and keeps game movement keys working while modifiers held.
                try:
                    key = KeyCode.from_vk(int(e["vk"]))
                except Exception:
                    key = None
            else:
                key = e.get("key")
            if key is None:
                return
            try:
                if e.get("action") == "press":
                    self.keyboard.press(key); self._keys.add(key)
                else:
                    self.keyboard.release(key); self._keys.discard(key)
            except Exception:
                pass
        elif t == "release_all":
            self.release_all()

    def release_all(self):
        for k in list(self._keys):
            try: self.keyboard.release(k)
            except Exception: pass
        for b in list(self._btns):
            try: self.mouse.release(b)
            except Exception: pass
        self._keys.clear(); self._btns.clear()


class AdaptiveEncoder:
    """Keeps the frame rate up by shedding load when a full frame (grab + encode
    + send) takes longer than the target interval. Resolution is dropped first
    (the biggest win for both CPU and bandwidth), then quality."""

    def __init__(self, quality, min_scale=SMOOTH_MIN_SCALE, start_scale=SCALE, max_scale=SCALE):
        self.max_quality = quality
        self.quality = quality
        self.min_scale = min_scale
        self.max_scale = max_scale       # ceiling recovery is allowed to climb back to
        self.scale = start_scale         # where we begin (capped up-front on big screens)
        self._ema = 0.0

    def note_frame_time(self, dt, interval):
        self._ema = 0.6 * self._ema + 0.4 * dt
        if self._ema > interval * 0.8:          # behind -> lighten load early (keeps latency low)
            if self.scale > self.min_scale:
                self.scale = round(max(self.min_scale, self.scale - 0.1), 2)
            elif self.quality > MIN_QUALITY:
                self.quality = max(MIN_QUALITY, self.quality - 4)
        elif self._ema < interval * 0.45:       # headroom -> improve again
            if self.quality < self.max_quality:
                self.quality = min(self.max_quality, self.quality + 3)
            elif self.scale < self.max_scale:
                self.scale = round(min(self.max_scale, self.scale + 0.05), 2)


def monitor_geometry(monitor_index):
    with mss.mss() as sct:
        m = sct.monitors[monitor_index]
    return m["left"], m["top"], m["width"], m["height"]


# Fallback arrow, used only if the real cursor can't be captured (the capture APIs
# don't include the cursor). A classic arrow shape.
_CURSOR_PX = 32                          # native cursor size (must match the capture size)
_CURSOR_POLY = np.array([[0, 0], [0, 16], [4, 12], [7, 19], [10, 18], [6, 11], [11, 11]], np.float32)


def _blit_cursor(img, x, y, color, transp):
    """Alpha-composite a premultiplied cursor (color) with per-pixel transparency onto img.
    out = color + background * transparency  (transparency: 0 = opaque, 1 = clear)."""
    ih, iw = img.shape[:2]
    ch, cw = color.shape[:2]
    sx, sy = max(0, -x), max(0, -y)                 # crop offset when the cursor hangs off an edge
    dx0, dy0 = max(0, x), max(0, y)
    dx1, dy1 = min(iw, x + cw), min(ih, y + ch)
    if dx1 <= dx0 or dy1 <= dy0:
        return
    bw, bh = dx1 - dx0, dy1 - dy0
    col = color[sy:sy + bh, sx:sx + bw]
    tr = transp[sy:sy + bh, sx:sx + bw]
    roi = img[dy0:dy1, dx0:dx1].astype(np.float32)
    img[dy0:dy1, dx0:dx1] = np.clip(col + roi * tr, 0, 255).astype(np.uint8)


def _draw_cursor(img, x, y, hcursor=None, scale=1.0):
    """Draw the mouse cursor into the frame at (x, y) in FRAME pixels, sized to `scale`
    so it stays the same apparent size no matter how the stream resolution adapts."""
    x, y = int(round(x)), int(round(y))
    size = max(8, int(round(_CURSOR_PX * scale)))
    layers = cursor_layers(hcursor)
    if layers is not None:
        color, transp = layers                       # real 32x32 cursor (color + alpha)
        interp = cv2.INTER_AREA if size < _CURSOR_PX else cv2.INTER_LINEAR
        col = cv2.resize(color, (size, size), interpolation=interp)
        tr = cv2.resize(transp, (size, size), interpolation=interp)
        _blit_cursor(img, x, y, col, tr)
        return
    # Fallback: a size-stable synthetic arrow (scales with the stream, so no more grow/shrink).
    h, w = img.shape[:2]
    if not (-size <= x < w and -size <= y < h):
        return
    pts = (_CURSOR_POLY * scale + (x, y)).astype(np.int32)
    cv2.polylines(img, [pts], True, (0, 0, 0), max(1, int(round(3 * scale))), cv2.LINE_AA)
    cv2.fillPoly(img, [pts], (255, 255, 255), cv2.LINE_AA)
    cv2.polylines(img, [pts], True, (0, 0, 0), 1, cv2.LINE_AA)


class ScreenGrabber:
    """bettercam (Desktop Duplication, captures games) with mss fallback. One thread only."""
    def __init__(self, monitor_index, target_fps=75):
        self.backend = "mss"
        self.cam = None
        self._sct = None
        self._prev = None
        self._last_full = None
        self._started = False
        try:
            self.left, self.top = monitor_geometry(monitor_index)[:2]
        except Exception:
            self.left, self.top = 0, 0
        if USE_BETTERCAM:
            try:
                import bettercam
                self.cam = bettercam.create(output_idx=max(0, monitor_index - 1), output_color="BGR")
                # Continuous capture — far faster than a one-shot grab() per frame
                # (grab() has heavy per-call overhead that caps you near ~20 fps).
                # video_mode keeps a steady supply (repeats the last frame if static).
                self.cam.start(target_fps=int(target_fps), video_mode=True)
                self._started = True
                self.backend = "bettercam"
            except Exception:
                try:
                    if self.cam is not None:
                        self.cam.release()
                except Exception:
                    pass
                self.cam = None
        if self.cam is None:
            self._sct = mss.mss()
            self._monitor = self._sct.monitors[monitor_index]

    def grab_jpeg(self, quality, scale, force):
        if self.cam is not None:
            try:
                frame = self.cam.get_latest_frame()
            except Exception:
                frame = None                      # e.g. a game grabbed exclusive fullscreen
            if frame is None:
                if self._last_full is None:
                    return None
                frame = self._last_full
            else:
                self._last_full = frame
            bgr = frame
        else:
            bgr = cv2.cvtColor(np.asarray(self._sct.grab(self._monitor)), cv2.COLOR_BGRA2BGR)
            sig = cv2.resize(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (64, 64), interpolation=cv2.INTER_AREA)
            if not force and self._prev is not None and float(np.mean(cv2.absdiff(sig, self._prev))) < DIFF_THRESHOLD:
                return None
            self._prev = sig
        if scale != 1.0:
            # INTER_LINEAR is much faster than INTER_AREA for downscaling and looks
            # nearly identical on live video — more fps / less delay.
            bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
        # Draw the REAL OS cursor into the frame when it's visible (menus, chests, desktop)
        # so the controller can see and click it. Hidden during game-play automatically.
        # Sized by `scale` so it stays a constant apparent size as the stream resolution adapts.
        cx, cy, hcur = cursor_state()
        if cx is not None:
            if scale == 1.0:
                bgr = bgr.copy()                  # don't scribble on the capture buffer
            _draw_cursor(bgr, (cx - self.left) * scale, (cy - self.top) * scale, hcur, scale)
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
        if hasattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR"):
            h, w = bgr.shape[:2]
            if w * h <= BIG_FRAME_PIXELS:       # ~1080p or less: 4:4:4, crisp edges/text
                samp = getattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR_444", 0x111111)
            else:                               # bigger: 4:2:0, far faster to encode + smaller
                samp = getattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR_420", 0x221111)
            params += [int(cv2.IMWRITE_JPEG_SAMPLING_FACTOR), int(samp)]
        ok, buf = cv2.imencode(".jpg", bgr, params)
        return buf.tobytes() if ok else None

    def close(self):
        # Only stop() the capture thread. We deliberately DON'T call release():
        # bettercam's COM release can throw a native access violation that can
        # crash the whole process. Dropping the reference lets it clean up safely.
        try:
            if self.cam is not None and self._started:
                self.cam.stop()
        except Exception: pass
        self.cam = None
        try:
            if self._sct is not None: self._sct.close()
        except Exception: pass


class Capture:
    def __init__(self, monitor_index, emit):
        self.monitor_index = monitor_index
        self.emit = emit
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture")
        self._grabber = None

    def _grab(self, quality, scale, force):
        if self._grabber is None:
            self._grabber = ScreenGrabber(self.monitor_index)
            self.emit("log", f"Capturing with: {self._grabber.backend}")
        return self._grabber.grab_jpeg(quality, scale, force)

    async def grab(self, loop, quality, scale, force):
        return await loop.run_in_executor(self.executor, self._grab, quality, scale, force)

    def close(self):
        def _c():
            if self._grabber: self._grabber.close()
        try: self.executor.submit(_c).result(timeout=2)
        except Exception: pass
        self.executor.shutdown(wait=False)


# ===========================================================================
#  HOST side: server so the OTHER PC can control this one
# ===========================================================================
class HostBackend:
    def __init__(self, events, monitor_index=1, quality=DEFAULT_QUALITY, fps=DEFAULT_FPS,
                 min_scale=SMOOTH_MIN_SCALE):
        self.events = events
        self.monitor_index = monitor_index
        self.quality = quality
        self.min_scale = min_scale
        self.interval = 1.0 / max(1, fps)
        self.loop = None
        self._async_stop = None
        self._session = None            # the Event that ends the CURRENT control session
        self._errored = False           # set when startup fails, so we show why

    def start(self):
        threading.Thread(target=self._thread_main, daemon=True).start()

    def stop(self):
        if self.loop and self._async_stop:
            self.loop.call_soon_threadsafe(self._async_stop.set)

    def kick(self):
        """End the current control session but keep sharing on (host stays ready)."""
        if self.loop and self._session is not None:
            sess = self._session
            self.loop.call_soon_threadsafe(sess.set)

    def _emit(self, *m): self.events.put(m)

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._errored = False
        try:
            self.loop.run_until_complete(self._run())
        except Exception as e:
            self._errored = True
            self._emit("host", "error", f"Couldn't start sharing: {e}")
        finally:
            # Don't overwrite a visible error message with a plain "off" state.
            if not self._errored:
                self._emit("host", "offline")

    async def _run(self):
        self._async_stop = asyncio.Event()
        try:
            self._capture = Capture(self.monitor_index, self._emit)
        except Exception as e:
            self._errored = True
            self._emit("host", "error", f"Can't capture this screen: {e}")
            return
        try:
            async with websockets.serve(self._handle, "0.0.0.0", PORT, max_size=None,
                                        ping_interval=10, ping_timeout=10):
                self._emit("host", "ready")
                await self._async_stop.wait()
        except OSError:
            self._errored = True
            self._emit("host", "error",
                       f"Port {PORT} is already in use — another copy of this app may "
                       f"already be sharing. Close it and turn sharing on again.")
        finally:
            try:
                self._capture.close()
            except Exception:
                pass

    async def _auth(self, ws):
        prefs = {}
        try:
            first = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(first) if isinstance(first, str) else {}
            ok = data.get("token") == SECRET
            if ok and isinstance(data.get("prefs"), dict):
                prefs = data["prefs"]
        except Exception:
            ok = False
        await ws.send(json.dumps({"type": "auth", "ok": ok}))
        return ok, prefs

    async def _handle(self, ws):
        addr = ws.remote_address[0] if ws.remote_address else "?"
        # Reject a PC connecting to itself (misconfigured peer) — that would show up
        # as being "controlled" with nobody actually there.
        if addr in ("127.0.0.1", "::1", "localhost"):
            try: await ws.close()
            except Exception: pass
            return
        authok, prefs = await self._auth(ws)
        if not authok:
            await ws.close(); return
        # Take over any existing session instead of rejecting as "busy": a reconnect
        # from the same user should replace a stale/zombie session, not be blocked.
        my_stop = asyncio.Event()
        prev, self._session = self._session, my_stop
        if prev is not None:
            prev.set()
        self._emit("host", "controlled", addr)
        geo = monitor_geometry(self.monitor_index)
        controller = InputController(*geo)
        mon_w = max(geo[2], 1)

        # The CONTROLLER picks quality/resolution/fps for this session (its "prefs");
        # fall back to this PC's saved settings when none are sent.
        quality = int(prefs.get("quality", self.quality))
        fps = int(prefs.get("fps", 0) or 0)
        interval = (1.0 / max(1, fps)) if fps else self.interval
        sharp = bool(prefs.get("sharp", self.min_scale >= 1.0))
        target_w = int(prefs.get("target_w", 0) or 0)   # 0 = native
        min_scale = SHARP_MIN_SCALE if sharp else SMOOTH_MIN_SCALE
        if sharp:
            start = maxs = 1.0
        elif target_w and mon_w > target_w:             # cap to the chosen width
            start = maxs = round(target_w / mon_w, 2)
        elif mon_w > SMOOTH_TARGET_W:                   # big screen, no explicit target
            start = maxs = round(SMOOTH_TARGET_W / mon_w, 2)
        else:
            start = maxs = 1.0
        encoder = AdaptiveEncoder(quality, min_scale=min_scale, start_scale=start, max_scale=maxs)
        paired = asyncio.Event(); paired.set()
        try:
            tasks = [
                asyncio.create_task(self._read(ws, controller)),
                asyncio.create_task(self._stream(ws, self._capture, encoder, paired, interval)),
                asyncio.create_task(my_stop.wait()),
                asyncio.create_task(self._async_stop.wait()),
            ]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending: t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            controller.release_all()
            replaced = self._session is not my_stop     # a newer session took over
            if not replaced:
                self._session = None
            # If THIS PC ended the session (turned sharing off, or pressed Disconnect),
            # tell the controller so it shows a message and stops — instead of a silent
            # drop (and a possible auto-reconnect). Not on takeover / controller leaving.
            turned_off = self._async_stop.is_set()
            kicked = my_stop.is_set() and not replaced and not turned_off
            if turned_off or kicked:
                reason = (f"Oops — it looks like {NAME} turned off remote control."
                          if turned_off else f"{NAME} disconnected you.")
                try:
                    await ws.send(json.dumps({"type": "bye", "reason": reason}))
                except Exception:
                    pass
            try:
                await ws.close()
            except Exception:
                pass
            if not replaced:                             # newer session already shows "controlled"
                self._emit("host", "ready")

    async def _read(self, ws, controller):
        async for msg in ws:
            if isinstance(msg, bytes):
                continue
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong", "t": data.get("t")}))
            else:
                controller.handle(data)

    async def _stream(self, ws, capture, encoder, paired, interval=None):
        loop = asyncio.get_running_loop()
        interval = interval or self.interval
        last_key = last_stat = 0.0

        def submit(now):
            force = (now - last_key) >= KEYFRAME_EVERY
            return asyncio.ensure_future(capture.grab(loop, encoder.quality, encoder.scale, force))

        await paired.wait()
        pending = submit(loop.time())           # start the first grab/encode
        try:
            while True:
                await paired.wait()
                start = loop.time()
                jpeg = await pending
                # Kick off the NEXT grab+encode now so it runs (in its own thread)
                # while we send the current frame — overlap is what lifts the fps.
                pending = submit(start)
                if jpeg is not None:
                    await ws.send(jpeg)
                    last_key = start
                # React to the WHOLE frame cost (grab + encode + send), not just send.
                encoder.note_frame_time(loop.time() - start, interval)
                if start - last_stat >= 1.0:
                    await ws.send(json.dumps({"type": "stat", "q": encoder.quality,
                                              "scale": encoder.scale}))
                    last_stat = start
                elapsed = loop.time() - start
                if elapsed < interval:
                    await asyncio.sleep(interval - elapsed)
        finally:
            pending.cancel()


# ===========================================================================
#  CLIENT side: connect out to the OTHER PC to control it
# ===========================================================================
class ClientBackend:
    def __init__(self, peer, events, prefs=None):
        self.uri = f"ws://{peer}:{PORT}"
        self.events = events
        self.prefs = prefs or {}          # quality/resolution chosen for THIS session
        self.loop = None
        self._async_stop = None
        self._outgoing = None
        self.frame_counter = 0
        self.latest_frame = None
        self.paired = False

    def start(self):
        threading.Thread(target=self._thread_main, daemon=True).start()

    def stop(self):
        if self.loop and self._async_stop:
            self.loop.call_soon_threadsafe(self._async_stop.set)

    def send(self, payload):
        if self.loop and self._outgoing is not None and self.paired:
            self.loop.call_soon_threadsafe(self._outgoing.put_nowait, payload)

    def _emit(self, *m): self.events.put(m)

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._run())
        except Exception as e:
            self._emit("status", f"error: {e}")
        finally:
            self._emit("closed")

    async def _run(self):
        self._async_stop = asyncio.Event()
        self._outgoing = asyncio.Queue()
        backoff = 1
        first_fail = None                         # when we started failing to connect
        MAX_CONNECT_WAIT = 90                     # give up after this long (don't latch on later)
        while not self._async_stop.is_set():
            try:
                self._emit("status", f"Connecting to {PEER} ...")
                async with websockets.connect(self.uri, max_size=None,
                                              ping_interval=10, ping_timeout=10) as ws:
                    await ws.send(json.dumps({"type": "auth", "token": SECRET,
                                              "prefs": self.prefs}))
                    reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if not reply.get("ok", False):
                        raise ConnectionError("wrong password")
                    self.paired = True
                    first_fail = None
                    self._emit("paired", True)
                    tasks = [
                        asyncio.create_task(self._recv(ws)),
                        asyncio.create_task(self._send_loop(ws)),
                        asyncio.create_task(self._ping(ws)),
                        asyncio.create_task(self._async_stop.wait()),
                    ]
                    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending: t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                self.paired = False
                self._emit("paired", False)
                if self._async_stop.is_set():
                    return
                backoff = 1
                first_fail = None
            except ConnectionError as e:
                self._emit("fatal", str(e)); return
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
                self.paired = False
                self._emit("paired", False)
                if self._async_stop.is_set():
                    return
                now = time.time()
                if first_fail is None:
                    first_fail = now
                elif now - first_fail > MAX_CONNECT_WAIT:
                    # Stop instead of retrying forever, so a forgotten "Control" window
                    # doesn't silently connect (and control) the other PC minutes later.
                    self._emit("fatal", f"Couldn't reach {PEER}. Make sure it has "
                                        f"'Allow this PC to be controlled' turned on, then try again.")
                    return
                self._emit("status", f"Can't reach {PEER} — retry in {backoff}s...")
                try:
                    await asyncio.wait_for(self._async_stop.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 10)

    async def _recv(self, ws):
        async for msg in ws:
            if isinstance(msg, bytes):
                self.latest_frame = msg
                self.frame_counter += 1
            else:
                data = json.loads(msg)
                if data.get("type") == "pong":
                    self._emit("rtt", (time.time() - data["t"]) * 1000)
                elif data.get("type") == "stat":
                    self._emit("stat", data.get("q"), data.get("scale"))
                elif data.get("type") == "bye":
                    # The other PC turned off "allow control" — stop cleanly, don't retry.
                    self._emit("fatal", data.get("reason") or
                               "The other PC stopped allowing remote control.")
                    self._async_stop.set()
                    return
                elif data.get("type") == "busy":
                    self._emit("fatal", "That PC is already being controlled by someone.")
                    self._async_stop.set()
                    return

    async def _send_loop(self, ws):
        while True:
            payload = await self._outgoing.get()
            try:
                await ws.send(json.dumps(payload))
            except websockets.WebSocketException:
                return

    async def _ping(self, ws):
        while True:
            await asyncio.sleep(1.0)
            if self.paired:
                try:
                    await ws.send(json.dumps({"type": "ping", "t": time.time()}))
                except websockets.WebSocketException:
                    return


# ===========================================================================
#  UNIFIED GUI
# ===========================================================================
class RemoteDesktopApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        apply_theme(self.settings.get("accent", "#22D3EE"))
        ctk.set_appearance_mode(self.settings.get("appearance", "Dark"))
        try:
            ctk.set_default_color_theme(self.settings.get("theme", "blue"))
        except Exception:
            ctk.set_default_color_theme("blue")
        try:
            ctk.set_widget_scaling(float(self.settings.get("ui_scale", 1.0)))
        except Exception:
            pass
        self.title(f"doris pccontrol  ·  v{APP_VERSION}")
        self.geometry("1280x820")
        self.minsize(960, 640)
        self.configure(fg_color=BG)
        try:
            self.attributes("-alpha", 0.0)     # fade in on launch
        except Exception:
            pass
        try:
            self.iconbitmap(ICON)
        except Exception:
            pass
        self._icon_img = None
        try:
            self._icon_img = ctk.CTkImage(Image.open(ICON), size=(30, 30))
        except Exception:
            pass

        self.host_events = queue.Queue()
        self.client_events = queue.Queue()
        self.client = None
        self.host = None
        self.sharing = False
        self.host_state = "off"
        self.client_paired = False
        self.paused = False
        self.fullscreen = False
        self.game_mode = False
        self.video_rect = (0, 0, 1, 1)
        self._last_counter = -1
        self._photo = None
        self._pending_move = None
        self._pending_rmove = [0, 0]
        self._lock_last = None          # last pointer pos while in mouse-look mode
        self._fps = deque(maxlen=60)
        self._bytes = deque(maxlen=120)
        self.rtt = None
        self.q = self.scale = None
        # client video decode runs on its own thread so heavy 4K decode/resize
        # never blocks the UI (that was capping the displayed frame rate).
        self._decode_thread = None
        self._decoding = False
        self._decoded = None            # (rgb_array, ox, oy, dw, dh, nbytes)
        self._decoded_counter = 0
        self._drawn_counter = 0
        self._canvas_wh = (1, 1)        # published by the main thread for the worker

        self._build_home()
        self._build_session()
        self._show_home()

        self.bind("<F11>", lambda e: self._toggle_fullscreen())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll)
        self.after(400, self._reassert_icon)   # CTk can reset the icon after init
        self.after(60, self._fade_in)
        self._pulse_dot()                      # single pulse loop (survives UI rebuilds)

    def _rebuild_ui(self):
        """Tear down and rebuild the home + session screens (used when the accent
        color changes so the whole UI recolors live)."""
        on_home = not (hasattr(self, "session") and self.session.winfo_ismapped())
        try: self.home.destroy()
        except Exception: pass
        try: self.session.destroy()
        except Exception: pass
        self._build_home()
        self._build_session()
        self._apply_host_state(self.host_state)
        (self._show_home if on_home else self._show_session)()

    def _fade_in(self):
        try:
            a = min(1.0, (self.attributes("-alpha") or 0.0) + 0.12)
            self.attributes("-alpha", a)
            if a < 1.0:
                self.after(16, self._fade_in)
        except Exception:
            try:
                self.attributes("-alpha", 1.0)
            except Exception:
                pass

    def _reassert_icon(self):
        try:
            self.iconbitmap(ICON)
        except Exception:
            pass

    # ---- futuristic building blocks -------------------------------------
    def _corner_brackets(self, parent, color=ACCENT_DIM, size=16, thick=2, pad=10):
        """Four L-shaped HUD brackets in the corners of `parent`."""
        for relx, anchor in ((0.0, "nw"), (1.0, "ne")):
            for rely, va in ((0.0, "n"), (1.0, "s")):
                ax = ("w" if relx == 0.0 else "e")
                cy = ("n" if rely == 0.0 else "s")
                h = ctk.CTkFrame(parent, width=size, height=thick, fg_color=color,
                                 corner_radius=0)
                h.place(relx=relx, rely=rely, anchor=cy + ax,
                        x=(pad if relx == 0 else -pad), y=(pad if rely == 0 else -pad))
                v = ctk.CTkFrame(parent, width=thick, height=size, fg_color=color,
                                 corner_radius=0)
                v.place(relx=relx, rely=rely, anchor=cy + ax,
                        x=(pad if relx == 0 else -pad), y=(pad if rely == 0 else -pad))

    def _accent_underline(self, parent, width=44, height=2):
        img = _gradient_image(width, height)
        lbl = ctk.CTkLabel(parent, text="", image=img)
        lbl._img_ref = img
        return lbl

    # ---- home page -------------------------------------------------------
    def _build_home(self):
        self.home = ctk.CTkFrame(self, fg_color=BG)

        # deep-space starfield behind everything (stars, nebula, a planet + a satellite)
        import tkinter as tk
        self._grid_canvas = tk.Canvas(self.home, bg=SPACE_BG, highlightthickness=0, bd=0)
        self._grid_canvas.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._grid_canvas.bind("<Configure>", self._draw_space)
        self._star_t = 0.0
        self.after(120, self._twinkle)

        header = ctk.CTkFrame(self.home, fg_color="transparent")
        header.pack(fill="x", padx=32, pady=(22, 0))
        if self._icon_img is not None:
            ctk.CTkLabel(header, image=self._icon_img, text="").pack(side="left")
        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.pack(side="left", padx=(8, 0))
        row = ctk.CTkFrame(brand, fg_color="transparent"); row.pack(anchor="w")
        ctk.CTkLabel(row, text="doris ", text_color=TEXT,
                     font=ctk.CTkFont(FONT_UI, 24, weight="bold")).pack(side="left")
        ctk.CTkLabel(row, text="pccontrol", text_color=ACCENT,
                     font=ctk.CTkFont(FONT_UI, 24, weight="bold")).pack(side="left")
        self._accent_underline(brand, width=120, height=2).pack(anchor="w", pady=(3, 0))

        ctk.CTkButton(header, text="⚙  Settings", width=104, height=34, corner_radius=17,
                      fg_color="transparent", border_width=1, border_color=BORDER,
                      hover_color=SURF_HOVER, text_color=TEXT_BODY,
                      command=self._open_settings).pack(side="right")
        ver = ctk.CTkFrame(header, corner_radius=12, fg_color=SURF_INSET,
                           border_width=1, border_color=BORDER)
        ver.pack(side="right", padx=10)
        ctk.CTkLabel(ver, text=f"v{APP_VERSION}", text_color=ACCENT,
                     font=ctk.CTkFont(FONT_MONO, 12, weight="bold")).pack(padx=10, pady=4)
        ctk.CTkLabel(header, text=f"This PC · {NAME}", text_color=MUTED,
                     font=ctk.CTkFont(FONT_UI, 13)).pack(side="right", padx=(0, 4))

        # two action cards, side by side
        grid = ctk.CTkFrame(self.home, fg_color="transparent")
        grid.pack(expand=True, padx=32, pady=18)
        c1 = self._make_card(grid, "◈", "Control your other PC",
                             f"Take over {PEER or 'the other computer'}'s screen and mouse.",
                             accent=True)
        c2 = self._make_card(grid, "⊚", "Allow this PC to be controlled",
                             "Turn on and leave the app open so your other PC can connect.",
                             accent=False)
        c1.grid(row=0, column=0, padx=(0, 12), sticky="nsew")
        c2.grid(row=0, column=1, padx=(12, 0), sticky="nsew")
        grid.grid_columnconfigure((0, 1), weight=1, uniform="cards")
        grid.grid_rowconfigure(0, weight=1)

        # --- card 1 action: connect button ---
        self.control_btn = ctk.CTkButton(
            c1.body, text=f"Connect  →", height=48, corner_radius=10,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ON_ACCENT,
            font=ctk.CTkFont(FONT_UI, 15, weight="bold"), command=self._start_control)
        self.control_btn.pack(fill="x", side="bottom")

        # --- card 2 action: status dot + switch ---
        arow = ctk.CTkFrame(c2.body, fg_color="transparent")
        arow.pack(fill="x", side="bottom")
        dotwrap = ctk.CTkFrame(arow, width=22, height=22, corner_radius=11, fg_color=BG_LAYER)
        dotwrap.pack(side="left"); dotwrap.pack_propagate(False)
        self.dot_halo = ctk.CTkFrame(dotwrap, width=18, height=18, corner_radius=9, fg_color=BG_LAYER)
        self.dot_halo.place(relx=0.5, rely=0.5, anchor="center")
        self.dot = ctk.CTkLabel(self.dot_halo, text="●", text_color="#41506a",
                                font=ctk.CTkFont(FONT_UI, 12))
        self.dot.place(relx=0.5, rely=0.5, anchor="center")
        self.share_label = ctk.CTkLabel(arow, text="Sharing off", text_color=MUTED,
                                        font=ctk.CTkFont(FONT_UI, 13))
        self.share_label.pack(side="left", padx=(8, 0))
        self.share_btn = ctk.CTkButton(
            arow, text="Turn on", width=96, height=34, corner_radius=17,
            fg_color="transparent", border_width=1, border_color=BORDER,
            hover_color=SURF_HOVER, text_color=TEXT,
            font=ctk.CTkFont(FONT_UI, 13, weight="bold"), command=self._toggle_share)
        self.share_btn.pack(side="right")
        # shown only while someone is controlling this PC — kicks them, keeps sharing on
        self.kick_btn = ctk.CTkButton(
            arow, text="Disconnect", width=104, height=34, corner_radius=17,
            fg_color="transparent", border_width=1, border_color=RED, text_color=RED,
            hover_color=RED_DIM, font=ctk.CTkFont(FONT_UI, 13, weight="bold"),
            command=self._kick_controller)

        self.home_status = ctk.CTkLabel(self.home, text="", text_color=WARNING,
                                        font=ctk.CTkFont(FONT_UI, 13), wraplength=760,
                                        justify="left")
        self.home_status.pack(padx=34, pady=(0, 18))

        if not PEER or not SECRET:
            self.control_btn.configure(state="disabled", text="Run Setup first")
            self.home_status.configure(text="⚠  Not set up yet — run the Setup Wizard (INSTALL.bat).")
        elif NAME and NAME != "this PC" and PEER.strip().lower() == NAME.strip().lower():
            self.home_status.configure(
                text="⚠  The other-PC name is the same as this PC. Re-run Setup and enter the "
                     "OTHER computer's Tailscale name.")

    def _make_card(self, parent, glyph, title, subtitle, accent):
        """A HUD-style action card. Returns the card frame with a `.body` for actions."""
        card = ctk.CTkFrame(parent, corner_radius=16, fg_color=CARD,
                            border_width=1, border_color=(ACCENT_DIM if accent else BORDER))
        card.configure(width=340, height=230)
        pad = ctk.CTkFrame(card, fg_color="transparent")
        pad.pack(fill="both", expand=True, padx=22, pady=20)
        self._corner_brackets(card, color=(ACCENT_DIM if accent else BORDER))

        disc = ctk.CTkFrame(pad, width=46, height=46, corner_radius=23, fg_color=SURF_INSET,
                            border_width=1, border_color=(ACCENT_DIM if accent else BORDER))
        disc.pack(anchor="w"); disc.pack_propagate(False)
        ctk.CTkLabel(disc, text=glyph, text_color=(ACCENT if accent else TEXT_BODY),
                     font=ctk.CTkFont(FONT_UI, 22)).place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(pad, text=title, text_color=TEXT, justify="left",
                     font=ctk.CTkFont(FONT_UI, 17, weight="bold")).pack(anchor="w", pady=(14, 0))
        if accent:
            self._accent_underline(pad, width=40, height=2).pack(anchor="w", pady=(4, 0))
        ctk.CTkLabel(pad, text=subtitle, text_color=TEXT_BODY, justify="left", wraplength=280,
                     font=ctk.CTkFont(FONT_UI, 13)).pack(anchor="w", pady=(8, 0))

        card.body = ctk.CTkFrame(pad, fg_color="transparent")
        card.body.pack(fill="x", side="bottom", pady=(16, 0))

        def enter(_):
            card.configure(fg_color=SURF_HOVER, border_color=ACCENT)
        def leave(_):
            card.configure(fg_color=CARD, border_color=(ACCENT_DIM if accent else BORDER))
        for w in (card, pad):
            w.bind("<Enter>", enter); w.bind("<Leave>", leave)
        return card

    # ---- deep-space background -------------------------------------------
    def _draw_space(self, event=None):
        """Paint the starfield sky: vertical gradient, two nebula clouds, a planet,
        a satellite, and a field of stars (twinkled separately)."""
        import random
        c = getattr(self, "_grid_canvas", None)
        if c is None or not c.winfo_exists():
            return
        w = event.width if event is not None else c.winfo_width()
        h = event.height if event is not None else c.winfo_height()
        if w < 4 or h < 4:
            return
        c.delete("space")
        # 1) vertical gradient sky (indigo at the top -> the void at the bottom)
        top = tuple(int(SPACE_TOP[i:i + 2], 16) for i in (1, 3, 5))
        bot = tuple(int(SPACE_BG[i:i + 2], 16) for i in (1, 3, 5))
        bands = 48
        for i in range(bands):
            t = i / (bands - 1)
            col = "#%02x%02x%02x" % tuple(int(top[j] + (bot[j] - top[j]) * t) for j in range(3))
            c.create_rectangle(0, int(h * i / bands), w, int(h * (i + 1) / bands) + 1,
                               fill=col, outline="", tags=("space", "bg"))
        # 2) nebula clouds (soft glows blended toward the void)
        self._draw_nebula(c, int(w * 0.80), int(h * 0.24), max(w, h) * 0.55, SPACE_NEBULA1)
        self._draw_nebula(c, int(w * 0.12), int(h * 0.86), max(w, h) * 0.42, SPACE_NEBULA2)
        # 3) the star field
        if getattr(self, "_stars", None) is None or self._stars_wh != (w, h):
            rnd = random.Random(42)
            self._stars = [(rnd.randint(0, w), rnd.randint(0, h),
                            rnd.choice([1, 1, 1, 1, 2, 2, 3]), rnd.random())
                           for _ in range(190)]
            self._stars_wh = (w, h)
        self._paint_stars(c)
        # 4) foreground bodies (drawn above the stars)
        self._draw_planet(c, int(w * 0.86), int(h * 0.82), max(18, int(min(w, h) * 0.085)))
        self._draw_satellite(c, int(w * 0.19), int(h * 0.30), max(0.8, min(w, h) / 700))
        # a lone shooting star streak, top-left quadrant
        sx, sy = int(w * 0.30), int(h * 0.16)
        for k in range(10):
            t = k / 9
            col = _blend(SPACE_BG, SPACE_STAR, 1 - t)
            c.create_line(sx - k * 7, sy - k * 3, sx - (k + 1) * 7, sy - (k + 1) * 3,
                          fill=col, width=2, tags=("space", "fg"))
        c.lower("space")

    def _paint_stars(self, c):
        import math
        c.delete("star")
        for (sx, sy, sr, ph) in getattr(self, "_stars", []):
            tw = 0.55 + 0.45 * math.sin(self._star_t + ph * 6.283)   # per-star twinkle
            b = int(120 + 135 * tw)
            col = "#%02x%02x%02x" % (b, b, min(255, b + 18))
            c.create_oval(sx - sr, sy - sr, sx + sr, sy + sr, fill=col, outline="",
                          tags=("space", "star"))
            if sr >= 3:                                              # a glint on the brightest
                c.create_line(sx - 6, sy, sx + 6, sy, fill=col, tags=("space", "star"))
                c.create_line(sx, sy - 6, sx, sy + 6, fill=col, tags=("space", "star"))

    def _draw_nebula(self, c, cx, cy, radius, rgb):
        """A soft radial glow built from concentric ovals blended toward the void."""
        hexcol = "#%02x%02x%02x" % rgb
        layers = 16
        for i in range(layers):
            t = i / (layers - 1)
            r = radius * (1 - t)
            col = _blend(SPACE_BG, hexcol, (1 - t) * 0.28)           # subtle, brighter in the core
            c.create_oval(cx - r, cy - r * 0.72, cx + r, cy + r * 0.72,
                          fill=col, outline="", tags=("space", "bg"))

    def _draw_planet(self, c, cx, cy, r):
        """A ringed planet with a lit limb and a couple of bands."""
        # ring behind
        c.create_oval(cx - r * 1.9, cy - r * 0.55, cx + r * 1.9, cy + r * 0.55,
                      outline=_blend(SPACE_BG, "#C4B5FD", 0.55), width=max(2, int(r * 0.10)),
                      tags=("space", "fg"))
        # body with a simple day/night shade (concentric offset ovals)
        base = "#7C3AED"
        for i in range(10):
            t = i / 9
            rr = r * (1 - t * 0.06)
            ox = int(r * 0.16 * t)                                    # shift toward the lit side
            col = _blend("#2A1A5E", base, 0.35 + 0.65 * t)
            c.create_oval(cx - rr - ox, cy - rr, cx + rr - ox, cy + rr,
                          fill=col, outline="", tags=("space", "fg"))
        # bright highlight
        c.create_oval(cx - r * 0.5, cy - r * 0.6, cx - r * 0.1, cy - r * 0.2,
                      fill=_blend(base, "#EDE9FE", 0.5), outline="", tags=("space", "fg"))
        # ring front (over the body, lower half)
        c.create_arc(cx - r * 1.9, cy - r * 0.55, cx + r * 1.9, cy + r * 0.55,
                     start=180, extent=180, style="arc",
                     outline=_blend(SPACE_BG, "#DDD6FE", 0.7), width=max(2, int(r * 0.10)),
                     tags=("space", "fg"))

    def _draw_satellite(self, c, cx, cy, s):
        """A little satellite: body, two solar-panel wings, a dish and an antenna."""
        A = ACCENT
        def R(x0, y0, x1, y1, fill, outline="", wdt=1):
            c.create_rectangle(cx + x0 * s, cy + y0 * s, cx + x1 * s, cy + y1 * s,
                               fill=fill, outline=outline, width=wdt, tags=("space", "fg"))
        def L(x0, y0, x1, y1, fill, wdt=1):
            c.create_line(cx + x0 * s, cy + y0 * s, cx + x1 * s, cy + y1 * s,
                          fill=fill, width=wdt, tags=("space", "fg"))
        panel = _blend(SPACE_BG, A, 0.55)
        edge = _blend(SPACE_BG, A, 0.9)
        # solar wings
        for side in (-1, 1):
            R(side * 20, -16, side * 52, 16, panel, edge, 1)
            for gx in range(1, 4):                                    # cell lines
                L(side * (20 + gx * 8), -16, side * (20 + gx * 8), 16, edge, 1)
            L(side * 12, 0, side * 20, 0, edge, max(1, int(2 * s)))    # strut
        # body
        R(-12, -14, 12, 14, _blend(SPACE_BG, "#CBD5E1", 0.75), edge, 1)
        R(-8, -10, 8, 10, _blend(SPACE_BG, A, 0.35), "", 0)
        # dish
        c.create_oval(cx - 9 * s, cy - 30 * s, cx + 9 * s, cy - 12 * s,
                      outline=edge, width=max(1, int(1.6 * s)), tags=("space", "fg"))
        L(0, -14, 0, -21, edge, max(1, int(1.6 * s)))                 # dish mast
        c.create_oval(cx - 1.6 * s, cy - 22.6 * s, cx + 1.6 * s, cy - 19.4 * s,
                      fill=edge, outline="", tags=("space", "fg"))

    def _twinkle(self):
        import math
        c = getattr(self, "_grid_canvas", None)
        if c is None or not c.winfo_exists():
            return                                                    # home rebuilt/closed — let this loop die
        self._star_t += 0.22
        try:
            if getattr(self, "_stars", None):
                self._paint_stars(c)
                c.tag_raise("fg")                                     # keep planet/satellite above the stars
                c.tag_lower("bg")                                     # keep sky/nebula behind the stars
        except Exception:
            pass
        self.after(140, self._twinkle)

    def _pulse_dot(self):
        # Breathing halo behind the sharing status dot; color depends on host state.
        import math
        self._pulse_t = getattr(self, "_pulse_t", 0.0) + 0.08
        f = (math.sin(self._pulse_t) + 1) / 2
        try:
            if self.host_state == "ready":
                self.dot_halo.configure(fg_color=_blend(GREEN_HALO, GREEN, f))
            elif self.host_state == "controlled":
                self.dot_halo.configure(fg_color=_blend(ACCENT_DIM, ACCENT, f))
            else:
                self.dot_halo.configure(fg_color=BG_LAYER)
        except Exception:
            pass                       # halo not built yet / being rebuilt
        self.after(60, self._pulse_dot)

    # ---- share (host) toggle --------------------------------------------
    def _toggle_share(self):
        self._stop_share() if self.host is not None else self._start_share()

    def _start_share(self):
        min_scale = SHARP_MIN_SCALE if self.settings.get("priority") == "sharp" else SMOOTH_MIN_SCALE
        self.home_status.configure(text="")          # clear any earlier error
        try:
            self.host = HostBackend(self.host_events,
                                    quality=int(self.settings.get("quality", DEFAULT_QUALITY)),
                                    fps=int(self.settings.get("fps", DEFAULT_FPS)),
                                    min_scale=min_scale)
            self.host.start()
        except Exception as e:
            self.host = None
            self._apply_host_state("error", f"Couldn't start sharing: {e}")
            return
        self.sharing = True
        self.share_btn.configure(text="Turn off", border_color=RED, text_color=RED,
                                 hover_color=RED_DIM)
        self._apply_host_state("starting")            # amber until the thread reports "ready"

    def _stop_share(self):
        if self.host:
            self.host.stop()
            self.host = None
        self.sharing = False
        self.share_btn.configure(text="Turn on", border_color=BORDER, text_color=TEXT,
                                 hover_color=SURF_HOVER)
        self._apply_host_state("off")

    # ---- settings window -------------------------------------------------
    def _save(self, **kw):
        self.settings.update(kw)
        save_settings(self.settings)

    def _set_accent(self, hex_):
        """Apply an accent color live across the whole app and remember it."""
        apply_theme(hex_)
        self._save(accent=hex_)
        self._rebuild_ui()

    def _open_settings(self):
        win = ctk.CTkToplevel(self)
        win.title("Settings")
        win.geometry("500x600")
        win.configure(fg_color=BG)
        win.transient(self)
        win.after(220, lambda: (win.lift(), win.focus_force()))
        try:
            win.iconbitmap(ICON)
        except Exception:
            pass

        head = ctk.CTkFrame(win, fg_color="transparent")
        head.pack(fill="x", padx=26, pady=(22, 0))
        ctk.CTkLabel(head, text="Settings", text_color=TEXT,
                     font=ctk.CTkFont(FONT_UI, 22, weight="bold")).pack(anchor="w")
        self._accent_underline(head, width=52, height=2).pack(anchor="w", pady=(3, 0))

        wrap = ctk.CTkScrollableFrame(win, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=20, pady=(12, 4))

        def card(title, subtitle=None):
            c = ctk.CTkFrame(wrap, corner_radius=14, fg_color=CARD,
                             border_width=1, border_color=BORDER)
            c.pack(fill="x", pady=8)
            inner = ctk.CTkFrame(c, fg_color="transparent")
            inner.pack(fill="x", padx=18, pady=16)
            ctk.CTkLabel(inner, text=title.upper(), text_color=ACCENT,
                         font=ctk.CTkFont(FONT_UI, 11, weight="bold")).pack(anchor="w")
            if subtitle:
                ctk.CTkLabel(inner, text=subtitle, text_color=MUTED,
                             font=ctk.CTkFont(FONT_UI, 11)).pack(anchor="w", pady=(1, 8))
            return inner

        # ---------- Accent color ----------
        acc = card("Accent color", "Pick a color for the whole app.")
        swrow = ctk.CTkFrame(acc, fg_color="transparent")
        swrow.pack(anchor="w", pady=(4, 6))
        self._swatches = []

        def refresh_swatches():
            cur = self.settings.get("accent", "#22D3EE").lower()
            for hexv, dot in self._swatches:
                sel = hexv.lower() == cur
                dot.configure(border_width=3 if sel else 0,
                              border_color=TEXT if sel else hexv)

        for i, (nm, hexv) in enumerate(ACCENT_SWATCHES):
            dot = ctk.CTkButton(swrow, text="", width=34, height=34, corner_radius=17,
                                fg_color=hexv, hover_color=hexv,
                                command=lambda h=hexv: (self._set_accent(h), refresh_swatches()))
            dot.grid(row=i // 4, column=i % 4, padx=6, pady=6)
            self._swatches.append((hexv, dot))
        refresh_swatches()

        hexrow = ctk.CTkFrame(acc, fg_color="transparent")
        hexrow.pack(anchor="w", fill="x", pady=(4, 0))
        hex_entry = ctk.CTkEntry(hexrow, width=130, placeholder_text="#RRGGBB",
                                 fg_color=SURF_INSET, border_color=BORDER)
        hex_entry.pack(side="left")
        hex_entry.insert(0, self.settings.get("accent", "#22D3EE"))

        def apply_hex():
            v = hex_entry.get().strip()
            if not v.startswith("#"):
                v = "#" + v
            try:
                assert len(v) == 7 and int(v[1:], 16) >= 0
                self._set_accent(v)
                refresh_swatches()
            except Exception:
                hex_entry.configure(border_color=RED)
        ctk.CTkButton(hexrow, text="Apply", width=72, fg_color="transparent", border_width=1,
                      border_color=BORDER, hover_color=SURF_HOVER, text_color=TEXT,
                      command=apply_hex).pack(side="left", padx=8)

        # ---------- Appearance ----------
        ap = card("Appearance", "Light or dark, and overall size.")
        appear_var = ctk.StringVar(value=self.settings.get("appearance", "Dark"))
        seg = ctk.CTkSegmentedButton(ap, values=["Dark", "Light", "System"], variable=appear_var,
                                     selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
                                     command=lambda v: (ctk.set_appearance_mode(v),
                                                        self._save(appearance=v)))
        seg.pack(anchor="w", fill="x", pady=(2, 10))

        scale_var = ctk.DoubleVar(value=float(self.settings.get("ui_scale", 1.0)))
        scale_lbl = ctk.CTkLabel(ap, text=f"Interface size · {scale_var.get():.2f}×",
                                 text_color=TEXT_BODY, font=ctk.CTkFont(FONT_UI, 13))
        scale_lbl.pack(anchor="w")

        def on_scale(v):
            scale_lbl.configure(text=f"Interface size · {float(v):.2f}×")
            try:
                ctk.set_widget_scaling(float(v))
            except Exception:
                pass
            self._save(ui_scale=round(float(v), 2))
        ctk.CTkSlider(ap, from_=0.8, to=1.6, number_of_steps=16, variable=scale_var,
                      button_color=ACCENT, button_hover_color=ACCENT_HOVER, progress_color=ACCENT,
                      command=on_scale).pack(anchor="w", fill="x", pady=(2, 4))

        # ---------- Controlling games ----------
        gm = card("Controlling games", "Mouse-look (F8) turn speed.")
        sens_var = ctk.DoubleVar(value=float(self.settings.get("sensitivity", 1.0)))
        sens_lbl = ctk.CTkLabel(gm, text=f"Sensitivity · {sens_var.get():.2f}×",
                                text_color=TEXT_BODY, font=ctk.CTkFont(FONT_UI, 13))
        sens_lbl.pack(anchor="w")
        ctk.CTkSlider(gm, from_=0.2, to=3.0, number_of_steps=28, variable=sens_var,
                      button_color=ACCENT, button_hover_color=ACCENT_HOVER, progress_color=ACCENT,
                      command=lambda v: (sens_lbl.configure(text=f"Sensitivity · {float(v):.2f}×"),
                                         self._save(sensitivity=round(float(v), 2)))).pack(
            anchor="w", fill="x", pady=(2, 4))

        ctk.CTkLabel(wrap, text="Resolution & quality are chosen each time you press Connect.",
                     text_color=MUTED, font=ctk.CTkFont(FONT_UI, 11), wraplength=430,
                     justify="left").pack(anchor="w", pady=(4, 2))

        # ---------- footer ----------
        foot = ctk.CTkFrame(win, fg_color="transparent")
        foot.pack(fill="x", padx=26, pady=(0, 18))

        def do_reset():
            self.settings = dict(DEFAULT_SETTINGS)
            save_settings(self.settings)
            apply_theme(self.settings["accent"])
            ctk.set_appearance_mode(self.settings["appearance"])
            try:
                ctk.set_widget_scaling(self.settings["ui_scale"])
            except Exception:
                pass
            win.destroy()
            self._rebuild_ui()
        ctk.CTkButton(foot, text="Reset to defaults", width=150, fg_color="transparent",
                      border_width=1, border_color=BORDER, hover_color=SURF_HOVER,
                      text_color=TEXT_BODY, command=do_reset).pack(side="left")
        ctk.CTkButton(foot, text="Done", width=110, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      text_color=ON_ACCENT, command=win.destroy).pack(side="right")

    # ---- session page ----------------------------------------------------
    def _pill(self, parent, text, command, width=118, danger=False):
        return ctk.CTkButton(
            parent, text=text, width=width, height=32, corner_radius=16,
            fg_color="transparent", border_width=1,
            border_color=(RED if danger else BORDER),
            text_color=(RED if danger else TEXT_BODY),
            hover_color=(RED_DIM if danger else SURF_HOVER),
            font=ctk.CTkFont(FONT_UI, 13, weight="bold"), command=command)

    def _build_session(self):
        self.session = ctk.CTkFrame(self, fg_color=BG)
        bar = ctk.CTkFrame(self.session, height=52, corner_radius=0, fg_color=SURF_INSET,
                           border_width=0)
        bar.pack(fill="x", side="top")
        ctk.CTkFrame(bar, height=1, fg_color=ACCENT_DIM).pack(fill="x", side="bottom")
        self._pill(bar, "← Disconnect", self._stop_control, width=124, danger=True).pack(
            side="left", padx=(10, 6), pady=9)
        self.pause_btn = self._pill(bar, "Pause input", self._toggle_pause)
        self.pause_btn.pack(side="left", padx=4, pady=9)
        self.game_btn = self._pill(bar, "🎮 Mouse-look: OFF", self._toggle_game, width=168)
        self.game_btn.pack(side="left", padx=4, pady=9)
        self.full_btn = self._pill(bar, "⛶ Fullscreen", self._toggle_fullscreen, width=134)
        self.full_btn.pack(side="left", padx=4, pady=9)
        self.stat_label = ctk.CTkLabel(bar, text="connecting…", text_color=TEXT_BODY,
                                       font=ctk.CTkFont(FONT_MONO, 13))
        self.stat_label.pack(side="right", padx=16)

        import tkinter as tk
        self.canvas = tk.Canvas(self.session, bg="#000000", highlightthickness=0)
        # Hide the LOCAL cursor over the video — the remote PC's real cursor is drawn
        # into the stream, so showing the local one too gives a confusing "two mice".
        self.canvas.configure(cursor="none")
        self.canvas.pack(fill="both", expand=True)
        self._img_id = self.canvas.create_image(0, 0, anchor="nw")
        self._msg_id = self.canvas.create_text(24, 24, anchor="nw", fill="#c7cbd4",
                                               font=("Segoe UI", 15), text="")
        c = self.canvas
        c.bind("<Motion>", self._on_motion)
        # also track motion WHILE a button is held — otherwise Tk only sends <B1-Motion>
        # and you can't look around while mining / dragging (one input at a time)
        for n in (1, 2, 3):
            c.bind(f"<B{n}-Motion>", self._on_motion)
        for n in (1, 2, 3):
            c.bind(f"<Button-{n}>", lambda e, n=n: self._on_button(e, n, True))
            c.bind(f"<ButtonRelease-{n}>", lambda e, n=n: self._on_button(e, n, False))
        c.bind("<MouseWheel>", self._on_wheel)
        c.bind("<KeyPress>", lambda e: self._on_key(e, "press"))
        c.bind("<KeyRelease>", lambda e: self._on_key(e, "release"))
        c.bind("<Enter>", lambda e: c.focus_set())
        # If focus leaves (alt-tab), release every held key on the host so nothing
        # gets "stuck down" (e.g. Shift staying pressed and blocking movement).
        c.bind("<FocusOut>", lambda e: self._release_all_remote())

    def _show_home(self):
        self.session.pack_forget()
        self.home.pack(fill="both", expand=True)

    def _show_session(self):
        self.home.pack_forget()
        self.session.pack(fill="both", expand=True)
        self.canvas.focus_set()

    # ---- control (client) start/stop ------------------------------------
    # Quality presets the user picks before every session.
    CONN_PRESETS = [
        ("smooth", "⚡  Smooth", "720p · fastest, lowest lag",
         {"target_w": 1280, "quality": 78, "fps": 60, "sharp": False}),
        ("balanced", "⚖  Balanced", "1080p · good balance (recommended)",
         {"target_w": 1920, "quality": 88, "fps": 60, "sharp": False}),
        ("sharp", "✦  Sharp", "Native resolution · crispest, needs bandwidth",
         {"target_w": 0, "quality": 92, "fps": 60, "sharp": True}),
    ]

    def _start_control(self):
        if not PEER or not SECRET:
            self.home_status.configure(text="⚠  Not set up yet — run the Setup Wizard.")
            return
        self._open_connect_chooser()

    def _open_connect_chooser(self):
        # Built as a full-window in-app page (not a separate window) so it can
        # never be clipped by display scaling.
        if getattr(self, "_chooser", None) is not None:
            try: self._chooser.destroy()
            except Exception: pass
        ov = ctk.CTkFrame(self, fg_color=BG)
        ov.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._chooser = ov

        card = ctk.CTkFrame(ov, corner_radius=18, fg_color=CARD,
                            border_width=1, border_color=BORDER)
        card.place(relx=0.5, rely=0.5, anchor="center")
        self._corner_brackets(card, color=ACCENT_DIM)
        pad = ctk.CTkFrame(card, fg_color="transparent")
        pad.pack(padx=40, pady=32)

        ctk.CTkLabel(pad, text="Pick quality for this session", text_color=TEXT,
                     font=ctk.CTkFont(FONT_UI, 19, weight="bold")).pack(anchor="w")
        self._accent_underline(pad, width=52, height=2).pack(anchor="w", pady=(3, 2))
        ctk.CTkLabel(pad, text=f"Controlling {PEER}", text_color=MUTED,
                     font=ctk.CTkFont(FONT_UI, 12)).pack(anchor="w", pady=(0, 16))

        last = self.settings.get("conn_preset", "balanced")

        def close_chooser():
            try: ov.destroy()
            except Exception: pass
            self._chooser = None

        def choose(key, prefs):
            self.settings["conn_preset"] = key
            save_settings(self.settings)
            close_chooser()
            self._begin_control(prefs)

        for key, title, desc, prefs in self.CONN_PRESETS:
            hot = (key == last)
            row = ctk.CTkButton(pad, text="", width=380, height=66, corner_radius=12,
                                fg_color=SURF_HOVER if hot else SURF_INSET,
                                hover_color=SURF_HOVER, border_width=1,
                                border_color=ACCENT if hot else BORDER,
                                command=lambda k=key, p=prefs: choose(k, p))
            row.pack(fill="x", pady=6)
            t = ctk.CTkLabel(row, text=title, text_color=(ACCENT if hot else TEXT),
                             font=ctk.CTkFont(FONT_UI, 15, weight="bold"))
            t.place(x=20, y=13)
            d = ctk.CTkLabel(row, text=desc, text_color=TEXT_BODY,
                             font=ctk.CTkFont(FONT_UI, 11))
            d.place(x=20, y=38)
            for lbl in (t, d):
                lbl.bind("<Button-1>", lambda _e, k=key, p=prefs: choose(k, p))
                try: lbl.configure(cursor="hand2")
                except Exception: pass

        ctk.CTkButton(pad, text="Cancel", width=380, height=34, corner_radius=17,
                      fg_color="transparent", border_width=1, border_color=BORDER,
                      hover_color=SURF_HOVER, text_color=TEXT_BODY,
                      command=close_chooser).pack(fill="x", pady=(12, 0))
        ov.bind("<Escape>", lambda _e: close_chooser())
        ov.focus_set()

    def _begin_control(self, prefs):
        self.client = ClientBackend(PEER, self.client_events, prefs=prefs)
        self.client.start()
        self._decoded = None
        self._decoded_counter = self._drawn_counter = 0
        self._decoding = True
        self._decode_thread = threading.Thread(target=self._decode_worker, daemon=True)
        self._decode_thread.start()
        self._show_session()
        self._set_message(f"Connecting to {PEER} ...")
        self.after(10, self._render_loop)

    def _stop_control(self):
        self._decoding = False
        self._decode_thread = None
        if self.client:
            self.client.stop()
            self.client = None
        self.client_paired = False
        if self.game_mode:
            self._toggle_game()
        if self.fullscreen:
            self._toggle_fullscreen()
        self._show_home()

    # ---- input -----------------------------------------------------------
    def _rel(self, x, y):
        vx, vy, vw, vh = self.video_rect
        if vw <= 0 or vh <= 0:
            return None
        rx, ry = (x - vx) / vw, (y - vy) / vh
        if 0.0 <= rx <= 1.0 and 0.0 <= ry <= 1.0:
            return rx, ry
        return None

    def _on_motion(self, e):
        if not (self.client_paired and not self.paused):
            return
        if self.game_mode:
            # Measure movement as the delta between successive positions (works the
            # same for a mouse or a trackpad), and only re-center the pointer when it
            # nears an edge — warping on every event fights a high-rate mouse and jitters.
            x, y = e.x, e.y
            w = max(self.canvas.winfo_width(), 2)
            h = max(self.canvas.winfo_height(), 2)
            if self._lock_last is not None:
                dx, dy = x - self._lock_last[0], y - self._lock_last[1]
                # Ignore huge deltas: those come from events queued BEFORE a
                # re-center warp took effect (measuring old pos vs new center) and
                # would show up in-game as a sudden camera jump.
                if (dx or dy) and abs(dx) < w * 0.5 and abs(dy) < h * 0.5:
                    self._pending_rmove[0] += dx
                    self._pending_rmove[1] += dy
            self._lock_last = (x, y)
            margin = 80
            if x < margin or y < margin or x > w - margin or y > h - margin:
                cx, cy = w // 2, h // 2
                self._lock_last = (cx, cy)
                try:
                    self.canvas.event_generate("<Motion>", warp=True, x=cx, y=cy)
                except Exception:
                    pass
        else:
            rel = self._rel(e.x, e.y)
            if rel:
                self._pending_move = {"type": "move", "x": rel[0], "y": rel[1]}

    def _on_button(self, e, n, pressed):
        self.canvas.focus_set()
        if not (self.client_paired and not self.paused):
            return
        if self.game_mode:                   # pointer is locked in-game; just press/release
            self.client.send({"type": "click", "button": _MOUSE[n], "pressed": pressed,
                              "game": True, "x": 0.5, "y": 0.5})
            return
        rel = self._rel(e.x, e.y)
        if rel:
            self.client.send({"type": "click", "button": _MOUSE[n], "pressed": pressed,
                              "x": rel[0], "y": rel[1]})

    def _on_wheel(self, e):
        if not (self.client_paired and not self.paused):
            return
        if self.game_mode:                   # hotbar scroll without moving the cursor
            self.client.send({"type": "scroll", "dx": 0, "dy": e.delta // 120,
                              "game": True, "x": 0.5, "y": 0.5})
            return
        rel = self._rel(e.x, e.y)
        if rel:
            self.client.send({"type": "scroll", "dx": 0, "dy": e.delta // 120,
                              "x": rel[0], "y": rel[1]})

    def _on_key(self, e, action):
        if e.keysym == "F11":
            return "break"
        if e.keysym == "F8":                 # local hotkey: toggle mouse-look (not sent to game)
            if action == "press":
                self._toggle_game()
            return "break"
        if self.client_paired and not self.paused:
            if e.keysym in _SPECIAL:
                self.client.send({"type": "key", "action": action, "key": _SPECIAL[e.keysym], "special": True})
            elif e.keycode:
                # send the physical key (virtual-key code) so Shift/Ctrl/Alt compose
                # correctly and game movement keeps working while a modifier is held
                self.client.send({"type": "key", "action": action, "vk": int(e.keycode)})
            elif e.char and e.char.isprintable():
                self.client.send({"type": "key", "action": action, "key": e.char, "special": False})
        return "break"

    def _release_all_remote(self):
        if self.client_paired:
            self.client.send({"type": "release_all"})

    def _toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            self.pause_btn.configure(text="Resume input", fg_color=WARNING, text_color=ON_ACCENT,
                                     border_color=WARNING, hover_color=WARNING)
        else:
            self.pause_btn.configure(text="Pause input", fg_color="transparent",
                                     text_color=TEXT_BODY, border_color=BORDER,
                                     hover_color=SURF_HOVER)

    def _toggle_game(self):
        self.game_mode = not self.game_mode
        self._pending_rmove = [0, 0]
        self._lock_last = None
        if self.game_mode:
            self.game_btn.configure(text="🎮 Mouse-look: ON  (F8)", fg_color=ACCENT,
                                    text_color=ON_ACCENT, border_color=ACCENT, hover_color=ACCENT_HOVER)
            self.canvas.focus_set()             # cursor is already hidden over the canvas
            self._recenter_pointer()
        else:
            self.game_btn.configure(text="🎮 Mouse-look: OFF", fg_color="transparent",
                                    text_color=TEXT_BODY, border_color=BORDER, hover_color=SURF_HOVER)

    def _recenter_pointer(self):
        cx = max(self.canvas.winfo_width() // 2, 1)
        cy = max(self.canvas.winfo_height() // 2, 1)
        self._lock_last = (cx, cy)
        try:
            self.canvas.event_generate("<Motion>", warp=True, x=cx, y=cy)
        except Exception:
            pass

    def _toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.attributes("-fullscreen", self.fullscreen)
        if hasattr(self, "full_btn"):
            self.full_btn.configure(text="⛶ Exit fullscreen" if self.fullscreen else "⛶ Fullscreen")

    # ---- rendering -------------------------------------------------------
    def _set_message(self, text):
        self.canvas.itemconfig(self._msg_id, text=text)

    def _decode_worker(self):
        """Decode + resize each incoming JPEG off the UI thread (the heavy part)."""
        last = -1
        while self._decoding:
            client = self.client
            if client is None or not self.client_paired:
                time.sleep(0.005); continue
            c = client.frame_counter
            if c == last:
                time.sleep(0.002); continue
            last = c
            jpeg = client.latest_frame
            if not jpeg:
                continue
            try:
                arr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                if arr is None:
                    continue
                fh, fw = arr.shape[:2]
                cw, ch = self._canvas_wh
                scale = min(cw / fw, ch / fh)
                dw, dh = max(int(fw * scale), 1), max(int(fh * scale), 1)
                # Smooth mode: cap the DRAWN size so the UI blit stays cheap (big fps win).
                # Drawing full 1080p is ~50 ms/frame (~20 fps); ~1440-wide is ~22 ms (~45 fps).
                if self.settings.get("priority") != "sharp" and dw > RENDER_MAX_W_SMOOTH:
                    r = RENDER_MAX_W_SMOOTH / dw
                    dw, dh = RENDER_MAX_W_SMOOTH, max(int(dh * r), 1)
                if (dw, dh) != (fw, fh):
                    interp = cv2.INTER_AREA if dw < fw else cv2.INTER_LINEAR
                    arr = cv2.resize(arr, (dw, dh), interpolation=interp)
                arr = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
                ox, oy = (cw - dw) // 2, (ch - dh) // 2
                self._decoded = (arr, ox, oy, dw, dh, len(jpeg))
                self._decoded_counter += 1
            except Exception:
                continue

    def _render_loop(self):
        if self.client is None:
            return
        # publish the canvas size for the decoder thread (winfo_* is main-thread only)
        self._canvas_wh = (max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1))
        if self._pending_move is not None:
            self.client.send(self._pending_move)
            self._pending_move = None
        if self._pending_rmove != [0, 0]:
            sens = float(self.settings.get("sensitivity", 1.0))
            dx = int(round(self._pending_rmove[0] * sens))
            dy = int(round(self._pending_rmove[1] * sens))
            if dx or dy:
                self.client.send({"type": "rmove", "dx": dx, "dy": dy})
            self._pending_rmove = [0, 0]
        if self.client_paired and self._decoded_counter != self._drawn_counter:
            self._drawn_counter = self._decoded_counter
            self._draw(self._decoded)
        elif not self.client_paired:
            self._set_message(f"Connecting to {PEER} ...  (is the app open on that PC?)")
        self.after(10, self._render_loop)

    def _draw(self, decoded):
        # Runs on the UI thread: only the cheap blit of an already-decoded frame.
        if not decoded:
            return
        arr, ox, oy, dw, dh, nbytes = decoded
        self.video_rect = (ox, oy, dw, dh)
        pil = Image.fromarray(arr)
        # Reuse the same Tk image and repaint its pixels in place — this is much faster
        # than building a new PhotoImage every frame (which alone caps ~1080p near 20 fps).
        # Only (re)create it when the drawn size changes (e.g. the window was resized).
        if self._photo is not None and (self._photo.width(), self._photo.height()) == (dw, dh):
            self._photo.paste(pil)
        else:
            self._photo = ImageTk.PhotoImage(pil)
        self.canvas.itemconfig(self._msg_id, text="")
        self.canvas.coords(self._img_id, ox, oy)
        self.canvas.itemconfig(self._img_id, image=self._photo)
        now = time.time()
        self._fps.append(now)
        self._bytes.append((now, nbytes))

    def _fps_val(self):
        if len(self._fps) < 2:
            return 0.0
        span = self._fps[-1] - self._fps[0]
        return (len(self._fps) - 1) / span if span > 0 else 0.0

    def _kbps(self):
        now = time.time()
        return sum(s for t, s in self._bytes if now - t <= 1.0) / 1024.0

    # ---- event pump ------------------------------------------------------
    def _apply_host_state(self, state, info=None):
        self.host_state = state
        if state == "ready":
            self.dot.configure(text_color=GREEN)
            self.share_label.configure(text="Sharing on — ready", text_color=TEXT_BODY)
            self._show_kick(False)
        elif state == "controlled":
            self.dot.configure(text_color=ACCENT)
            self.share_label.configure(text=f"Controlled by {info}" if info else "Being controlled now",
                                       text_color=ACCENT)
            self._show_kick(self.sharing)      # let the user boot the controller
        elif state == "starting":
            self.dot.configure(text_color=WARNING)
            self.share_label.configure(text="Starting…", text_color=WARNING)
            self._show_kick(False)
        elif state == "error":
            self.sharing = False
            self.host = None
            self.dot.configure(text_color=RED)
            self.share_label.configure(text="Couldn't start", text_color=RED)
            self.share_btn.configure(text="Turn on", border_color=BORDER,
                                     text_color=TEXT, hover_color=SURF_HOVER)
            if info:
                self.home_status.configure(text=f"⚠  {info}")
            self._show_kick(False)
        else:
            self.dot.configure(text_color="#41506a")
            self.share_label.configure(text="Sharing off", text_color=MUTED)
            self._show_kick(False)

    def _show_kick(self, show):
        if not hasattr(self, "kick_btn"):
            return
        if show:
            self.kick_btn.pack(side="right", padx=(0, 8))
        else:
            self.kick_btn.pack_forget()

    def _kick_controller(self):
        if self.host:
            self.host.kick()

    def _poll(self):
        try:
            while True:
                m = self.host_events.get_nowait()
                if m[0] == "host":
                    self._apply_host_state(m[1], m[2] if len(m) > 2 else None)
        except queue.Empty:
            pass
        try:
            while True:
                m = self.client_events.get_nowait()
                k = m[0]
                if k == "paired":
                    self.client_paired = m[1]
                elif k == "status":
                    self.stat_label.configure(text=m[1])
                elif k == "rtt":
                    self.rtt = m[1]
                elif k == "stat":
                    self.q, self.scale = m[1], m[2]
                elif k == "fatal":
                    self.home_status.configure(text=f"⚠  {m[1]}")
                    self._stop_control()
        except queue.Empty:
            pass
        if self.client_paired:
            rtt = "-- ms" if self.rtt is None else f"{self.rtt:.0f} ms"
            mbps = self._kbps() / 1024.0
            self.stat_label.configure(
                text=f"{self._fps_val():.0f} fps · {mbps:.1f} MB/s · {rtt} · q{self.q} {self.scale}x",
                text_color=self._rtt_color())
        self.after(120, self._poll)

    def _rtt_color(self):
        if self.rtt is None:
            return TEXT_BODY
        if self.rtt <= 40:
            return GREEN
        if self.rtt <= 100:
            return WARNING
        return RED

    def _on_close(self):
        if self.client: self.client.stop()
        if self.host: self.host.stop()
        self.destroy()


def main():
    try:
        import updater
        if updater.check_and_update():
            return
    except Exception:
        pass
    RemoteDesktopApp().mainloop()


if __name__ == "__main__":
    main()
