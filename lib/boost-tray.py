#!/usr/bin/env python3
"""Boost Power Manager – system tray applet.

Provides a GTK tray indicator for quick access to power profiles,
auto-mode switching, snooze controls, and live CPU telemetry.
"""
import os
import sys
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timezone
import threading

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AyatanaAppIndicator3', '0.1')
gi.require_version('Notify', '0.7')
from gi.repository import Gtk, Gdk, GLib, AyatanaAppIndicator3, Notify

VERSION = "1.2.0"
STATE_DIR = Path("/var/lib/power-profile")
CONF_FILE = Path("/etc/boost-auto.conf")

# ── Cached browser path ──────────────────────────────────────────────
_cached_browser_path = None

# ── Cached power profile (refreshed every 15 s / 5 cycles) ──────────
_cached_profile = None
_profile_cycle_count = 0

Notify.init("Boost")

def run_cmd(cmd):
    subprocess.Popen(cmd, shell=True, env=dict(os.environ, AUTO_HELPER_INTERNAL="1"))

def notify(message, icon="power-profile-balanced-symbolic"):
    """Show a native GNOME desktop notification."""
    try:
        Notify.Notification.new("Boost Power Manager", message, icon).show()
    except Exception:
        pass

def open_dashboard(_widget=None):
    """Open the web dashboard in the user's browser, trying multiple strategies."""
    global _cached_browser_path
    url = "http://127.0.0.1:8765"
    env = os.environ.copy()

    # Use cached browser if we already found one
    if _cached_browser_path:
        try:
            subprocess.Popen([_cached_browser_path, url], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:
            _cached_browser_path = None  # invalidate, fall through to re-discover

    # Discover browser using shutil.which (no subprocess overhead)
    browsers = [
        'brave-browser', 'brave', 'google-chrome', 'google-chrome-stable',
        'chromium-browser', 'chromium', 'firefox', 'firefox-esr',
        'microsoft-edge', 'opera', 'vivaldi',
    ]
    for browser in browsers:
        path = shutil.which(browser)
        if path:
            _cached_browser_path = path
            subprocess.Popen([path, url], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return

    # Fallback: xdg-open
    try:
        subprocess.Popen(['xdg-open', url], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def read_text(path, default="unknown"):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return default

_cached_temp_file = None

def get_cpu_temp():
    global _cached_temp_file
    if _cached_temp_file:
        try:
            return int(read_text(_cached_temp_file, "0") or "0") // 1000
        except ValueError:
            _cached_temp_file = None

    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        name = read_text(hwmon / "name", "")
        if name not in {"coretemp", "k10temp", "zenpower", "amd_energy"}:
            continue
        for label_file in hwmon.glob("temp*_label"):
            label = read_text(label_file, "")
            if label in {"Package id 0", "Tctl", "Tdie", "Tccd1", "Tccd2"}:
                _cached_temp_file = str(label_file).replace("_label", "_input")
                return int(read_text(_cached_temp_file, "0") or "0") // 1000
        input_file = hwmon / "temp1_input"
        raw = int(read_text(input_file, "0") or "0")
        if raw > 0:
            _cached_temp_file = str(input_file)
            return raw // 1000
    return 0

_prev_total = 0
_prev_idle = 0
def get_cpu_load():
    global _prev_total, _prev_idle
    try:
        parts = read_text("/proc/stat", "").splitlines()[0].split()
        values = [int(x) for x in parts[1:]]
        idle = values[3] + values[4]
        total = sum(values)
        delta_total = total - _prev_total
        delta_idle = idle - _prev_idle
        _prev_total = total
        _prev_idle = idle
        if delta_total <= 0: return 0
        return int((delta_total - delta_idle) * 100 / delta_total)
    except Exception:
        return 0

def get_profile():
    """Return the active power profile, using a 15-second cache."""
    global _cached_profile, _profile_cycle_count
    _profile_cycle_count += 1
    if _cached_profile is None or _profile_cycle_count >= 5:
        _profile_cycle_count = 0
        try:
            out = subprocess.check_output(['powerprofilesctl', 'get'], text=True).strip()
            _cached_profile = out
        except Exception:
            _cached_profile = "balanced"
    return _cached_profile

def get_auto_mode():
    mode = "friendly"
    try:
        for line in read_text(CONF_FILE, "").splitlines():
            if line.startswith("AUTO_MODE="):
                mode = line.split("=")[1].strip()
    except Exception:
        pass
    return mode

def get_snooze_remaining():
    """Return minutes remaining on snooze, or 0 if not snoozed."""
    try:
        until_str = read_text(STATE_DIR / "auto-snooze-until", "")
        if not until_str:
            return 0
        until_ts = int(until_str)
        now_ts = int(datetime.now(timezone.utc).timestamp())
        remaining = until_ts - now_ts
        return max(0, remaining // 60)
    except (ValueError, OSError):
        return 0

def is_today_off():
    """Return True if auto-mode is skipped for today."""
    try:
        skip_date = read_text(STATE_DIR / "skip-date", "")
        if not skip_date:
            return False
        return skip_date == datetime.now().strftime("%Y-%m-%d")
    except Exception:
        return False


class BoostTray:
    def __init__(self):
        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "boost-tray",
            "power-profile-balanced-symbolic",
            AyatanaAppIndicator3.IndicatorCategory.SYSTEM_SERVICES
        )
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self._last_state = {
            "temp": 0, "load": 0, "prof": "balanced",
            "amode": "friendly", "snooze_mins": 0, "today_skip": False
        }

        # Build Menu
        self.menu = Gtk.Menu()

        self.item_stats = Gtk.MenuItem(label="Checking stats...")
        self.item_stats.set_sensitive(False)
        self.menu.append(self.item_stats)

        self.item_mode = Gtk.MenuItem(label="Mode: Unknown")
        self.item_mode.set_sensitive(False)
        self.menu.append(self.item_mode)

        self.menu.append(Gtk.SeparatorMenuItem())

        self.add_profile_item("🚀 Boost (Max Performance)", "boost", "performance")
        self.add_profile_item("🍃 Powersave (Cool & Quiet)", "powersave", "power-saver")
        self.add_profile_item("🌙 Silent (Overnight)", "silent", "power-saver")
        self.add_profile_item("♻️ Restore BIOS Defaults", "restore", "balanced")

        self.menu.append(Gtk.SeparatorMenuItem())

        # Auto Modes
        self.auto_menu = Gtk.Menu()
        auto_item = Gtk.MenuItem(label="🤖 Auto Mode")
        auto_item.set_submenu(self.auto_menu)
        self.menu.append(auto_item)

        self.auto_mode_items = {}
        for m in ["Calm", "Summer", "Friendly", "Active", "Quiet", "Off"]:
            item = Gtk.MenuItem(label=m)
            item.connect("activate", self.on_auto_mode, m.lower())
            self.auto_menu.append(item)
            self.auto_mode_items[m.lower()] = item

        # Snooze
        snooze_menu = Gtk.Menu()
        snooze_item = Gtk.MenuItem(label="⏳ Snooze Auto")
        snooze_item.set_submenu(snooze_menu)
        self.menu.append(snooze_item)

        for duration in ["30m", "1h", "2h", "4h"]:
            item = Gtk.MenuItem(label=duration)
            item.connect("activate", self.on_snooze, duration)
            snooze_menu.append(item)

        today_off = Gtk.MenuItem(label="All Today")
        today_off.connect("activate", self.on_today_off)
        snooze_menu.append(today_off)

        resume_item = Gtk.MenuItem(label="▶️ Resume Auto Mode")
        resume_item.connect("activate", self.on_resume)
        snooze_menu.append(resume_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        dash_item = Gtk.MenuItem(label="📊 Open Web Dashboard")
        dash_item.connect("activate", open_dashboard)
        self.menu.append(dash_item)

        quit_item = Gtk.MenuItem(label="Quit Tray")
        quit_item.connect("activate", Gtk.main_quit)
        self.menu.append(quit_item)

        # ── Version footer ────────────────────────────────────────
        self.menu.append(Gtk.SeparatorMenuItem())
        version_item = Gtk.MenuItem(label=f"Boost v{VERSION}")
        version_item.set_sensitive(False)
        self.menu.append(version_item)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)

        # Start update loop
        get_cpu_load()  # initialize baseline
        GLib.timeout_add_seconds(3, self.update_status)
        self.update_status()

    def add_profile_item(self, label, command, expected_profile):
        """Add a power-profile menu item that also sends a desktop notification."""
        item = Gtk.MenuItem(label=label)
        friendly = label.split("(")[0].strip().lstrip("🚀🍃🌙♻️ ").strip()
        item.connect("activate", self._on_profile_click, command, friendly)
        self.menu.append(item)

    def _on_profile_click(self, _widget, command, friendly_name):
        """Execute a profile command and show a notification."""
        global _cached_profile, _profile_cycle_count
        run_cmd(f"/usr/local/bin/{command}")
        # Invalidate profile cache so next status update picks up the change
        _cached_profile = None
        _profile_cycle_count = 0
        notify(f"Switched to {friendly_name} mode ⚡")
        # Optimistic UI update
        prof = {"boost": "performance", "powersave": "balanced", "silent": "power-saver"}.get(command, "balanced")
        self._last_state["prof"] = prof
        GLib.idle_add(lambda: self.apply_status(**self._last_state) or False)

    def on_auto_mode(self, widget, mode):
        run_cmd(f"/usr/local/bin/auto mode {mode}")
        notify(f"Auto mode set to {mode.capitalize()}")
        self._last_state["amode"] = mode
        GLib.idle_add(lambda: self.apply_status(**self._last_state) or False)

    def on_snooze(self, widget, duration):
        run_cmd(f"/usr/local/bin/auto snooze {duration}")
        notify(f"Auto mode snoozed for {duration}")
        mins = {"30m": 30, "1h": 60, "2h": 120, "4h": 240}.get(duration, 30)
        self._last_state["snooze_mins"] = mins
        self._last_state["today_skip"] = False
        GLib.idle_add(lambda: self.apply_status(**self._last_state) or False)

    def on_today_off(self, widget):
        run_cmd("/usr/local/bin/auto today-off")
        notify("Auto mode paused for today")
        self._last_state["today_skip"] = True
        self._last_state["snooze_mins"] = 0
        GLib.idle_add(lambda: self.apply_status(**self._last_state) or False)

    def on_resume(self, widget):
        run_cmd("/usr/local/bin/auto resume")
        notify("Auto mode resumed")
        self._last_state["snooze_mins"] = 0
        self._last_state["today_skip"] = False
        GLib.idle_add(lambda: self.apply_status(**self._last_state) or False)

    def update_status(self):
        def fetch_data():
            temp = get_cpu_temp()
            load = get_cpu_load()
            prof = get_profile()
            amode = get_auto_mode()
            snooze_mins = get_snooze_remaining()
            today_skip = is_today_off()
            GLib.idle_add(self.apply_status, temp, load, prof, amode,
                          snooze_mins, today_skip)

        threading.Thread(target=fetch_data, daemon=True).start()
        return True

    def apply_status(self, temp, load, prof, amode, snooze_mins, today_skip):
        self._last_state.update({
            "temp": temp, "load": load, "prof": prof,
            "amode": amode, "snooze_mins": snooze_mins, "today_skip": today_skip
        })
        icon = "power-profile-balanced-symbolic"
        if prof == "performance": icon = "power-profile-performance-symbolic"
        elif prof == "power-saver": icon = "power-profile-power-saver-symbolic"

        if temp > 80: icon = "dialog-warning-symbolic"

        self.indicator.set_icon_full(icon, "Boost Status")

        # Stats line – include snooze remaining if active
        stats_label = f"🌡️ CPU: {temp}°C  |  ⚡ Load: {load}%"
        if snooze_mins > 0:
            if snooze_mins >= 60:
                h, m = divmod(snooze_mins, 60)
                stats_label += f"  |  ⏳ Snooze: {h}h{m:02d}m left"
            else:
                stats_label += f"  |  ⏳ Snooze: {snooze_mins}m left"
        self.item_stats.set_label(stats_label)

        # Mode line – show snoozed/paused indicator
        friendly = {"performance": "Boost", "balanced": "Balanced",
                     "power-saver": "Powersave"}.get(prof, prof)
        auto_suffix = amode
        if snooze_mins > 0:
            auto_suffix += " · Snoozed"
        elif today_skip:
            auto_suffix += " · Paused"
        self.item_mode.set_label(f"⚙️ Profile: {friendly}  (Auto: {auto_suffix})")

        # Update auto-mode checkmarks
        for key, item in self.auto_mode_items.items():
            if key == amode:
                item.set_label(f"✓ {key.capitalize()}")
            else:
                item.set_label(key.capitalize())

if __name__ == "__main__":
    BoostTray()
    Gtk.main()
