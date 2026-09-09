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
from pynput.keyboard import Controller as KeyboardController, Key
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
else:
    def _move_relative(dx, dy):
        pass  # patched to pynput's relative move per-controller on non-Windows

# capture / quality
MIN_QUALITY = 60                      # quality floor when shedding load
SCALE = 1.0                           # capture at native resolution (ceiling)
SMOOTH_MIN_SCALE = 0.5                # "Smoothest": may drop to half-res to hold the frame rate
SHARP_MIN_SCALE = 1.0                 # "Sharpest": never downscale (fps may suffer on big screens)
BIG_FRAME_PIXELS = 2_100_000          # above ~1080p, use faster 4:2:0 chroma instead of 4:4:4
SMOOTH_TARGET_W = 1920                 # "Smoothest" starts capped near this width on big screens
DIFF_THRESHOLD = 1.2
KEYFRAME_EVERY = 2.0
USE_BETTERCAM = True
DEFAULT_QUALITY = 90                  # high quality; edges/text stay crisp
DEFAULT_FPS = 60                      # smooth 60 fps

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
            key = getattr(Key, e["key"], None) if e.get("special") else e.get("key")
            if key is None:
                return
            try:
                if e.get("action") == "press":
                    self.keyboard.press(key); self._keys.add(key)
                else:
                    self.keyboard.release(key); self._keys.discard(key)
            except Exception:
                pass

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
        if self._ema > interval * 0.9:          # behind -> lighten the load
            if self.scale > self.min_scale:
                self.scale = round(max(self.min_scale, self.scale - 0.1), 2)
            elif self.quality > MIN_QUALITY:
                self.quality = max(MIN_QUALITY, self.quality - 4)
        elif self._ema < interval * 0.5:        # headroom -> improve again
            if self.quality < self.max_quality:
                self.quality = min(self.max_quality, self.quality + 3)
            elif self.scale < self.max_scale:
                self.scale = round(min(self.max_scale, self.scale + 0.05), 2)


def monitor_geometry(monitor_index):
    with mss.mss() as sct:
        m = sct.monitors[monitor_index]
    return m["left"], m["top"], m["width"], m["height"]


class ScreenGrabber:
    """bettercam (Desktop Duplication, captures games) with mss fallback. One thread only."""
    def __init__(self, monitor_index):
        self.backend = "mss"
        self.cam = None
        self._sct = None
        self._prev = None
        self._last_full = None
        if USE_BETTERCAM:
            try:
                import bettercam
                self.cam = bettercam.create(output_idx=max(0, monitor_index - 1), output_color="BGR")
                self.cam.grab()
                self.backend = "bettercam"
            except Exception:
                self.cam = None
        if self.cam is None:
            self._sct = mss.mss()
            self._monitor = self._sct.monitors[monitor_index]

    def grab_jpeg(self, quality, scale, force):
        if self.cam is not None:
            frame = self.cam.grab()
            if frame is None:
                if force and self._last_full is not None:
                    frame = self._last_full
                else:
                    return None
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
            bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
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
        try:
            if self.cam is not None: self.cam.release()
        except Exception: pass
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

    def start(self):
        threading.Thread(target=self._thread_main, daemon=True).start()

    def stop(self):
        if self.loop and self._async_stop:
            self.loop.call_soon_threadsafe(self._async_stop.set)

    def _emit(self, *m): self.events.put(m)

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._run())
        except Exception as e:
            self._emit("log", f"Share error: {e}")
        finally:
            self._emit("host", "offline")

    async def _run(self):
        self._async_stop = asyncio.Event()
        self._capture = Capture(self.monitor_index, self._emit)
        try:
            async with websockets.serve(self._handle, "0.0.0.0", PORT, max_size=None,
                                        ping_interval=10, ping_timeout=10):
                self._emit("host", "ready")
                await self._async_stop.wait()
        except OSError as e:
            self._emit("log", f"Could not listen on port {PORT}: {e}")
        finally:
            self._capture.close()

    async def _auth(self, ws):
        try:
            first = await asyncio.wait_for(ws.recv(), timeout=10)
            ok = isinstance(first, str) and json.loads(first).get("token") == SECRET
        except Exception:
            ok = False
        await ws.send(json.dumps({"type": "auth", "ok": ok}))
        return ok

    async def _handle(self, ws):
        if not await self._auth(ws):
            await ws.close(); return
        # Take over any existing session instead of rejecting as "busy": a reconnect
        # from the same user should replace a stale/zombie session, not be blocked.
        my_stop = asyncio.Event()
        prev, self._session = self._session, my_stop
        if prev is not None:
            prev.set()
        self._emit("host", "controlled")
        geo = monitor_geometry(self.monitor_index)
        controller = InputController(*geo)
        mon_w = max(geo[2], 1)
        # "Smoothest" starts capped near 1080p on big screens so it's fluid from the
        # first frame instead of lagging while it adapts down; "Sharpest" stays native.
        if self.min_scale < 1.0 and mon_w > SMOOTH_TARGET_W:
            cap = round(SMOOTH_TARGET_W / mon_w, 2)
            encoder = AdaptiveEncoder(self.quality, min_scale=self.min_scale,
                                      start_scale=cap, max_scale=cap)
        else:
            encoder = AdaptiveEncoder(self.quality, min_scale=self.min_scale)
        paired = asyncio.Event(); paired.set()
        try:
            tasks = [
                asyncio.create_task(self._read(ws, controller)),
                asyncio.create_task(self._stream(ws, self._capture, encoder, paired)),
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
            # If WE turned sharing off, tell the controller before the socket closes.
            if self._async_stop.is_set():
                try:
                    await ws.send(json.dumps({"type": "bye",
                        "reason": f"{NAME} stopped allowing remote control."}))
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

    async def _stream(self, ws, capture, encoder, paired):
        loop = asyncio.get_running_loop()
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
                encoder.note_frame_time(loop.time() - start, self.interval)
                if start - last_stat >= 1.0:
                    await ws.send(json.dumps({"type": "stat", "q": encoder.quality,
                                              "scale": encoder.scale}))
                    last_stat = start
                elapsed = loop.time() - start
                if elapsed < self.interval:
                    await asyncio.sleep(self.interval - elapsed)
        finally:
            pending.cancel()


# ===========================================================================
#  CLIENT side: connect out to the OTHER PC to control it
# ===========================================================================
class ClientBackend:
    def __init__(self, peer, events):
        self.uri = f"ws://{peer}:{PORT}"
        self.events = events
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
                    await ws.send(json.dumps({"type": "auth", "token": SECRET}))
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
        ctk.set_appearance_mode(self.settings.get("appearance", "Dark"))
        try:
            ctk.set_default_color_theme(self.settings.get("theme", "blue"))
        except Exception:
            ctk.set_default_color_theme("blue")
        try:
            ctk.set_widget_scaling(float(self.settings.get("ui_scale", 1.0)))
        except Exception:
            pass
        self.title("doris pccontrol")
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

        # faint dot-grid texture behind everything
        import tkinter as tk
        self._grid_canvas = tk.Canvas(self.home, bg=BG, highlightthickness=0, bd=0)
        self._grid_canvas.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._grid_canvas.bind("<Configure>", self._draw_grid)

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
        ctk.CTkLabel(header, text=f"This PC · {NAME}    ", text_color=MUTED,
                     font=ctk.CTkFont(FONT_UI, 13)).pack(side="right")

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
        self._pulse_dot()

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

    def _draw_grid(self, event):
        c = self._grid_canvas
        c.delete("grid")
        step = 34
        for x in range(0, event.width, step):
            for y in range(0, event.height, step):
                c.create_oval(x, y, x + 2, y + 2, fill="#141c28", outline="", tags="grid")
        c.lower("grid")

    def _pulse_dot(self):
        # Breathing halo behind the sharing status dot; color depends on host state.
        self._pulse_t = getattr(self, "_pulse_t", 0.0) + 0.08
        import math
        f = (math.sin(self._pulse_t) + 1) / 2
        if self.host_state == "ready":
            self.dot_halo.configure(fg_color=_blend(GREEN_HALO, GREEN, f))
        elif self.host_state == "controlled":
            self.dot_halo.configure(fg_color=_blend(ACCENT_DIM, ACCENT, f))
        else:
            self.dot_halo.configure(fg_color=BG_LAYER)
        self.after(60, self._pulse_dot)

    # ---- share (host) toggle --------------------------------------------
    def _toggle_share(self):
        self._stop_share() if self.host is not None else self._start_share()

    def _start_share(self):
        min_scale = SHARP_MIN_SCALE if self.settings.get("priority") == "sharp" else SMOOTH_MIN_SCALE
        self.host = HostBackend(self.host_events,
                                quality=int(self.settings.get("quality", DEFAULT_QUALITY)),
                                fps=int(self.settings.get("fps", DEFAULT_FPS)),
                                min_scale=min_scale)
        self.host.start()
        self.sharing = True
        self.share_btn.configure(text="Turn off", border_color=RED, text_color=RED,
                                 hover_color=RED_DIM)

    def _stop_share(self):
        if self.host:
            self.host.stop()
            self.host = None
        self.sharing = False
        self.share_btn.configure(text="Turn on", border_color=BORDER, text_color=TEXT,
                                 hover_color=SURF_HOVER)
        self._apply_host_state("off")

    # ---- settings window -------------------------------------------------
    def _open_settings(self):
        win = ctk.CTkToplevel(self)
        win.title("Settings")
        win.geometry("460x620")
        win.configure(fg_color=BG)
        win.transient(self)
        win.after(250, lambda: (win.lift(), win.focus_force()))
        try:
            win.iconbitmap(ICON)
        except Exception:
            pass

        wrap = ctk.CTkScrollableFrame(win, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=22, pady=18)

        ctk.CTkLabel(wrap, text="Settings", font=ctk.CTkFont(size=20, weight="bold")).pack(
            anchor="w", pady=(0, 4))
        ctk.CTkLabel(wrap, text="Personalize the app and how it streams.",
                     text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", pady=(0, 14))

        def section(title):
            ctk.CTkLabel(wrap, text=title.upper(), text_color="#7c8698",
                         font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", pady=(16, 4))

        # --- Appearance ---
        section("Appearance")
        appear_var = ctk.StringVar(value=self.settings.get("appearance", "Dark"))
        ctk.CTkLabel(wrap, text="Theme mode", font=ctk.CTkFont(size=13)).pack(anchor="w")
        ctk.CTkOptionMenu(wrap, values=["Dark", "Light", "System"], variable=appear_var,
                          command=lambda v: ctk.set_appearance_mode(v)).pack(anchor="w", pady=(2, 8))

        color_var = ctk.StringVar(value=self.settings.get("theme", "blue"))
        ctk.CTkLabel(wrap, text="Accent color  (applies after restart)",
                     font=ctk.CTkFont(size=13)).pack(anchor="w")
        ctk.CTkOptionMenu(wrap, values=["blue", "green", "dark-blue"],
                          variable=color_var).pack(anchor="w", pady=(2, 8))

        scale_var = ctk.DoubleVar(value=float(self.settings.get("ui_scale", 1.0)))
        scale_lbl = ctk.CTkLabel(wrap, text=f"Interface size:  {scale_var.get():.2f}x",
                                 font=ctk.CTkFont(size=13))
        scale_lbl.pack(anchor="w")

        def on_scale(v):
            scale_lbl.configure(text=f"Interface size:  {float(v):.2f}x")
            try:
                ctk.set_widget_scaling(float(v))
            except Exception:
                pass
        ctk.CTkSlider(wrap, from_=0.8, to=1.6, number_of_steps=16, variable=scale_var,
                      command=on_scale).pack(anchor="w", fill="x", pady=(2, 8))

        # --- Controlling games ---
        section("Controlling games")
        sens_var = ctk.DoubleVar(value=float(self.settings.get("sensitivity", 1.0)))
        sens_lbl = ctk.CTkLabel(wrap, text=f"Mouse-look sensitivity:  {sens_var.get():.2f}x",
                                font=ctk.CTkFont(size=13))
        sens_lbl.pack(anchor="w")
        ctk.CTkSlider(wrap, from_=0.2, to=3.0, number_of_steps=28, variable=sens_var,
                      command=lambda v: sens_lbl.configure(
                          text=f"Mouse-look sensitivity:  {float(v):.2f}x")).pack(
            anchor="w", fill="x", pady=(2, 8))

        # --- Streaming (when this PC is shared) ---
        section("Streaming  (when this PC is controlled)")
        fps_var = ctk.IntVar(value=int(self.settings.get("fps", DEFAULT_FPS)))
        fps_lbl = ctk.CTkLabel(wrap, text=f"Frame rate:  {fps_var.get()} fps",
                               font=ctk.CTkFont(size=13))
        fps_lbl.pack(anchor="w")
        ctk.CTkSlider(wrap, from_=15, to=75, number_of_steps=12, variable=fps_var,
                      command=lambda v: fps_lbl.configure(
                          text=f"Frame rate:  {int(float(v))} fps")).pack(
            anchor="w", fill="x", pady=(2, 8))

        q_var = ctk.IntVar(value=int(self.settings.get("quality", DEFAULT_QUALITY)))
        q_lbl = ctk.CTkLabel(wrap, text=f"Image quality:  {q_var.get()}",
                             font=ctk.CTkFont(size=13))
        q_lbl.pack(anchor="w")
        ctk.CTkSlider(wrap, from_=50, to=100, number_of_steps=50, variable=q_var,
                      command=lambda v: q_lbl.configure(
                          text=f"Image quality:  {int(float(v))}")).pack(
            anchor="w", fill="x", pady=(2, 8))
        _PRIO = {"smooth": "Smoothest — keep the frame rate (recommended)",
                 "sharp": "Sharpest — always full resolution"}
        _PRIO_REV = {v: k for k, v in _PRIO.items()}
        prio_var = ctk.StringVar(value=_PRIO.get(self.settings.get("priority", "smooth"),
                                                 _PRIO["smooth"]))
        ctk.CTkLabel(wrap, text="On big screens (2K/4K)", font=ctk.CTkFont(size=13)).pack(
            anchor="w", pady=(4, 0))
        ctk.CTkOptionMenu(wrap, values=list(_PRIO.values()), variable=prio_var,
                          width=360).pack(anchor="w", pady=(2, 8))

        ctk.CTkLabel(wrap, text="Higher fps / quality look better but use more bandwidth. "
                               "'Smoothest' briefly lowers resolution when a big screen can't keep "
                               "up, so it stays fluid; 'Sharpest' never lowers it, so the frame "
                               "rate may drop. Streaming changes apply next time sharing is turned on.",
                     text_color=MUTED, font=ctk.CTkFont(size=11), wraplength=380,
                     justify="left").pack(anchor="w", pady=(2, 6))

        status = ctk.CTkLabel(wrap, text="", text_color=GREEN, font=ctk.CTkFont(size=12))
        status.pack(anchor="w", pady=(8, 0))

        def do_save():
            self.settings.update({
                "appearance": appear_var.get(),
                "theme": color_var.get(),
                "ui_scale": round(float(scale_var.get()), 2),
                "sensitivity": round(float(sens_var.get()), 2),
                "fps": int(fps_var.get()),
                "quality": int(q_var.get()),
                "priority": _PRIO_REV.get(prio_var.get(), "smooth"),
            })
            save_settings(self.settings)
            status.configure(text="✓ Saved.")

        def do_reset():
            self.settings = dict(DEFAULT_SETTINGS)
            save_settings(self.settings)
            ctk.set_appearance_mode(self.settings["appearance"])
            try:
                ctk.set_widget_scaling(self.settings["ui_scale"])
            except Exception:
                pass
            win.destroy()

        btns = ctk.CTkFrame(wrap, fg_color="transparent")
        btns.pack(fill="x", pady=(16, 0))
        ctk.CTkButton(btns, text="Save", fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=do_save).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btns, text="Save & close", fg_color=GREEN, hover_color="#128a3e",
                      command=lambda: (do_save(), win.after(150, win.destroy))).pack(side="left")
        ctk.CTkButton(btns, text="Reset", fg_color="#374151", hover_color="#2c333f",
                      command=do_reset).pack(side="right")

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

    def _show_home(self):
        self.session.pack_forget()
        self.home.pack(fill="both", expand=True)

    def _show_session(self):
        self.home.pack_forget()
        self.session.pack(fill="both", expand=True)
        self.canvas.focus_set()

    # ---- control (client) start/stop ------------------------------------
    def _start_control(self):
        if not PEER or not SECRET:
            self.home_status.configure(text="⚠  Not set up yet — run the Setup Wizard.")
            return
        self.client = ClientBackend(PEER, self.client_events)
        self.client.start()
        self._decoded = None
        self._decoded_counter = self._drawn_counter = 0
        self._decoding = True
        self._decode_thread = threading.Thread(target=self._decode_worker, daemon=True)
        self._decode_thread.start()
        self._show_session()
        self._set_message(f"Connecting to {PEER} ...")
        self.after(16, self._render_loop)

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
            cx = self.canvas.winfo_width() // 2
            cy = self.canvas.winfo_height() // 2
            dx, dy = e.x - cx, e.y - cy
            if dx == 0 and dy == 0:
                return                       # our own warp-to-center event; ignore
            self._pending_rmove[0] += dx
            self._pending_rmove[1] += dy
            try:                             # snap the pointer back so the trackpad never runs out
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
            elif e.char and e.char.isprintable():
                self.client.send({"type": "key", "action": action, "key": e.char, "special": False})
            elif len(e.keysym) == 1:
                self.client.send({"type": "key", "action": action, "key": e.keysym, "special": False})
        return "break"

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
        if self.game_mode:
            self.game_btn.configure(text="🎮 Mouse-look: ON  (F8)", fg_color=ACCENT,
                                    text_color=ON_ACCENT, border_color=ACCENT, hover_color=ACCENT_HOVER)
            # show a crosshair reticle at the locked center instead of hiding the cursor
            self.canvas.configure(cursor="crosshair")
            self.canvas.focus_set()
            self._recenter_pointer()
        else:
            self.game_btn.configure(text="🎮 Mouse-look: OFF", fg_color="transparent",
                                    text_color=TEXT_BODY, border_color=BORDER, hover_color=SURF_HOVER)
            self.canvas.configure(cursor="")

    def _recenter_pointer(self):
        cx = max(self.canvas.winfo_width() // 2, 1)
        cy = max(self.canvas.winfo_height() // 2, 1)
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
        self.after(16, self._render_loop)

    def _draw(self, decoded):
        # Runs on the UI thread: only the cheap blit of an already-decoded frame.
        if not decoded:
            return
        arr, ox, oy, dw, dh, nbytes = decoded
        self.video_rect = (ox, oy, dw, dh)
        self._photo = ImageTk.PhotoImage(Image.fromarray(arr))
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
    def _apply_host_state(self, state):
        self.host_state = state
        if state == "ready":
            self.dot.configure(text_color=GREEN)
            self.share_label.configure(text="Sharing on — ready", text_color=TEXT_BODY)
        elif state == "controlled":
            self.dot.configure(text_color=ACCENT)
            self.share_label.configure(text="Being controlled now", text_color=ACCENT)
        else:
            self.dot.configure(text_color="#41506a")
            self.share_label.configure(text="Sharing off", text_color=MUTED)

    def _poll(self):
        try:
            while True:
                m = self.host_events.get_nowait()
                if m[0] == "host":
                    self._apply_host_state(m[1])
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
