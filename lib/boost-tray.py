#!/usr/bin/env python3
import os
import sys
import time
import subprocess
import webbrowser
from pathlib import Path
import threading

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AyatanaAppIndicator3', '0.1')
from gi.repository import Gtk, Gdk, GLib, AyatanaAppIndicator3

STATE_DIR = Path("/var/lib/power-profile")
CONF_FILE = Path("/etc/boost-auto.conf")

def run_cmd(cmd):
    subprocess.Popen(cmd, shell=True, env=dict(os.environ, AUTO_HELPER_INTERNAL="1"))

def open_dashboard(_widget=None):
    """Open the web dashboard in the user's browser, trying multiple strategies."""
    url = "http://127.0.0.1:8765"
    env = os.environ.copy()
    
    # Strategy 1: Find the actual browser binary and launch directly
    browsers = [
        'brave-browser', 'brave', 'google-chrome', 'google-chrome-stable',
        'chromium-browser', 'chromium', 'firefox', 'firefox-esr',
        'microsoft-edge', 'opera', 'vivaldi',
    ]
    for browser in browsers:
        try:
            path = subprocess.check_output(['which', browser], stderr=subprocess.DEVNULL, text=True).strip()
            if path:
                subprocess.Popen([path, url], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        except Exception:
            continue
    
    # Strategy 2: xdg-open as fallback
    try:
        subprocess.Popen(['xdg-open', url], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def read_text(path, default="unknown"):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return default

def get_cpu_temp():
    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        name = read_text(hwmon / "name", "")
        if name not in {"coretemp", "k10temp", "zenpower", "amd_energy"}:
            continue
        for label_file in hwmon.glob("temp*_label"):
            label = read_text(label_file, "")
            if label in {"Package id 0", "Tctl", "Tdie", "Tccd1", "Tccd2"}:
                raw = int(read_text(str(label_file).replace("_label", "_input"), "0") or "0")
                return raw // 1000
        raw = int(read_text(hwmon / "temp1_input", "0") or "0")
        if raw > 0:
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
    try:
        out = subprocess.check_output(['powerprofilesctl', 'get'], text=True).strip()
        return out
    except Exception:
        return "balanced"

def get_auto_mode():
    mode = "friendly"
    try:
        for line in read_text(CONF_FILE, "").splitlines():
            if line.startswith("AUTO_MODE="):
                mode = line.split("=")[1].strip()
    except Exception:
        pass
    return mode

class BoostTray:
    def __init__(self):
        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "boost-tray",
            "power-profile-balanced-symbolic",
            AyatanaAppIndicator3.IndicatorCategory.SYSTEM_SERVICES
        )
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        
        # Build Menu
        self.menu = Gtk.Menu()
        
        self.item_stats = Gtk.MenuItem(label="Checking stats...")
        self.item_stats.set_sensitive(False)
        self.menu.append(self.item_stats)
        
        self.item_mode = Gtk.MenuItem(label="Mode: Unknown")
        self.item_mode.set_sensitive(False)
        self.menu.append(self.item_mode)
        
        self.menu.append(Gtk.SeparatorMenuItem())
        
        self.add_menu_item("🚀 Boost (Max Performance)", "boost")
        self.add_menu_item("🍃 Powersave (Cool & Quiet)", "powersave")
        self.add_menu_item("🌙 Silent (Overnight)", "silent")
        self.add_menu_item("♻️ Restore BIOS Defaults", "restore")
        
        self.menu.append(Gtk.SeparatorMenuItem())
        
        # Auto Modes
        auto_menu = Gtk.Menu()
        auto_item = Gtk.MenuItem(label="🤖 Auto Mode")
        auto_item.set_submenu(auto_menu)
        self.menu.append(auto_item)
        
        for m in ["Calm", "Summer", "Friendly", "Active", "Quiet", "Off"]:
            item = Gtk.MenuItem(label=m)
            item.connect("activate", self.on_auto_mode, m.lower())
            auto_menu.append(item)
            
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
        today_off.connect("activate", lambda w: run_cmd("/usr/local/bin/auto today-off"))
        snooze_menu.append(today_off)
        
        resume_item = Gtk.MenuItem(label="▶️ Resume Auto Mode")
        resume_item.connect("activate", lambda w: run_cmd("/usr/local/bin/auto resume"))
        snooze_menu.append(resume_item)
        
        self.menu.append(Gtk.SeparatorMenuItem())
        
        dash_item = Gtk.MenuItem(label="📊 Open Web Dashboard")
        dash_item.connect("activate", open_dashboard)
        self.menu.append(dash_item)
        
        quit_item = Gtk.MenuItem(label="Quit Tray")
        quit_item.connect("activate", Gtk.main_quit)
        self.menu.append(quit_item)
        
        self.menu.show_all()
        self.indicator.set_menu(self.menu)
        
        # Start update loop
        get_cpu_load() # initialize baseline
        GLib.timeout_add_seconds(3, self.update_status)
        self.update_status()
        
    def add_menu_item(self, label, command):
        item = Gtk.MenuItem(label=label)
        item.connect("activate", lambda w: run_cmd(f"/usr/local/bin/{command}"))
        self.menu.append(item)
        
    def on_auto_mode(self, widget, mode):
        run_cmd(f"/usr/local/bin/auto mode {mode}")
        
    def on_snooze(self, widget, duration):
        run_cmd(f"/usr/local/bin/auto snooze {duration}")
        
    def update_status(self):
        def fetch_data():
            temp = get_cpu_temp()
            load = get_cpu_load()
            prof = get_profile()
            amode = get_auto_mode()
            GLib.idle_add(self.apply_status, temp, load, prof, amode)
        
        threading.Thread(target=fetch_data, daemon=True).start()
        return True
        
    def apply_status(self, temp, load, prof, amode):
        icon = "power-profile-balanced-symbolic"
        if prof == "performance": icon = "power-profile-performance-symbolic"
        elif prof == "power-saver": icon = "power-profile-power-saver-symbolic"
        
        if temp > 80: icon = "dialog-warning-symbolic"
        
        self.indicator.set_icon_full(icon, "Boost Status")
        
        self.item_stats.set_label(f"🌡️ CPU: {temp}°C  |  ⚡ Load: {load}%")
        
        friendly = {"performance": "Boost", "balanced": "Balanced", "power-saver": "Powersave"}.get(prof, prof)
        self.item_mode.set_label(f"⚙️ Profile: {friendly}  (Auto: {amode})")

if __name__ == "__main__":
    BoostTray()
    Gtk.main()
