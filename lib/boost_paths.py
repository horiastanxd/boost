"""Where Boost keeps its configuration and state, per platform.

Every other module imports its paths from here instead of hardcoding them, so
adding a platform is a change in one file. The Linux values are exactly the
ones Boost has always used — `/etc/boost-auto.conf` and
`/var/lib/power-profile/` — and must not change.

Windows has no `/etc` and no `/var`, so both live under
`%ProgramData%\\Boost` (typically `C:\\ProgramData\\Boost`), which is the
conventional home for machine-wide state a service writes to.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")


def _windows_root() -> Path:
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Boost"


def config_dir() -> Path:
    """Directory holding boost-auto.conf and boost-fans.json."""
    return _windows_root() if IS_WINDOWS else Path("/etc")


def state_dir() -> Path:
    """Directory holding runtime state: stats.csv, live.json, reports."""
    return _windows_root() / "state" if IS_WINDOWS else Path("/var/lib/power-profile")


CONF_FILE = config_dir() / "boost-auto.conf"
FANS_FILE = config_dir() / "boost-fans.json"

STATE_DIR = state_dir()
STATS_FILE = STATE_DIR / "stats.csv"
LIVE_FILE = STATE_DIR / "live.json"
SNOOZE_FILE = STATE_DIR / "auto-snooze-until"
SKIP_TODAY_FILE = STATE_DIR / "auto-skip-date"
SILENT_PENDING_FILE = STATE_DIR / "silent-pending"
LATEST_REPORT = STATE_DIR / "reports" / "latest.html"


def ensure_state_dir() -> None:
    """Create the state directory if it is missing.

    On Linux install.sh already does this; on Windows there is no installer
    step that runs as SYSTEM, so the first writer creates it.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # read-only or unprivileged: callers already tolerate missing state
