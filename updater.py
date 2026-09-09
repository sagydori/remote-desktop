"""
updater.py  —  Silent auto-update from GitHub.

remote_desktop.py calls check_and_update() at launch. It compares the local
VERSION file with the one in the GitHub repo; if the repo is newer it downloads
the latest code, overwrites the app files (never config.py), and relaunches.

If the PC is offline or the repo is unreachable, it does nothing and the app
just starts normally. REPO is filled in once the GitHub repo exists.
"""

import io
import os
import shutil
import sys
import urllib.request
import zipfile

REPO = "sagydori/remote-desktop"
BRANCH = "main"

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(HERE, "VERSION")
PRESERVE = {"config.py"}         # local settings are never overwritten


def _configured():
    return REPO and "REPLACE_WITH" not in REPO


def local_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "0"


def remote_version(timeout=5):
    url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8").strip()


def _download_and_apply(timeout=45):
    url = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"
    data = urllib.request.urlopen(url, timeout=timeout).read()
    zf = zipfile.ZipFile(io.BytesIO(data))
    root = zf.namelist()[0].split("/")[0] + "/"     # e.g. "repo-main/"
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


def check_and_update():
    """Return True if an update was applied and the app was relaunched."""
    if not _configured():
        return False
    try:
        if remote_version() == local_version():
            return False
    except Exception:
        return False                # offline / unreachable -> just run
    try:
        _download_and_apply()
    except Exception:
        return False                # any failure -> run the current version
    # Relaunch with the freshly downloaded code.
    os.execv(sys.executable, [sys.executable] + sys.argv)
    return True
