========================================================================
 REMOTE DESKTOP  —  one app, both PCs can control each other
========================================================================

You run ONE thing to install:  INSTALL.bat
It sets up everything (Python, libraries, Tailscale) and opens a Setup
Wizard. After that, updates download themselves — you never re-copy the
folder to your other PC again.

------------------------------------------------------------------------
FIRST-TIME SETUP  (do on BOTH PCs)
------------------------------------------------------------------------
1. Copy this folder onto the PC (only needed the first time).
2. Double-click  INSTALL.bat  and wait. Allow the Python / Tailscale
   installers if Windows asks.
3. In the wizard:
     - Install Tailscale and log in.  << SAME account on both PCs >>
     - Enter the OTHER PC's Tailscale name and a shared password
       (the password must be identical on both PCs).
4. A "Remote Desktop" shortcut is added to your Desktop.

------------------------------------------------------------------------
EVERY DAY
------------------------------------------------------------------------
Open "Remote Desktop" on the PC you're sitting at. Leave it open on the
other PC too (that's what lets you in). Press the big
   "Control  <other pc>  →"
button to take over the other computer.

Either PC can control the other — it's the same app on both.

Controls while controlling: Disconnect / Pause input / Fullscreen (F11),
with live FPS / latency / bandwidth in the corner.

------------------------------------------------------------------------
GOOD TO KNOW
------------------------------------------------------------------------
* Both PCs must be on the SAME Tailscale account.
* Keep the PC you want to reach awake (Power settings -> never sleep).
* Fullscreen games are captured via Desktop Duplication. If a game still
  looks frozen, set it to Borderless/Windowed mode.
* Admin apps: right-click the shortcut -> Run as administrator. Windows
  security screens (UAC prompt, lock screen, Ctrl+Alt+Del) can't be
  controlled remotely — a Windows safety rule.
* Change settings later: run setup_wizard.py (in this folder).
* Updates install automatically at launch when connected to the internet.
