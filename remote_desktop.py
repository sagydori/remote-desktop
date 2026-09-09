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

try:
    import config
except Exception:
    config = type("cfg", (), {})()          # no config.py yet (before Setup Wizard runs)
NAME = getattr(config, "NAME", "") or "this PC"
PEER = getattr(config, "PEER", "") or getattr(config, "HOST", "")
PORT = getattr(config, "PORT", 8765)
SECRET = getattr(config, "SECRET", "")

HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "icon.ico")

# capture / quality
MIN_QUALITY, MIN_SCALE = 20, 0.40
SCALE = 0.75
DIFF_THRESHOLD = 1.2
KEYFRAME_EVERY = 2.0
USE_BETTERCAM = True
DEFAULT_QUALITY = 55
DEFAULT_FPS = 30

# palette
BG = "#12141a"
CARD = "#1b1e26"
ACCENT = "#3b82f6"
ACCENT_HOVER = "#2f6ad0"
GREEN = "#16a34a"
RED = "#b91c1c"
MUTED = "#8b93a1"

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
        elif t == "click":
            self.mouse.position = self._abs(e["x"], e["y"])
            b = self._BUTTONS.get(e.get("button"), Button.left)
            if e.get("pressed"):
                self.mouse.press(b); self._btns.add(b)
            else:
                self.mouse.release(b); self._btns.discard(b)
        elif t == "scroll":
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
    def __init__(self, quality):
        self.max_quality = quality
        self.quality = quality
        self.scale = SCALE
        self._ema = 0.0

    def note_send_time(self, dt, interval):
        self._ema = 0.6 * self._ema + 0.4 * dt
        if self._ema > interval * 0.70:
            if self.quality > MIN_QUALITY:
                self.quality = max(MIN_QUALITY, self.quality - 5)
            elif self.scale > MIN_SCALE:
                self.scale = round(max(MIN_SCALE, self.scale - 0.1), 2)
        elif self._ema < interval * 0.25:
            if self.scale < SCALE:
                self.scale = round(min(SCALE, self.scale + 0.1), 2)
            elif self.quality < self.max_quality:
                self.quality = min(self.max_quality, self.quality + 5)


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
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
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
    def __init__(self, events, monitor_index=1, quality=DEFAULT_QUALITY, fps=DEFAULT_FPS):
        self.events = events
        self.monitor_index = monitor_index
        self.quality = quality
        self.interval = 1.0 / max(1, fps)
        self.loop = None
        self._async_stop = None
        self._busy = False

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
                                        ping_interval=20, ping_timeout=20):
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
        if self._busy:
            await ws.send(json.dumps({"type": "busy"})); await ws.close(); return
        self._busy = True
        self._emit("host", "controlled")
        controller = InputController(*monitor_geometry(self.monitor_index))
        encoder = AdaptiveEncoder(self.quality)
        paired = asyncio.Event(); paired.set()
        try:
            tasks = [
                asyncio.create_task(self._read(ws, controller)),
                asyncio.create_task(self._stream(ws, self._capture, encoder, paired)),
                asyncio.create_task(self._async_stop.wait()),
            ]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending: t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            controller.release_all()
            self._busy = False
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
        while True:
            await paired.wait()
            start = loop.time()
            force = (start - last_key) >= KEYFRAME_EVERY
            jpeg = await capture.grab(loop, encoder.quality, encoder.scale, force)
            if jpeg is not None:
                t0 = loop.time()
                await ws.send(jpeg)
                encoder.note_send_time(loop.time() - t0, self.interval)
                last_key = start
            if start - last_stat >= 1.0:
                await ws.send(json.dumps({"type": "stat", "q": encoder.quality, "scale": encoder.scale}))
                last_stat = start
            elapsed = loop.time() - start
            if elapsed < self.interval:
                await asyncio.sleep(self.interval - elapsed)


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
        while not self._async_stop.is_set():
            try:
                self._emit("status", f"Connecting to {PEER} ...")
                async with websockets.connect(self.uri, max_size=None,
                                              ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"type": "auth", "token": SECRET}))
                    reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if not reply.get("ok", False):
                        raise ConnectionError("wrong password")
                    self.paired = True
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
            except ConnectionError as e:
                self._emit("fatal", str(e)); return
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
                self.paired = False
                self._emit("paired", False)
                if self._async_stop.is_set():
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
                elif data.get("type") == "busy":
                    raise ConnectionError("that PC is already being controlled by someone")

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
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("Remote Desktop")
        self.geometry("1040x680")
        self.minsize(820, 560)
        self.configure(fg_color=BG)
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
        self.video_rect = (0, 0, 1, 1)
        self._last_counter = -1
        self._photo = None
        self._pending_move = None
        self._fps = deque(maxlen=60)
        self._bytes = deque(maxlen=120)
        self.rtt = None
        self.q = self.scale = None

        self._build_home()
        self._build_session()
        self._show_home()

        self.bind("<F11>", lambda e: self._toggle_fullscreen())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll)
        self.after(400, self._reassert_icon)   # CTk can reset the icon after init

    def _reassert_icon(self):
        try:
            self.iconbitmap(ICON)
        except Exception:
            pass

    # ---- home page -------------------------------------------------------
    def _build_home(self):
        self.home = ctk.CTkFrame(self, fg_color=BG)

        header = ctk.CTkFrame(self.home, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(20, 0))
        if self._icon_img is not None:
            ctk.CTkLabel(header, image=self._icon_img, text="").pack(side="left")
        ctk.CTkLabel(header, text="  Remote Desktop",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")
        ctk.CTkLabel(header, text=f"This PC:  {NAME}", text_color=MUTED,
                     font=ctk.CTkFont(size=13)).pack(side="right")

        card = ctk.CTkFrame(self.home, corner_radius=22, fg_color=CARD)
        card.place(relx=0.5, rely=0.52, anchor="center")
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(padx=56, pady=44)

        # Section 1 — control the other PC
        ctk.CTkLabel(inner, text="CONTROL YOUR OTHER PC", text_color="#7c8698",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(inner, text="Take over the other computer's screen.",
                     text_color="#c7cbd4", font=ctk.CTkFont(size=13)).pack(anchor="w", pady=(2, 12))
        self.control_btn = ctk.CTkButton(
            inner, text=f"🖥   Control  {PEER or 'other PC'}", height=56, width=400,
            corner_radius=12, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=17, weight="bold"), command=self._start_control)
        self.control_btn.pack(fill="x")

        ctk.CTkFrame(inner, height=1, fg_color="#2b2f3a").pack(fill="x", pady=24)

        # Section 2 — allow this PC to be controlled
        ctk.CTkLabel(inner, text="LET YOUR OTHER PC CONTROL THIS ONE", text_color="#7c8698",
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(inner, text="Turn on, then leave the app open, to allow access.",
                     text_color="#c7cbd4", font=ctk.CTkFont(size=13)).pack(anchor="w", pady=(2, 12))
        self.share_btn = ctk.CTkButton(
            inner, text="🔓   Allow this PC to be controlled", height=56, width=400,
            corner_radius=12, fg_color="#374151", hover_color="#2c333f",
            font=ctk.CTkFont(size=16, weight="bold"), command=self._toggle_share)
        self.share_btn.pack(fill="x")
        srow = ctk.CTkFrame(inner, fg_color="transparent")
        srow.pack(anchor="w", pady=(12, 0))
        self.dot = ctk.CTkLabel(srow, text="●", text_color="#6b7280", font=ctk.CTkFont(size=16))
        self.dot.pack(side="left", padx=(0, 8))
        self.share_label = ctk.CTkLabel(srow, text="Off — not shareable", text_color=MUTED,
                                        font=ctk.CTkFont(size=13))
        self.share_label.pack(side="left")

        self.home_status = ctk.CTkLabel(inner, text="", text_color="#f59e0b",
                                        font=ctk.CTkFont(size=13), wraplength=400, justify="left")
        self.home_status.pack(anchor="w", pady=(16, 0))

        if not PEER or not SECRET:
            self.control_btn.configure(state="disabled", text="🖥   Run Setup first")
            self.home_status.configure(text="⚠  Not set up yet — run the Setup Wizard (INSTALL.bat).")
        elif NAME and NAME != "this PC" and PEER.strip().lower() == NAME.strip().lower():
            self.home_status.configure(
                text="⚠  The other-PC name is the same as this PC. Re-run Setup and enter the "
                     "OTHER computer's Tailscale name.")

    # ---- share (host) toggle --------------------------------------------
    def _toggle_share(self):
        self._stop_share() if self.host is not None else self._start_share()

    def _start_share(self):
        self.host = HostBackend(self.host_events)
        self.host.start()
        self.sharing = True
        self.share_btn.configure(text="🔒   Stop allowing control", fg_color=RED,
                                 hover_color="#991b1b")

    def _stop_share(self):
        if self.host:
            self.host.stop()
            self.host = None
        self.sharing = False
        self.share_btn.configure(text="🔓   Allow this PC to be controlled",
                                 fg_color="#374151", hover_color="#2c333f")
        self._apply_host_state("off")

    # ---- session page ----------------------------------------------------
    def _build_session(self):
        self.session = ctk.CTkFrame(self, fg_color=BG)
        bar = ctk.CTkFrame(self.session, height=50, corner_radius=0, fg_color="#0f1116")
        bar.pack(fill="x", side="top")
        ctk.CTkButton(bar, text="← Disconnect", width=120, fg_color=RED, hover_color="#991b1b",
                      command=self._stop_control).pack(side="left", padx=8, pady=8)
        self.pause_btn = ctk.CTkButton(bar, text="Pause input", width=110, command=self._toggle_pause)
        self.pause_btn.pack(side="left", padx=4, pady=8)
        ctk.CTkButton(bar, text="Fullscreen", width=100,
                      command=self._toggle_fullscreen).pack(side="left", padx=4, pady=8)
        self.stat_label = ctk.CTkLabel(bar, text="connecting...", text_color=MUTED)
        self.stat_label.pack(side="right", padx=14)

        import tkinter as tk
        self.canvas = tk.Canvas(self.session, bg="#000000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._img_id = self.canvas.create_image(0, 0, anchor="nw")
        self._msg_id = self.canvas.create_text(24, 24, anchor="nw", fill="#c7cbd4",
                                               font=("Segoe UI", 15), text="")
        c = self.canvas
        c.bind("<Motion>", self._on_motion)
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
        self._show_session()
        self._set_message(f"Connecting to {PEER} ...")
        self.after(16, self._render_loop)

    def _stop_control(self):
        if self.client:
            self.client.stop()
            self.client = None
        self.client_paired = False
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
        if self.client_paired and not self.paused:
            rel = self._rel(e.x, e.y)
            if rel:
                self._pending_move = {"type": "move", "x": rel[0], "y": rel[1]}

    def _on_button(self, e, n, pressed):
        self.canvas.focus_set()
        if self.client_paired and not self.paused:
            rel = self._rel(e.x, e.y)
            if rel:
                self.client.send({"type": "click", "button": _MOUSE[n], "pressed": pressed,
                                  "x": rel[0], "y": rel[1]})

    def _on_wheel(self, e):
        if self.client_paired and not self.paused:
            rel = self._rel(e.x, e.y)
            if rel:
                self.client.send({"type": "scroll", "dx": 0, "dy": e.delta // 120,
                                  "x": rel[0], "y": rel[1]})

    def _on_key(self, e, action):
        if e.keysym == "F11":
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
        self.pause_btn.configure(text="Resume input" if self.paused else "Pause input",
                                 fg_color="#d97706" if self.paused else ["#3a7ebf", "#1f538d"])

    def _toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.attributes("-fullscreen", self.fullscreen)

    # ---- rendering -------------------------------------------------------
    def _set_message(self, text):
        self.canvas.itemconfig(self._msg_id, text=text)

    def _render_loop(self):
        if self.client is None:
            return
        if self._pending_move is not None:
            self.client.send(self._pending_move)
            self._pending_move = None
        if self.client_paired and self.client.frame_counter != self._last_counter:
            self._last_counter = self.client.frame_counter
            frame = self.client.latest_frame
            if frame:
                self._draw(frame)
                self._fps.append(time.time())
                self._bytes.append((time.time(), len(frame)))
        elif not self.client_paired:
            self._set_message(f"Connecting to {PEER} ...  (is the app open on that PC?)")
        self.after(16, self._render_loop)

    def _draw(self, jpeg):
        arr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return
        arr = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
        fh, fw = arr.shape[:2]
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        scale = min(cw / fw, ch / fh)
        dw, dh = max(int(fw * scale), 1), max(int(fh * scale), 1)
        if (dw, dh) != (fw, fh):
            arr = cv2.resize(arr, (dw, dh), interpolation=cv2.INTER_LINEAR)
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        self.video_rect = (ox, oy, dw, dh)
        self._photo = ImageTk.PhotoImage(Image.fromarray(arr))
        self.canvas.itemconfig(self._msg_id, text="")
        self.canvas.coords(self._img_id, ox, oy)
        self.canvas.itemconfig(self._img_id, image=self._photo)

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
            self.share_label.configure(text="On — your other PC can connect")
        elif state == "controlled":
            self.dot.configure(text_color=ACCENT)
            self.share_label.configure(text="Your other PC is controlling this one now")
        else:
            self.dot.configure(text_color="#6b7280")
            self.share_label.configure(text="Off — not shareable")

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
            rtt = "--" if self.rtt is None else f"{self.rtt:.0f}ms"
            self.stat_label.configure(
                text=f"{self._fps_val():.0f} fps   {self._kbps():.0f} KB/s   RTT {rtt}   q{self.q} {self.scale}x")
        self.after(120, self._poll)

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
