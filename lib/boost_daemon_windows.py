"""Windows auto daemon: the parts of the Linux "auto" behavior that Windows
can support safely.

Boost's Linux daemon (boost-daemon.py) reads sysfs directly and drives fan
curves, RAPL and EPP. None of that exists on Windows. What this module does
instead is the platform-independent half of "dynamic" mode: watch CPU/GPU
temperature and load, notice games/creator workloads/meetings by process
name, react to AC/battery and screen-lock changes, and switch profiles
through WindowsBackend.apply_profile() the same way the dashboard does.

There is no systemd here, so this module also owns the bits systemd would
normally provide: a pidfile so `auto stop`/`auto status` know whether the
daemon is running, and a log file since there is no journal to read.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import boost_paths
import platform_backend

AUTO_MODES = {"dynamic", "gaming", "creator", "quiet", "off", "custom"}
YES_NO = {"yes", "no"}

# Process names as they appear in `tasklist`. Kept short and specific rather
# than exhaustive, matching the intent (not the exact list) of the Linux
# daemon's GAME_PROCESSES / CREATOR_PROCESSES / MEETING_PROCESSES.
GAME_PROCESSES = {
    "steam.exe", "epicgameslauncher.exe", "riotclientservices.exe",
    "cs2.exe", "csgo.exe", "dota2.exe", "valorant-win64-shipping.exe",
    "battle.net.exe", "gog galaxy.exe",
}
CREATOR_PROCESSES = {
    "ffmpeg.exe", "blender.exe", "handbrakecli.exe", "resolve.exe",
    "cargo.exe", "cmake.exe", "nvcc.exe", "julia.exe", "premiere.exe",
    "afterfx.exe",
}
MEETING_PROCESSES = {
    "zoom.exe", "teams.exe", "ms-teams.exe", "slack.exe", "discord.exe",
    "obs64.exe", "obs32.exe",
}

GPU_HEAVY_UTIL_PCT = 80
GPU_HEAVY_DURATION_S = 30


# ──────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────
def read_config() -> dict[str, str]:
    """Parse boost-auto.conf into a flat {KEY: value} dict. Missing = {}."""
    path = boost_paths.CONF_FILE
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_config_value(key: str, value: str) -> None:
    """Update one key in boost-auto.conf, creating the file if needed."""
    boost_paths.ensure_state_dir()
    path = boost_paths.CONF_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"auto: could not write {path}: {exc}", file=sys.stderr)


_presets_cache: dict[str, Any] = {}
_presets_mtime = -1.0


def load_presets() -> dict[str, Any]:
    global _presets_cache, _presets_mtime
    path = boost_paths.presets_file()
    if path is None:
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return _presets_cache
    if mtime == _presets_mtime:
        return _presets_cache
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _presets_cache
    if isinstance(data, dict):
        _presets_cache, _presets_mtime = data, mtime
    return _presets_cache


# ──────────────────────────────────────────────────────────────────────────
# AC/battery via GetSystemPowerStatus, screen lock via tasklist
# ──────────────────────────────────────────────────────────────────────────
class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def read_power_status() -> tuple[int | None, int | None]:
    """Return (ac_online, battery_pct); either may be None when unknown."""
    status = _SYSTEM_POWER_STATUS()
    try:
        ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None, None
    if not ok:
        return None, None
    ac = status.ACLineStatus if status.ACLineStatus in (0, 1) else None
    pct = status.BatteryLifePercent if 0 <= status.BatteryLifePercent <= 100 else None
    return ac, pct


_process_cache: tuple[float, set[str]] = (0.0, set())


def running_processes(poll_interval: float) -> set[str]:
    """Lower-cased image names from tasklist, cached for one poll cycle."""
    global _process_cache
    now = time.time()
    if now - _process_cache[0] < poll_interval:
        return _process_cache[1]
    names: set[str] = set()
    try:
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True,
            timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        for line in out.splitlines():
            parts = line.split('","')
            if parts:
                names.add(parts[0].strip('"').lower())
    except (OSError, subprocess.SubprocessError):
        pass
    _process_cache = (now, names)
    return names


def is_screen_locked(poll_interval: float) -> bool:
    """Heuristic: the lock screen runs as LogonUI.exe."""
    return "logonui.exe" in running_processes(poll_interval)


# ──────────────────────────────────────────────────────────────────────────
# Notifications (best effort; a failure here must never break the daemon)
# ──────────────────────────────────────────────────────────────────────────
def notify(title: str, body: str) -> None:
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$ni = New-Object System.Windows.Forms.NotifyIcon;"
        "$ni.Icon = [System.Drawing.SystemIcons]::Information;"
        "$ni.Visible = $true;"
        f"$ni.BalloonTipTitle = {json.dumps(title)};"
        f"$ni.BalloonTipText = {json.dumps(body)};"
        "$ni.ShowBalloonTip(6000);"
        "Start-Sleep -Seconds 6;"
        "$ni.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────────────────
# The daemon
# ──────────────────────────────────────────────────────────────────────────
class WindowsAutoDaemon:
    def __init__(self) -> None:
        self.backend = platform_backend.get_backend()
        self.mode = "dynamic"
        self.poll_interval = 5
        self.temp_hot = 78
        self.temp_critical = 85
        self.boost_temp_limit = 78
        self.load_high = 75
        self.load_high_duration = 120
        self.load_idle = 8
        self.load_idle_duration = 600
        self.prompt_cooldown = 900
        self.allow_critical = "yes"
        self.quiet_start = "22:00"
        self.quiet_end = "08:00"
        self.summer_nights = "no"
        self.ac_profile = "boost"
        self.battery_profile = "powersave"
        self.battery_low_pct = 20
        self.battery_critical_pct = 10
        self.battery_low_notify = "yes"
        self.screen_lock_powersave = "yes"

        self._last_conf_mtime: float = -1
        self._cached_profile: str | None = None
        self._high_since = 0
        self._idle_since = 0
        self._gpu_high_since = 0
        self._last_auto = 0
        self._last_prompt = 0
        self._last_ac_online: int | None = None
        self._battery_notified_low = False
        self._battery_notified_critical = False
        self._screen_locked = False
        self._pre_lock_profile: str | None = None
        self._meeting_notified = False
        self.last_switch_reason = ""
        self.last_switch_reason_text = ""
        self.last_switch_time = 0
        self._running = True

    # ── config ────────────────────────────────────────────────────────
    def _int(self, value: str, current: int, lo: int, hi: int) -> int:
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError):
            return current
        return n if lo <= n <= hi else current

    def load_config(self) -> None:
        cfg = read_config()

        def choice(key: str, current: str, allowed: set[str]) -> str:
            value = cfg.get(key)
            return value if value in allowed else current

        self.mode = choice("AUTO_MODE", self.mode, AUTO_MODES)
        preset = load_presets().get(self.mode)
        if isinstance(preset, dict):
            self.temp_hot = preset.get("tempHot", self.temp_hot)
            self.boost_temp_limit = preset.get("boostTempLimit", self.boost_temp_limit)
            self.load_high = preset.get("loadHigh", self.load_high)
            self.load_high_duration = preset.get("loadHighDuration", self.load_high_duration)
            self.load_idle = preset.get("loadIdle", self.load_idle)
            self.load_idle_duration = preset.get("loadIdleDuration", self.load_idle_duration)
            self.prompt_cooldown = preset.get("promptCooldown", self.prompt_cooldown)

        if "TEMP_HOT" in cfg: self.temp_hot = self._int(cfg["TEMP_HOT"], self.temp_hot, 40, 100)
        if "TEMP_CRITICAL" in cfg: self.temp_critical = self._int(cfg["TEMP_CRITICAL"], self.temp_critical, 50, 110)
        if "BOOST_TEMP_LIMIT" in cfg: self.boost_temp_limit = self._int(cfg["BOOST_TEMP_LIMIT"], self.boost_temp_limit, 40, 100)
        if "LOAD_HIGH" in cfg: self.load_high = self._int(cfg["LOAD_HIGH"], self.load_high, 1, 100)
        if "LOAD_HIGH_DURATION" in cfg: self.load_high_duration = self._int(cfg["LOAD_HIGH_DURATION"], self.load_high_duration, 5, 86400)
        if "LOAD_IDLE" in cfg: self.load_idle = self._int(cfg["LOAD_IDLE"], self.load_idle, 0, 100)
        if "LOAD_IDLE_DURATION" in cfg: self.load_idle_duration = self._int(cfg["LOAD_IDLE_DURATION"], self.load_idle_duration, 5, 86400)
        if "PROMPT_COOLDOWN" in cfg: self.prompt_cooldown = self._int(cfg["PROMPT_COOLDOWN"], self.prompt_cooldown, 0, 86400)
        if "POLL_INTERVAL" in cfg: self.poll_interval = self._int(cfg["POLL_INTERVAL"], self.poll_interval, 1, 3600)
        if "BATTERY_LOW_PCT" in cfg: self.battery_low_pct = self._int(cfg["BATTERY_LOW_PCT"], self.battery_low_pct, 1, 100)
        if "BATTERY_CRITICAL_PCT" in cfg: self.battery_critical_pct = self._int(cfg["BATTERY_CRITICAL_PCT"], self.battery_critical_pct, 1, 100)

        self.allow_critical = choice("ALLOW_CRITICAL_AUTO", self.allow_critical, YES_NO)
        self.summer_nights = choice("SUMMER_SILENT_NIGHTS", self.summer_nights, YES_NO)
        self.battery_low_notify = choice("BATTERY_LOW_NOTIFY", self.battery_low_notify, YES_NO)
        self.screen_lock_powersave = choice("SCREEN_LOCK_POWERSAVE", self.screen_lock_powersave, YES_NO)
        self.ac_profile = choice("AC_PROFILE", self.ac_profile, {"boost", "powersave", "silent", "restore"})
        self.battery_profile = choice("BATTERY_PROFILE", self.battery_profile, {"boost", "powersave", "silent", "restore"})

        for key, attr in (("QUIET_HOURS_START", "quiet_start"), ("QUIET_HOURS_END", "quiet_end")):
            value = cfg.get(key, "")
            if re.fullmatch(r"\d{2}:\d{2}", value):
                setattr(self, attr, value)

    # ── helpers ──────────────────────────────────────────────────────
    def apply(self, profile: str, reason_code: str, reason_text: str) -> None:
        self.backend.apply_profile(profile)
        self._cached_profile = profile
        self.last_switch_reason = reason_code
        self.last_switch_reason_text = reason_text
        self.last_switch_time = int(time.time())
        log(f"{reason_code}: {reason_text}")

    def current_profile(self) -> str:
        # apply_profile() already tells us what we asked for; power_profile()
        # would need to reverse-map powercfg's own labels, which is lossy.
        return self._cached_profile or "unknown"

    def in_quiet_hours(self) -> bool:
        if self.quiet_start == self.quiet_end:
            return False
        now = datetime.now()
        now_m = now.hour * 60 + now.minute
        sh, sm = map(int, self.quiet_start.split(":"))
        eh, em = map(int, self.quiet_end.split(":"))
        start_m, end_m = sh * 60 + sm, eh * 60 + em
        return (start_m <= now_m < end_m) if start_m < end_m else (now_m >= start_m or now_m < end_m)

    def suggestions_paused(self) -> bool:
        return self.mode in ("quiet", "off") or self.in_quiet_hours()

    # ── one poll tick ────────────────────────────────────────────────
    def tick(self) -> None:
        try:
            conf_mtime = boost_paths.CONF_FILE.stat().st_mtime
        except OSError:
            conf_mtime = 0
        if conf_mtime != self._last_conf_mtime:
            self.load_config()
            self._last_conf_mtime = conf_mtime

        now = int(time.time())
        temp = self.backend.get_cpu_temp()
        load = self.backend.get_cpu_load()
        gpu = self.backend.get_gpu_stats()
        procs = running_processes(self.poll_interval)
        is_game = bool(procs & GAME_PROCESSES)
        is_creator = bool(procs & CREATOR_PROCESSES)
        is_meeting = bool(procs & MEETING_PROCESSES)
        profile = self.current_profile()

        try:
            gpu_util = int(float(gpu.get("util") or 0))
        except ValueError:
            gpu_util = 0
        if gpu_util >= GPU_HEAVY_UTIL_PCT:
            if self._gpu_high_since == 0:
                self._gpu_high_since = now
        else:
            self._gpu_high_since = 0
        is_gpu_heavy = self._gpu_high_since != 0 and now - self._gpu_high_since >= GPU_HEAVY_DURATION_S

        ac_online, battery_pct = read_power_status()
        write_live_snapshot(self, temp, load, profile, gpu, ac_online, battery_pct)

        if self.mode == "off":
            return

        # AC plug/unplug
        if ac_online is not None and ac_online != self._last_ac_online:
            first = self._last_ac_online is None
            self._last_ac_online = ac_online
            if not first:
                if ac_online == 1:
                    self.apply(self.ac_profile, "ac-connected", f"AC plugged in. Applied the {self.ac_profile} profile.")
                    notify("AC Power Connected", f"Switched to {self.ac_profile} profile.")
                else:
                    self.apply(self.battery_profile, "on-battery", f"Unplugged from AC. Applied the {self.battery_profile} profile.")
                    notify("On Battery", f"Switched to {self.battery_profile} profile.")
            self._battery_notified_low = False
            self._battery_notified_critical = False

        # Battery thresholds
        if ac_online == 0 and battery_pct is not None:
            if battery_pct <= self.battery_critical_pct and not self._battery_notified_critical:
                self._battery_notified_critical = True
                self._battery_notified_low = True
                if profile != "powersave":
                    self.apply("powersave", "battery-critical", f"Battery critical at {battery_pct}%. Forced powersave.")
                if self.battery_low_notify == "yes":
                    notify("Battery Critical", f"Only {battery_pct}% remaining. Maximum power saving.")
            elif battery_pct <= self.battery_low_pct and not self._battery_notified_low:
                self._battery_notified_low = True
                if self.battery_low_notify == "yes":
                    notify("Battery Low", f"{battery_pct}% remaining. Consider plugging in your charger.")
            if battery_pct > self.battery_low_pct:
                self._battery_notified_low = False
            if battery_pct > self.battery_critical_pct:
                self._battery_notified_critical = False
        elif ac_online == 1:
            self._battery_notified_low = False
            self._battery_notified_critical = False

        # Screen lock
        if self.screen_lock_powersave == "yes":
            locked = is_screen_locked(self.poll_interval)
            if locked and not self._screen_locked:
                self._screen_locked = True
                self._pre_lock_profile = profile
                if profile != "powersave":
                    self.apply("powersave", "screen-lock", "Screen locked. Switched to powersave.")
            elif not locked and self._screen_locked:
                self._screen_locked = False
                if self._pre_lock_profile == "boost":
                    self.apply("boost", "screen-unlock", "Screen unlocked. Restored the performance profile.")
                self._pre_lock_profile = None

        # Summer quiet-hours auto-silent
        if self.summer_nights == "yes" and self.in_quiet_hours() and profile != "silent":
            if now - self._last_auto > 3600:
                self.apply("silent", "summer-quiet-hours", "Summer quiet hours are active. Switched to Silent.")
                notify("Summer night mode", "Quiet hours are active, so Auto applied Silent mode.")
                self._last_auto = now
                self._last_prompt = now
                return

        # Game detected -> boost
        if is_game and profile != "boost" and temp < self.boost_temp_limit:
            if now - self._last_auto > 60:
                self.apply("boost", "game-detected", "Game detected. Switched to maximum performance.")
                notify("Game Mode Enabled", "Detected a game running. Switched to maximum performance.")
                self._last_auto = now
                return

        # Creator/GPU-heavy workload -> suggest boost
        if (is_creator or is_gpu_heavy) and not is_game and profile != "boost" and temp < self.boost_temp_limit:
            if now - self._last_prompt > self.prompt_cooldown:
                notify("Heavy workload detected", "Rendering or compilation in progress. Enable Boost for faster results.")
                self._last_prompt = now

        # Meeting detection
        if is_meeting and not self._meeting_notified and profile == "boost":
            self._meeting_notified = True
            if ac_online == 0:
                self.apply("powersave", "meeting-on-battery", "Video call detected on battery. Switched to powersave.")
                notify("Video call - Eco Mode", "Switched to Eco Mode to keep fans quiet and save battery during your call.")
            else:
                notify("Video call detected", "Consider switching to Balanced mode to reduce fan noise during your call.")
        elif not is_meeting:
            self._meeting_notified = False

        # Critical heat
        if temp >= self.temp_critical and profile == "boost" and self.allow_critical == "yes":
            if now - self._last_auto > 120:
                self.apply("powersave", "critical-heat", f"CPU hit {temp}C. Forced powersave to protect hardware.")
                notify("Critical Heat Warning", f"CPU reached {temp}C. Switched to cooler mode to protect hardware.")
                self._last_auto = now
                self._last_prompt = now

        if self.suggestions_paused():
            return

        # Hot warning
        if temp >= self.temp_hot and profile == "boost":
            if now - self._last_prompt > self.prompt_cooldown:
                notify("The computer is getting warm", "Consider switching to a cooler mode.")
                self._last_prompt = now

        # High load warning
        elif load >= self.load_high and not is_game:
            if self._high_since == 0:
                self._high_since = now
            elif now - self._high_since >= self.load_high_duration and profile != "boost" and temp < self.boost_temp_limit:
                if now - self._last_prompt > self.prompt_cooldown:
                    notify("It looks like you need more power", "Consider enabling Boost for heavy work.")
                    self._last_prompt = now
                    self._high_since = 0
            self._idle_since = 0

        # Idle warning
        elif load <= self.load_idle and not is_game:
            if self._idle_since == 0:
                self._idle_since = now
            elif now - self._idle_since >= self.load_idle_duration and profile == "boost":
                if now - self._last_prompt > self.prompt_cooldown:
                    notify("The PC looks quiet now", "Consider leaving Boost to reduce heat.")
                    self._last_prompt = now
                    self._idle_since = 0
            self._high_since = 0
        else:
            self._high_since = 0
            self._idle_since = 0

    def loop(self) -> None:
        log("Windows auto daemon started")
        while self._running:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - one bad tick must not kill the daemon
                log(f"Poll cycle error (recovered): {exc!r}")
            time.sleep(self.poll_interval)

    def stop(self, *_args: Any) -> None:
        self._running = False
        log("Windows auto daemon stopping")
        raise SystemExit(0)


def write_live_snapshot(
    daemon: WindowsAutoDaemon, temp: int, load: int, profile: str,
    gpu: dict[str, str], ac_online: int | None, battery_pct: int | None,
) -> None:
    try:
        boost_paths.ensure_state_dir()
        snapshot = {
            "time": int(time.time()),
            "cpu": {"temp": temp, "load": load},
            "gpu": {
                "temp": gpu.get("temp", ""), "power": gpu.get("power", ""),
                "limit": gpu.get("limit", ""), "util": gpu.get("util", ""),
            },
            "profile": profile,
            "mode": daemon.mode,
            "battery": {"acOnline": ac_online, "pct": battery_pct},
            "sensors": daemon.backend.get_sensors(),
            "fans": {"enabled": False, "supported": False, "fans": []},
            "interlock": {
                "silentBlocked": False, "reason": "", "pending": False, "pendingSince": 0,
                "thresholds": {
                    "tempHot": daemon.temp_hot, "tempCritical": daemon.temp_critical,
                    "boostTempLimit": daemon.boost_temp_limit, "loadHigh": daemon.load_high,
                },
            },
            "lastSwitch": {
                "reason": daemon.last_switch_reason, "text": daemon.last_switch_reason_text,
                "time": daemon.last_switch_time,
            },
        }
        tmp = boost_paths.LIVE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot), encoding="utf-8")
        tmp.replace(boost_paths.LIVE_FILE)
    except OSError as exc:
        log(f"Error writing live snapshot: {exc}")


def log(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    print(line, flush=True)
    try:
        boost_paths.ensure_state_dir()
        with open(boost_paths.AUTO_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────────────────
# Process management: no systemd, so `auto start/stop/status/logs` manage a
# plain child process directly.
# ──────────────────────────────────────────────────────────────────────────
def _read_pid() -> int | None:
    try:
        return int(boost_paths.AUTO_PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    return True


def cmd_start(argv: list[str]) -> int:
    pid = _read_pid()
    if pid and _pid_alive(pid):
        print(f"auto: already running (pid {pid}).")
        return 0
    exe = sys.executable if getattr(sys, "frozen", False) else None
    if exe:
        cmd = [exe, "auto", "_run"]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve().parent.parent / "bin" / "boost.py"), "auto", "_run"]
    boost_paths.ensure_state_dir()
    # PyInstaller's onefile bootloader (6.x) does its own anti-hijack check on
    # startup: it walks up to its parent process and re-verifies its image
    # path. DETACHED_PROCESS/CREATE_NEW_PROCESS_GROUP make Windows tear down
    # that parent linkage (or this process exits before the child finishes
    # the check), so the child's bootloader fails with "Security validation
    # failure: failed to obtain executable path for parent proces". Plain
    # CREATE_NO_WINDOW keeps the parent link intact; staying alive for a beat
    # after Popen gives the child's bootloader time to finish that check
    # before this short-lived "auto start" process exits.
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with open(boost_paths.AUTO_LOG_FILE, "a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            cmd, stdout=log_file, stderr=log_file, stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )
    boost_paths.AUTO_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    time.sleep(1.5)
    print(f"auto: started (pid {proc.pid}). Logs: {boost_paths.AUTO_LOG_FILE}")
    return 0


def cmd_stop(argv: list[str]) -> int:
    pid = _read_pid()
    if not pid or not _pid_alive(pid):
        print("auto: not running.")
        try:
            boost_paths.AUTO_PID_FILE.unlink()
        except OSError:
            pass
        return 0
    # The daemon is a console process with no message loop, so a plain
    # taskkill (which asks nicely, like clicking a window's close button)
    # does nothing to it; /F sends the real termination signal.
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 and _pid_alive(pid):
        print(f"auto: could not stop pid {pid}: {result.stderr.strip() or result.stdout.strip()}", file=sys.stderr)
        return 1
    try:
        boost_paths.AUTO_PID_FILE.unlink()
    except OSError:
        pass
    print("auto: stopped.")
    return 0


def cmd_status(argv: list[str]) -> int:
    pid = _read_pid()
    running = bool(pid and _pid_alive(pid))
    print(f"Daemon        : {'running (pid ' + str(pid) + ')' if running else 'stopped'}")
    try:
        data = json.loads(boost_paths.LIVE_FILE.read_text(encoding="utf-8"))
        age = time.time() - boost_paths.LIVE_FILE.stat().st_mtime
        print(f"Mode          : {data.get('mode', 'unknown')}")
        print(f"Profile       : {data.get('profile', 'unknown')}")
        cpu = data.get("cpu", {})
        print(f"CPU           : {cpu.get('temp', 'n/a')}°C, {cpu.get('load', 'n/a')}% load")
        print(f"Last update   : {age:.0f}s ago")
        last = data.get("lastSwitch", {})
        if last.get("text"):
            print(f"Last switch   : {last['text']}")
    except (OSError, ValueError):
        print("Live status   : unavailable (daemon has not ticked yet)")
    return 0


def cmd_logs(argv: list[str]) -> int:
    try:
        lines = boost_paths.AUTO_LOG_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        print("auto: no log file yet.")
        return 0
    for line in lines[-200:]:
        print(line)
    return 0


def cmd_run(argv: list[str]) -> int:
    """Internal: run the poll loop in the foreground (spawned by `auto start`)."""
    daemon = WindowsAutoDaemon()
    signal.signal(signal.SIGTERM, daemon.stop)
    try:
        signal.signal(signal.SIGINT, daemon.stop)
    except (ValueError, OSError):
        pass
    daemon.loop()
    return 0


def cmd_mode(argv: list[str]) -> int:
    if not argv or argv[0] not in AUTO_MODES:
        print(f"auto: mode must be one of {sorted(AUTO_MODES)}", file=sys.stderr)
        return 1
    write_config_value("AUTO_MODE", argv[0])
    print(f"auto: mode set to {argv[0]}.")
    return 0


def cmd_snooze(argv: list[str]) -> int:
    durations = {"30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400}
    if not argv or argv[0] not in durations:
        print("auto: snooze needs one of 30m, 1h, 2h, 4h", file=sys.stderr)
        return 1
    boost_paths.ensure_state_dir()
    until = int(time.time()) + durations[argv[0]]
    boost_paths.SNOOZE_FILE.write_text(str(until), encoding="utf-8")
    print(f"auto: suggestions snoozed for {argv[0]}.")
    return 0


def cmd_today_off(argv: list[str]) -> int:
    boost_paths.ensure_state_dir()
    boost_paths.SKIP_TODAY_FILE.write_text(datetime.now().strftime("%Y-%m-%d"), encoding="utf-8")
    print("auto: suggestions off for today.")
    return 0


def cmd_resume(argv: list[str]) -> int:
    for path in (boost_paths.SNOOZE_FILE, boost_paths.SKIP_TODAY_FILE):
        try:
            path.unlink()
        except OSError:
            pass
    print("auto: suggestions resumed.")
    return 0


def cmd_quiet_hours(argv: list[str]) -> int:
    if len(argv) != 2 or not all(re.fullmatch(r"\d{2}:\d{2}", a) for a in argv):
        print("auto: quiet-hours needs two HH:MM values", file=sys.stderr)
        return 1
    write_config_value("QUIET_HOURS_START", argv[0])
    write_config_value("QUIET_HOURS_END", argv[1])
    print(f"auto: quiet hours set to {argv[0]}-{argv[1]}.")
    return 0


def cmd_summer_nights(argv: list[str]) -> int:
    if not argv or argv[0] not in ("on", "off"):
        print("auto: summer-nights needs on or off", file=sys.stderr)
        return 1
    write_config_value("SUMMER_SILENT_NIGHTS", "yes" if argv[0] == "on" else "no")
    print(f"auto: summer nights {'enabled' if argv[0] == 'on' else 'disabled'}.")
    return 0


def cmd_report(argv: list[str]) -> int:
    print("auto: HTML history reports are not available on Windows yet.")
    return 1


COMMANDS = {
    "start": cmd_start, "stop": cmd_stop, "status": cmd_status, "logs": cmd_logs,
    "_run": cmd_run, "mode": cmd_mode, "snooze": cmd_snooze, "today-off": cmd_today_off,
    "resume": cmd_resume, "quiet-hours": cmd_quiet_hours, "summer-nights": cmd_summer_nights,
    "report": cmd_report,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(f"auto: usage: boost auto {{{'|'.join(k for k in COMMANDS if not k.startswith('_'))}}}", file=sys.stderr)
        return 1
    return COMMANDS[argv[0]](argv[1:])
