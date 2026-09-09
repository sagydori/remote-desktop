"""
updater.py  —  Silent auto-update from GitHub, with a progress window.

remote_desktop.py calls check_and_update() at launch. It asks GitHub for the
latest commit on the branch (via the API, which is never cached), and if that
commit differs from the one this PC last applied it downloads that exact commit
as a zip — showing a progress window with %, size and estimated time — then
overwrites the app files (never config.py / settings.json), records the commit,
and relaunches the fresh copy.

Using the commit id (not the CDN-cached VERSION file) means an update is picked
up the moment it is pushed — no waiting for GitHub's cache to expire.

If the PC is offline or GitHub is unreachable, it does nothing and the app just
starts normally.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

REPO = "sagydori/remote-desktop"
BRANCH = "main"

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(HERE, "VERSION")
STATE_FILE = os.path.join(HERE, ".applied_commit")   # which commit is installed here
PRESERVE = {"config.py", "settings.json", ".applied_commit"}   # never overwritten
ICON = os.path.join(HERE, "icon.ico")


def _configured():
    return REPO and "REPLACE_WITH" not in REPO


def _open(url, timeout, accept=None):
    # No-cache headers so we never get a stale CDN copy.
    headers = {"User-Agent": "pccontrol-updater",
               "Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache"}
    if accept:
        headers["Accept"] = accept
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def local_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "0"


def latest_commit(timeout=8):
    """The newest commit id on the branch, straight from the API (uncached)."""
    url = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
    with _open(url, timeout, accept="application/vnd.github+json") as r:
        return json.loads(r.read().decode("utf-8"))["sha"]


def _applied_commit():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _record_commit(sha):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(sha)
    except Exception:
        pass


# ---------------------------------------------------------------------------
#  Progress window (a tiny splash shown only while an update is downloading)
# ---------------------------------------------------------------------------
class _Splash:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk
        self.root = tk.Tk()
        self.root.title("doris pccontrol — Updating")
        self.root.configure(bg="#12141a")
        self.root.resizable(False, False)
        try:
            self.root.iconbitmap(ICON)
        except Exception:
            pass
        w, h = 440, 170
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass
        tk.Label(self.root, text="Updating doris pccontrol", bg="#12141a", fg="#ffffff",
                 font=("Segoe UI", 14, "bold")).pack(pady=(22, 2))
        self.sub = tk.Label(self.root, text="Checking for the latest version…", bg="#12141a",
                            fg="#8b93a1", font=("Segoe UI", 10))
        self.sub.pack(pady=(0, 6))
        style = ttk.Style(self.root)
        try:
            style.theme_use("default")
            style.configure("pcc.Horizontal.TProgressbar", troughcolor="#1b1e26",
                            background="#3b82f6", bordercolor="#12141a",
                            lightcolor="#3b82f6", darkcolor="#3b82f6", thickness=16)
        except Exception:
            pass
        self.bar = ttk.Progressbar(self.root, length=380, mode="determinate", maximum=100,
                                   style="pcc.Horizontal.TProgressbar")
        self.bar.pack(pady=10)
        self.pct = tk.Label(self.root, text="", bg="#12141a", fg="#c7cbd4",
                            font=("Segoe UI", 10, "bold"))
        self.pct.pack()
        self.start = time.time()
        self._pump()

    def _pump(self):
        try:
            self.root.update()
        except Exception:
            pass

    @staticmethod
    def _eta(secs):
        secs = max(int(secs), 0)
        return f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s"

    def on_download(self, got, total):
        el = max(time.time() - self.start, 1e-6)
        rate = got / el
        mb = got / 1048576
        if total > 0:
            pct = min(got * 100 / total, 100)
            self.bar["mode"] = "determinate"
            self.bar["value"] = pct
            remain = (total - got) / rate if rate > 0 else 0
            self.sub.config(text=f"Downloading update…  {mb:.1f} / {total / 1048576:.1f} MB")
            self.pct.config(text=f"{pct:.0f}%     about {self._eta(remain)} left")
        else:
            self.bar["mode"] = "indeterminate"
            try:
                self.bar.step(6)
            except Exception:
                pass
            self.sub.config(text="Downloading update…")
            self.pct.config(text=f"{mb:.1f} MB")
        self._pump()

    def message(self, text, pct=None):
        if pct is not None:
            self.bar["mode"] = "determinate"
            self.bar["value"] = pct
            self.pct.config(text=f"{pct:.0f}%")
        self.sub.config(text=text)
        self._pump()

    def close(self):
        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
#  Download + apply
# ---------------------------------------------------------------------------
def _fetch_zip(sha, timeout=90, on_progress=None):
    # A per-commit archive is immutable, so it can never be a stale/cached copy.
    url = f"https://codeload.github.com/{REPO}/zip/{sha}"
    resp = _open(url, timeout)
    total = int(resp.headers.get("Content-Length") or 0)
    buf = io.BytesIO()
    got = 0
    if on_progress:
        try:
            on_progress(0, total)
        except Exception:
            pass
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        buf.write(chunk)
        got += len(chunk)
        if on_progress:
            try:
                on_progress(got, total)
            except Exception:
                pass
    return buf.getvalue()


def _apply_zip(data):
    zf = zipfile.ZipFile(io.BytesIO(data))
    root = zf.namelist()[0].split("/")[0] + "/"     # e.g. "remote-desktop-<sha>/"
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        rel = name[len(root):]
        if not rel or rel in PRESERVE or rel.startswith(".git"):
            continue
        target = os.path.join(HERE, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(target) or HERE, exist_ok=True)
        with zf.open(name) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def _do_update(sha, splash=None):
    data = _fetch_zip(sha, on_progress=(splash.on_download if splash else None))
    if splash:
        splash.message("Installing…", pct=100)
    _apply_zip(data)
    _record_commit(sha)
    if splash:
        splash.message("Done — starting the app…", pct=100)
        time.sleep(0.6)


def check_and_update():
    """Return True if an update was applied and a fresh copy was relaunched."""
    if getattr(sys, "frozen", False):
        return False                # packaged .exe: source-swap update doesn't apply
    if not _configured():
        return False
    try:
        sha = latest_commit()
    except Exception:
        return False                # offline / unreachable -> just run
    if not sha or sha == _applied_commit():
        return False                # already on the newest commit
    splash = None
    try:
        splash = _Splash()          # show the progress window
    except Exception:
        splash = None               # no display available -> update silently
    try:
        _do_update(sha, splash)
    except Exception:
        if splash:
            splash.close()
        return False                # any failure -> run the current version
    if splash:
        splash.close()
    # Relaunch the freshly downloaded code in a new process, then let this one exit.
    try:
        subprocess.Popen([sys.executable] + sys.argv, cwd=HERE, close_fds=False)
        return True
    except Exception:
        return False


def force_update():
    """Re-download the newest commit now, ignoring what's installed (FORCE_UPDATE.bat)."""
    if not _configured():
        print("Updater is not configured.")
        return False
    try:
        sha = latest_commit()
    except Exception as e:
        print(f"Could not reach GitHub: {e}")
        return False
    splash = None
    try:
        splash = _Splash()
    except Exception:
        splash = None
    try:
        _do_update(sha, splash)
    except Exception as e:
        if splash:
            splash.close()
        print(f"Update failed: {e}")
        return False
    if splash:
        splash.close()
    print(f"Updated to latest ({sha[:7]}).")
    return True


if __name__ == "__main__":
    force_update()
