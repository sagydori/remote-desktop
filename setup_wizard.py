"""
setup_wizard.py  —  The graphical Setup Wizard (run once per PC).

Launched by INSTALL.bat after Python + libraries are installed. Steps:
  1. Install Tailscale and log in (same account on both PCs).
  2. Enter the OTHER PC's Tailscale name + a shared password.
  3. It writes config.py, drops a Desktop shortcut, and can launch the app.

Every PC runs the SAME app (remote_desktop.py) and can control the other,
so there is no "host vs laptop" choice anymore.
Re-run any time to change settings:  pythonw setup_wizard.py
"""

import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import urllib.request

import customtkinter as ctk

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.py")
TS_MSI_URL = "https://pkgs.tailscale.com/stable/tailscale-setup-latest-amd64.msi"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

BG, CARD, ACCENT, MUTED = "#12141a", "#1b1e26", "#3b82f6", "#8b93a1"


def _run(cmd, timeout=25):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=NO_WINDOW)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


def tailscale_exe():
    for p in (r"C:\Program Files\Tailscale\tailscale.exe",
              r"C:\Program Files (x86)\Tailscale\tailscale.exe"):
        if os.path.exists(p):
            return p
    from shutil import which
    return which("tailscale")


def tailscale_ip():
    exe = tailscale_exe()
    if not exe:
        return None
    code, out, _ = _run([exe, "ip", "-4"])
    return out.splitlines()[0].strip() if code == 0 and out else None


def tailscale_name():
    exe = tailscale_exe()
    if not exe:
        return None
    code, out, _ = _run([exe, "status", "--json"])
    if code == 0 and out:
        try:
            dns = json.loads(out).get("Self", {}).get("DNSName", "").rstrip(".")
            if dns:
                return dns.split(".")[0]
        except Exception:
            pass
    return None


def pythonw_path():
    cand = sys.executable.replace("python.exe", "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


def write_config(name, peer, port, secret):
    text = ('"""config.py — written by the Setup Wizard."""\n\n'
            f'NAME = {name!r}\nPEER = {peer!r}\nPORT = {int(port)}\nSECRET = {secret!r}\n')
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def read_config():
    ns = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            exec(f.read(), ns)
    except Exception:
        pass
    peer = ns.get("PEER", "") or ns.get("HOST", "")
    return ns.get("NAME", ""), peer, ns.get("PORT", 8765), ns.get("SECRET", "")


def _psq(s):
    return s.replace("'", "''")


def make_shortcut(name, script):
    target, args = pythonw_path(), f'"{os.path.join(HERE, script)}"'
    ps = ("$d=[Environment]::GetFolderPath('Desktop');"
          f"$p=Join-Path $d '{_psq(name)}.lnk';"
          "$w=New-Object -ComObject WScript.Shell;$s=$w.CreateShortcut($p);"
          f"$s.TargetPath='{_psq(target)}';$s.Arguments='{_psq(args)}';"
          f"$s.WorkingDirectory='{_psq(HERE)}';$s.IconLocation='{_psq(target)}';"
          "$s.Save();Write-Output $p")
    code, out, _ = _run(["powershell", "-NoProfile", "-Command", ps], timeout=20)
    return out.strip().splitlines()[-1] if code == 0 and out else None


class Wizard(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("Remote Desktop — Setup")
        self.geometry("660x580")
        self.minsize(620, 540)
        self.configure(fg_color=BG)

        self.jobs = queue.Queue()
        name, peer, port, secret = read_config()
        self.this_name = name or (tailscale_name() or socket.gethostname())
        self.peer = peer
        self.port = port or 8765
        self.secret = secret or secrets.token_urlsafe(10)

        ctk.CTkLabel(self, text="Remote Desktop Setup",
                     font=ctk.CTkFont(size=26, weight="bold")).pack(pady=(24, 2))
        self.step_label = ctk.CTkLabel(self, text="", text_color=MUTED)
        self.step_label.pack()
        self.content = ctk.CTkFrame(self, corner_radius=16, fg_color=CARD)
        self.content.pack(fill="both", expand=True, padx=28, pady=18)

        self.step_tailscale()
        self.after(120, self._poll)

    def _clear(self):
        for w in self.content.winfo_children():
            w.destroy()

    def _bg(self, fn, on_done):
        def worker():
            try:
                res = fn()
            except Exception as e:
                res = e
            self.jobs.put((on_done, res))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                cb, res = self.jobs.get_nowait()
                cb(res)
        except queue.Empty:
            pass
        self.after(120, self._poll)

    # ---- step 1: Tailscale ----------------------------------------------
    def step_tailscale(self):
        self._clear()
        self.step_label.configure(text="Step 1 of 2  ·  Tailscale (secure link)")
        ctk.CTkLabel(self.content, text="Set up Tailscale",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(22, 4))
        ctk.CTkLabel(self.content, wraplength=540, justify="center", text_color=MUTED,
                     text=("Tailscale is a free app that privately links your PCs so they can reach "
                           "each other from anywhere. Install it and log in with the SAME account "
                           "on both PCs.")).pack(pady=(0, 14), padx=20)
        self.ts_status = ctk.CTkLabel(self.content, text="Checking...", text_color="#c7cbd4")
        self.ts_status.pack(pady=4)
        self.ts_addr = ctk.CTkLabel(self.content, text="", font=ctk.CTkFont(size=15, weight="bold"))
        self.ts_addr.pack(pady=4)

        row = ctk.CTkFrame(self.content, fg_color="transparent")
        row.pack(pady=14)
        self.btn_install = ctk.CTkButton(row, text="Install Tailscale", command=self._install_ts)
        self.btn_install.grid(row=0, column=0, padx=6)
        self.btn_login = ctk.CTkButton(row, text="Log in", command=self._login_ts)
        self.btn_login.grid(row=0, column=1, padx=6)
        self.btn_detect = ctk.CTkButton(row, text="Detect", command=self._detect_ts)
        self.btn_detect.grid(row=0, column=2, padx=6)

        nav = ctk.CTkFrame(self.content, fg_color="transparent")
        nav.pack(side="bottom", fill="x", pady=16, padx=20)
        ctk.CTkButton(nav, text="Next →", width=120, command=self.step_settings).pack(side="right")
        self._refresh_ts()

    def _refresh_ts(self):
        if not tailscale_exe():
            self.ts_status.configure(text="❌  Tailscale is not installed yet.", text_color="#d97706")
            self.ts_addr.configure(text="")
            self.btn_login.configure(state="disabled")
            self.btn_detect.configure(state="disabled")
            return
        self.btn_install.configure(text="Installed ✓", state="disabled")
        self.btn_login.configure(state="normal")
        self.btn_detect.configure(state="normal")
        ip, name = tailscale_ip(), tailscale_name()
        if ip:
            if name:
                self.this_name = name
            self.ts_status.configure(text="✅  Connected. This PC's Tailscale name:", text_color="#16a34a")
            self.ts_addr.configure(text=f"{name or '(no name)'}    ·    {ip}")
        else:
            self.ts_status.configure(text="⚠  Installed but not logged in. Click 'Log in'.",
                                     text_color="#d97706")
            self.ts_addr.configure(text="")

    def _install_ts(self):
        self.ts_status.configure(text="Downloading Tailscale...", text_color="#c7cbd4")
        self.btn_install.configure(state="disabled")

        def job():
            path = os.path.join(os.environ.get("TEMP", HERE), "tailscale-setup.msi")
            urllib.request.urlretrieve(TS_MSI_URL, path)
            subprocess.run(["msiexec", "/i", path], creationflags=NO_WINDOW)
            return True

        def done(res):
            if isinstance(res, Exception):
                self.ts_status.configure(text=f"Install failed: {res}", text_color="#ef4444")
                self.btn_install.configure(state="normal")
            else:
                self._refresh_ts()
        self._bg(job, done)

    def _login_ts(self):
        exe = tailscale_exe()
        if exe:
            self.ts_status.configure(text="Opening Tailscale login in your browser...",
                                     text_color="#c7cbd4")
            self._bg(lambda: _run([exe, "up"], timeout=90), lambda r: self._refresh_ts())

    def _detect_ts(self):
        self.ts_status.configure(text="Detecting...", text_color="#c7cbd4")
        self._bg(lambda: True, lambda r: self._refresh_ts())

    # ---- step 2: settings + finish --------------------------------------
    def step_settings(self):
        self._clear()
        self.step_label.configure(text="Step 2 of 2  ·  Settings")
        ctk.CTkLabel(self.content, text="Almost done",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(22, 8))

        ctk.CTkLabel(self.content, text="This PC's Tailscale name:").pack(anchor="w", padx=28, pady=(6, 2))
        self.name_entry = ctk.CTkEntry(self.content, width=440)
        self.name_entry.insert(0, self.this_name)
        self.name_entry.pack(padx=28)

        ctk.CTkLabel(self.content, text="Other PC's Tailscale name (the one you'll control):").pack(
            anchor="w", padx=28, pady=(14, 2))
        self.peer_entry = ctk.CTkEntry(self.content, width=440, placeholder_text="e.g. home-pc")
        if self.peer:
            self.peer_entry.insert(0, self.peer)
        self.peer_entry.pack(padx=28)

        ctk.CTkLabel(self.content, text="Shared password (must match on BOTH PCs):").pack(
            anchor="w", padx=28, pady=(14, 2))
        self.pw_entry = ctk.CTkEntry(self.content, width=440)
        self.pw_entry.insert(0, self.secret)
        self.pw_entry.pack(padx=28)

        self.finish_msg = ctk.CTkLabel(self.content, text="", text_color=MUTED)
        self.finish_msg.pack(pady=(14, 0))
        nav = ctk.CTkFrame(self.content, fg_color="transparent")
        nav.pack(side="bottom", fill="x", pady=16, padx=20)
        ctk.CTkButton(nav, text="← Back", width=90, fg_color="#3a3f4b", hover_color="#2c313b",
                      command=self.step_tailscale).pack(side="left")
        ctk.CTkButton(nav, text="Finish & Save", width=150, command=self._finish).pack(side="right")

    def _finish(self):
        name = self.name_entry.get().strip()
        peer = self.peer_entry.get().strip()
        secret = self.pw_entry.get().strip()
        if not peer or not secret:
            self.finish_msg.configure(text="⚠  Fill in the other PC's name and a password.",
                                      text_color="#ef4444")
            return
        write_config(name, peer, self.port, secret)
        lnk = make_shortcut("Remote Desktop", "remote_desktop.py")

        self._clear()
        self.step_label.configure(text="All set!")
        ctk.CTkLabel(self.content, text="✅  Setup complete",
                     font=ctk.CTkFont(size=22, weight="bold"), text_color="#16a34a").pack(pady=(46, 10))
        msg = ("A 'Remote Desktop' shortcut was added to your Desktop.\n\n"
               "Open it on both PCs. Each one can then control the other — just press "
               "the Control button. Leave the app open to allow the other PC in.")
        if not lnk:
            msg = ("Settings saved. (Couldn't make a desktop shortcut — run remote_desktop.py "
                   "from this folder.)\n\n" + msg.split("\n\n", 1)[1])
        ctk.CTkLabel(self.content, text=msg, wraplength=540, justify="center",
                     text_color="#c7cbd4").pack(pady=6, padx=20)
        row = ctk.CTkFrame(self.content, fg_color="transparent")
        row.pack(pady=26)
        ctk.CTkButton(row, text="Launch now", width=140, command=self._launch).grid(row=0, column=0, padx=8)
        ctk.CTkButton(row, text="Close", width=100, fg_color="#3a3f4b", hover_color="#2c313b",
                      command=self.destroy).grid(row=0, column=1, padx=8)

    def _launch(self):
        subprocess.Popen([pythonw_path(), os.path.join(HERE, "remote_desktop.py")], cwd=HERE)
        self.destroy()


if __name__ == "__main__":
    Wizard().mainloop()
