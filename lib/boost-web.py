"""Local web dashboard for boost.

The server binds to 127.0.0.1 by default and uses only Python stdlib.
It is intended to run as root through systemd so profile buttons can call
the existing boost/powersave/auto commands.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import boost_paths
import platform_backend

try:
    import sensors as sensor_layer
    import fancontrol
except ImportError:  # pragma: no cover - partial install
    sensor_layer = None
    fancontrol = None


HOST = "127.0.0.1"
PORT = 8765

# Paths come from boost_paths so the same server runs on Windows, where there
# is no /etc and no /var. On Linux these resolve to exactly the values they
# have always had.
CONF_FILE = boost_paths.CONF_FILE
STATS_FILE = boost_paths.STATS_FILE
LATEST_REPORT = boost_paths.LATEST_REPORT
SNOOZE_FILE = boost_paths.SNOOZE_FILE
SKIP_TODAY_FILE = boost_paths.SKIP_TODAY_FILE
LIVE_FILE = boost_paths.LIVE_FILE
LIVE_FRESH_SECONDS = 10

# The backend knows what this platform can actually do. On Linux it reads
# sysfs natively, so the hand-tuned readers further down stay in charge; on
# any other platform they hand over to the backend instead.
BACKEND = platform_backend.get_backend()
NATIVE_SYSFS = BACKEND.reads_sysfs


# ──────────────────────────────────────────────────────────────────────────
# Process + filesystem helpers
# ──────────────────────────────────────────────────────────────────────────
def run(cmd: list[str], timeout: float = 4.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)


def read_text(path: str | Path, default: str = "unknown") -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return default


# ──────────────────────────────────────────────────────────────────────────
# Configuration: /etc/boost-auto.conf read, validate, write
# ──────────────────────────────────────────────────────────────────────────
_CONFIG_CACHE_MTIME: float = -1
_CONFIG_CACHE_DATA: dict[str, str] = {}
_CONFIG_LOCK = threading.Lock()

def read_config() -> dict[str, str]:
    global _CONFIG_CACHE_MTIME, _CONFIG_CACHE_DATA
    try:
        current_mtime = CONF_FILE.stat().st_mtime
    except OSError:
        current_mtime = 0
        
    with _CONFIG_LOCK:
        if current_mtime != 0 and current_mtime == _CONFIG_CACHE_MTIME:
            return _CONFIG_CACHE_DATA.copy()
            
        config: dict[str, str] = {}
        if current_mtime != 0:
            try:
                for line in CONF_FILE.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    config[key.strip()] = value.strip()
            except OSError:
                pass
                
        _CONFIG_CACHE_MTIME = current_mtime
        _CONFIG_CACHE_DATA = config
        return config.copy()


def write_config(updates: dict[str, str]) -> bool:
    """Update config file with new key=value pairs, preserving comments and order."""
    try:
        if not CONF_FILE.exists():
            CONF_FILE.write_text("")
        file_lines = CONF_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return False

    updated_keys = set(updates.keys())
    new_lines = []
    for line in file_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}\n")
                updated_keys.discard(key)
                continue
        new_lines.append(line)
    for key in updated_keys:
        new_lines.append(f"{key}={updates[key]}\n")
    try:
        CONF_FILE.write_text("".join(new_lines), encoding="utf-8")
        return True
    except OSError:
        return False


def config_payload() -> dict[str, Any]:
    """Return all config keys and their descriptions for the config UI."""
    config = read_config()
    return {
        "ok": True,
        "config": {
            "AUTO_MODE": config.get("AUTO_MODE", "dynamic"),
            "TEMP_CRITICAL": config.get("TEMP_CRITICAL", "85"),
            "TEMP_HOT": config.get("TEMP_HOT", "78"),
            "BOOST_TEMP_LIMIT": config.get("BOOST_TEMP_LIMIT", "78"),
            "LOAD_HIGH": config.get("LOAD_HIGH", "75"),
            "LOAD_HIGH_DURATION": config.get("LOAD_HIGH_DURATION", "120"),
            "LOAD_IDLE": config.get("LOAD_IDLE", "8"),
            "LOAD_IDLE_DURATION": config.get("LOAD_IDLE_DURATION", "600"),
            "PROMPT_COOLDOWN": config.get("PROMPT_COOLDOWN", "900"),
            "QUIET_HOURS_START": config.get("QUIET_HOURS_START", "22:00"),
            "QUIET_HOURS_END": config.get("QUIET_HOURS_END", "08:00"),
            "SUMMER_SILENT_NIGHTS": config.get("SUMMER_SILENT_NIGHTS", "no"),
            "ALLOW_CRITICAL_AUTO": config.get("ALLOW_CRITICAL_AUTO", "yes"),
            "POLL_INTERVAL": config.get("POLL_INTERVAL", "5"),
            "STATS_INTERVAL": config.get("STATS_INTERVAL", "60"),
            "AC_PROFILE": config.get("AC_PROFILE", "restore"),
            "BATTERY_PROFILE": config.get("BATTERY_PROFILE", "powersave"),
            "BATTERY_LOW_PCT": config.get("BATTERY_LOW_PCT", "20"),
            "BATTERY_CRITICAL_PCT": config.get("BATTERY_CRITICAL_PCT", "10"),
            "BATTERY_LOW_NOTIFY": config.get("BATTERY_LOW_NOTIFY", "yes"),
            "BOOST_EPP": config.get("BOOST_EPP", "balance_performance"),
            "BOOST_PL1_PCT": config.get("BOOST_PL1_PCT", "100"),
            "BOOST_PL2_PCT": config.get("BOOST_PL2_PCT", "80"),
            "SCREEN_LOCK_POWERSAVE": config.get("SCREEN_LOCK_POWERSAVE", "yes"),
            "BATTERY_CHARGE_LIMIT": config.get("BATTERY_CHARGE_LIMIT", "0"),
            "SLOW_CHARGE_THRESHOLD_W": config.get("SLOW_CHARGE_THRESHOLD_W", "2"),
            "SLOW_CHARGE_BATTERY_PCT": config.get("SLOW_CHARGE_BATTERY_PCT", "25"),
            "SLOW_CHARGE_RECOVERY_PCT": config.get("SLOW_CHARGE_RECOVERY_PCT", "35"),
            "GPU_PL_BOOST_W": config.get("GPU_PL_BOOST_W", ""),
            "GPU_PL_POWERSAVE_W": config.get("GPU_PL_POWERSAVE_W", ""),
            "GPU_PL_SILENT_W": config.get("GPU_PL_SILENT_W", ""),
        },
    }


CONFIG_SCHEMA: dict[str, dict[str, Any]] = {
    "TEMP_CRITICAL": {"type": "int", "min": 50, "max": 110},
    "TEMP_HOT": {"type": "int", "min": 40, "max": 100},
    "BOOST_TEMP_LIMIT": {"type": "int", "min": 40, "max": 100},
    "LOAD_HIGH": {"type": "int", "min": 1, "max": 100},
    "LOAD_HIGH_DURATION": {"type": "int", "min": 5, "max": 86400},
    "LOAD_IDLE": {"type": "int", "min": 0, "max": 100},
    "LOAD_IDLE_DURATION": {"type": "int", "min": 5, "max": 86400},
    "PROMPT_COOLDOWN": {"type": "int", "min": 0, "max": 86400},
    "QUIET_HOURS_START": {"type": "hhmm"},
    "QUIET_HOURS_END": {"type": "hhmm"},
    "SUMMER_SILENT_NIGHTS": {"type": "choice", "values": {"yes", "no"}},
    "ALLOW_CRITICAL_AUTO": {"type": "choice", "values": {"yes", "no"}},
    "POLL_INTERVAL": {"type": "int", "min": 1, "max": 3600},
    "STATS_INTERVAL": {"type": "int", "min": 10, "max": 86400},
    "AC_PROFILE": {"type": "choice", "values": {"boost", "powersave", "silent", "restore"}},
    "BATTERY_PROFILE": {"type": "choice", "values": {"boost", "powersave", "silent", "restore"}},
    "BATTERY_LOW_PCT": {"type": "int", "min": 1, "max": 100},
    "BATTERY_CRITICAL_PCT": {"type": "int", "min": 1, "max": 100},
    "BATTERY_LOW_NOTIFY": {"type": "choice", "values": {"yes", "no"}},
    "BOOST_EPP": {"type": "choice", "values": {"performance", "balance_performance"}},
    "BOOST_PL1_PCT": {"type": "int", "min": 40, "max": 100},
    "BOOST_PL2_PCT": {"type": "int", "min": 40, "max": 100},
    "SCREEN_LOCK_POWERSAVE": {"type": "choice", "values": {"yes", "no"}},
    "BATTERY_CHARGE_LIMIT": {"type": "int", "min": 0, "max": 100},
    "SLOW_CHARGE_THRESHOLD_W": {"type": "float", "min": 0, "max": 200},
    "SLOW_CHARGE_BATTERY_PCT": {"type": "int", "min": 1, "max": 100},
    "SLOW_CHARGE_RECOVERY_PCT": {"type": "int", "min": 1, "max": 100},
    # Empty means "scale the limit with the profile" (the pre-1.9 behaviour).
    "GPU_PL_BOOST_W": {"type": "watts"},
    "GPU_PL_POWERSAVE_W": {"type": "watts"},
    "GPU_PL_SILENT_W": {"type": "watts"},
}


def validate_config_updates(updates: dict[str, Any]) -> tuple[dict[str, str], str | None]:
    sanitized: dict[str, str] = {}
    current_config = read_config()
    for key, raw_value in updates.items():
        spec = CONFIG_SCHEMA.get(key)
        if spec is None:
            return {}, f"Unknown config key: {key}"

        value = str(raw_value).strip()
        if spec["type"] == "int":
            if not re.fullmatch(r"[0-9]+", value):
                return {}, f"{key} must be a whole number."
            number = int(value)
            if number < spec["min"] or number > spec["max"]:
                return {}, f"{key} must be between {spec['min']} and {spec['max']}."
            sanitized[key] = str(number)
        elif spec["type"] == "hhmm":
            if not valid_hhmm(value):
                return {}, f"{key} must use HH:MM."
            sanitized[key] = value
        elif spec["type"] == "choice":
            if value not in spec["values"]:
                allowed = ", ".join(sorted(spec["values"]))
                return {}, f"{key} must be one of: {allowed}."
            sanitized[key] = value
        elif spec["type"] == "watts":
            if value in ("", "auto", "0"):
                sanitized[key] = ""
                continue
            if not re.fullmatch(r"[0-9]+", value):
                return {}, f"{key} must be a whole number of watts, or empty for automatic."
            min_w, max_w = _gpu_limit_range()
            number = int(value)
            if max_w and not min_w <= number <= max_w:
                return {}, f"{key} must be between {min_w} and {max_w} W (the driver's own range)."
            sanitized[key] = str(number)
        elif spec["type"] == "float":
            try:
                number = float(value)
            except ValueError:
                return {}, f"{key} must be a number."
            if number < spec["min"] or number > spec["max"]:
                return {}, f"{key} must be between {spec['min']} and {spec['max']}."
            sanitized[key] = str(number)

    low = int(sanitized.get("BATTERY_LOW_PCT", str(number_config(current_config, "BATTERY_LOW_PCT", 20))))
    critical = int(sanitized.get("BATTERY_CRITICAL_PCT", str(number_config(current_config, "BATTERY_CRITICAL_PCT", 10))))
    if critical > low:
        return {}, "BATTERY_CRITICAL_PCT cannot be higher than BATTERY_LOW_PCT."

    temp_critical = int(sanitized.get("TEMP_CRITICAL", str(number_config(current_config, "TEMP_CRITICAL", 85))))
    temp_hot = int(sanitized.get("TEMP_HOT", str(number_config(current_config, "TEMP_HOT", 78))))
    boost_limit = int(sanitized.get("BOOST_TEMP_LIMIT", str(number_config(current_config, "BOOST_TEMP_LIMIT", 78))))
    if temp_hot > temp_critical:
        return {}, "TEMP_HOT cannot be higher than TEMP_CRITICAL."
    if boost_limit > temp_critical:
        return {}, "BOOST_TEMP_LIMIT cannot be higher than TEMP_CRITICAL."

    slow_charge_pct = int(sanitized.get("SLOW_CHARGE_BATTERY_PCT", str(number_config(current_config, "SLOW_CHARGE_BATTERY_PCT", 25))))
    slow_recovery_pct = int(sanitized.get("SLOW_CHARGE_RECOVERY_PCT", str(number_config(current_config, "SLOW_CHARGE_RECOVERY_PCT", 35))))
    if slow_recovery_pct <= slow_charge_pct:
        return {}, "SLOW_CHARGE_RECOVERY_PCT must be higher than SLOW_CHARGE_BATTERY_PCT."

    return sanitized, None


# ──────────────────────────────────────────────────────────────────────────
# Mode presets and effective thresholds
# ──────────────────────────────────────────────────────────────────────────
DEFAULT_THRESHOLDS = {
    "tempCritical": 85,
    "tempHot": 78,
    "boostTempLimit": 78,
    "loadHigh": 75,
    "loadHighDuration": 120,
    "loadIdle": 8,
    "loadIdleDuration": 600,
    "promptCooldown": 900,
}

# Canonical mode presets, shared with lib/boost-daemon.py and bin/auto so the
# three implementations can never drift apart (see CHANGELOG v1.2.0).
PRESETS_FILE_CANDIDATES = [
    Path("/usr/local/share/boost/presets.json"),
    Path(__file__).resolve().parent.parent / "config" / "presets.json",
]

_PRESETS_CACHE: dict[str, Any] = {}
_PRESETS_CACHE_MTIME: float = -1
_PRESETS_CACHE_PATH: Path | None = None
_PRESETS_LOCK = threading.Lock()

def load_presets() -> dict[str, Any]:
    global _PRESETS_CACHE, _PRESETS_CACHE_MTIME, _PRESETS_CACHE_PATH
    with _PRESETS_LOCK:
        path = _PRESETS_CACHE_PATH if _PRESETS_CACHE_PATH and _PRESETS_CACHE_PATH.is_file() else None
        if path is None:
            path = next((candidate for candidate in PRESETS_FILE_CANDIDATES if candidate.is_file()), None)
        if path is None:
            _PRESETS_CACHE, _PRESETS_CACHE_MTIME, _PRESETS_CACHE_PATH = {}, -1, None
            return {}
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return _PRESETS_CACHE
        if mtime == _PRESETS_CACHE_MTIME and _PRESETS_CACHE_PATH == path:
            return _PRESETS_CACHE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _PRESETS_CACHE
        if not isinstance(data, dict):
            return _PRESETS_CACHE
        _PRESETS_CACHE, _PRESETS_CACHE_MTIME, _PRESETS_CACHE_PATH = data, mtime, path
        return data


def number_config(config: dict[str, str], key: str, default: int) -> int:
    try:
        return int(float(config.get(key, str(default)) or default))
    except ValueError:
        return default


def mode_thresholds(mode: str, config: dict[str, str] | None = None) -> dict[str, int | str]:
    thresholds: dict[str, int | str] = dict(DEFAULT_THRESHOLDS)
    presets = load_presets()
    if mode in presets and isinstance(presets[mode], dict):
        thresholds.update(presets[mode])
    elif mode == "custom" and config:
        thresholds.update(
            {
                "tempCritical": number_config(config, "TEMP_CRITICAL", 85),
                "tempHot": number_config(config, "TEMP_HOT", 78),
                "boostTempLimit": number_config(config, "BOOST_TEMP_LIMIT", 78),
                "loadHigh": number_config(config, "LOAD_HIGH", 75),
                "loadHighDuration": number_config(config, "LOAD_HIGH_DURATION", 120),
                "loadIdle": number_config(config, "LOAD_IDLE", 8),
                "loadIdleDuration": number_config(config, "LOAD_IDLE_DURATION", 600),
                "promptCooldown": number_config(config, "PROMPT_COOLDOWN", 900),
            }
        )
    thresholds["mode"] = mode
    return thresholds


# ──────────────────────────────────────────────────────────────────────────
# Ambient temperature and quiet hours
# ──────────────────────────────────────────────────────────────────────────
_AMBIENT_CACHE_VAL = None
_AMBIENT_CACHE_TIME = 0
_AMBIENT_LOCK = threading.Lock()

def ambient_temp(config: dict[str, str]) -> dict[str, Any]:
    global _AMBIENT_CACHE_VAL, _AMBIENT_CACHE_TIME
    with _AMBIENT_LOCK:
        now = time.time()
        if _AMBIENT_CACHE_VAL is not None and now - _AMBIENT_CACHE_TIME < 600:
            return _AMBIENT_CACHE_VAL

    value = config.get("AMBIENT_TEMP_C", "").strip()
    if value:
        try:
            res = {"detected": True, "temp": int(float(value)), "source": "AMBIENT_TEMP_C"}
            with _AMBIENT_LOCK:
                _AMBIENT_CACHE_VAL, _AMBIENT_CACHE_TIME = res, time.time()
            return res
        except ValueError:
            pass

    temp_file = config.get("AMBIENT_TEMP_FILE", "").strip()
    if temp_file and Path(temp_file).is_file():
        raw = read_text(temp_file, "")
        try:
            parsed = int(float(raw))
            res = {"detected": True, "temp": parsed // 1000 if parsed > 200 else parsed, "source": temp_file}
            with _AMBIENT_LOCK:
                _AMBIENT_CACHE_VAL, _AMBIENT_CACHE_TIME = res, time.time()
            return res
        except ValueError:
            pass

    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        for label_file in hwmon.glob("temp*_label"):
            label = read_text(label_file, "").lower()
            if not any(part in label for part in ("ambient", "room", "system", "motherboard", "systin")):
                continue
            raw = int(read_text(str(label_file).replace("_label", "_input"), "0") or "0")
            if raw > 0:
                res = {"detected": True, "temp": raw // 1000, "source": f"{hwmon.name}:{label}"}
                with _AMBIENT_LOCK:
                    _AMBIENT_CACHE_VAL, _AMBIENT_CACHE_TIME = res, time.time()
                return res

    res = {"detected": False, "temp": None, "source": "not detected"}
    with _AMBIENT_LOCK:
        _AMBIENT_CACHE_VAL, _AMBIENT_CACHE_TIME = res, time.time()
    return res


def apply_ambient_adjustment(thresholds: dict[str, int | str], ambient: dict[str, Any]) -> dict[str, int | str]:
    adjusted = dict(thresholds)
    if adjusted.get("mode") != "summer" or not ambient.get("detected"):
        return adjusted
    temp = int(ambient.get("temp") or 0)
    if temp >= 30:
        adjusted["tempCritical"] = int(adjusted["tempCritical"]) - 2
        adjusted["tempHot"] = int(adjusted["tempHot"]) - 2
        adjusted["boostTempLimit"] = int(adjusted["boostTempLimit"]) - 3
    elif temp >= 28:
        adjusted["tempCritical"] = int(adjusted["tempCritical"]) - 1
        adjusted["tempHot"] = int(adjusted["tempHot"]) - 1
        adjusted["boostTempLimit"] = int(adjusted["boostTempLimit"]) - 2
    return adjusted


def quiet_active(start: str, end: str) -> bool:
    if start == end:
        return False
    if not valid_hhmm(start) or not valid_hhmm(end):
        return False
    now = time.localtime()
    now_m = now.tm_hour * 60 + now.tm_min
    start_h, start_m = [int(part) for part in start.split(":", 1)]
    end_h, end_m = [int(part) for part in end.split(":", 1)]
    start_total = start_h * 60 + start_m
    end_total = end_h * 60 + end_m
    if start_total < end_total:
        return start_total <= now_m < end_total
    return now_m >= start_total or now_m < end_total


# ──────────────────────────────────────────────────────────────────────────
# Auto-daemon pause state and decision reasons
# ──────────────────────────────────────────────────────────────────────────
_SNOOZE_WEB_CACHE = (0, 0, False)
_SNOOZE_WEB_LOCK = threading.Lock()

def pause_payload(config: dict[str, str]) -> dict[str, Any]:
    global _SNOOZE_WEB_CACHE
    now = int(time.time())
    mode = config.get("AUTO_MODE", "dynamic")
    quiet = quiet_active(config.get("QUIET_HOURS_START", "22:00"), config.get("QUIET_HOURS_END", "08:00"))
    
    with _SNOOZE_WEB_LOCK:
        if now - _SNOOZE_WEB_CACHE[0] < 30:
            snooze_until, today_off = _SNOOZE_WEB_CACHE[1], _SNOOZE_WEB_CACHE[2]
        else:
            today_off = SKIP_TODAY_FILE.exists() and read_text(SKIP_TODAY_FILE, "") == time.strftime("%Y-%m-%d")
            snooze_until = int(read_text(SNOOZE_FILE, "0") or "0") if SNOOZE_FILE.exists() else 0
            _SNOOZE_WEB_CACHE = (now, snooze_until, today_off)
            
    snoozed = snooze_until > now
    if mode == "off":
        reason = "Auto mode is off."
    elif mode == "quiet":
        reason = "Quiet mode only allows critical heat protection."
    elif quiet:
        reason = "Quiet hours are active."
    elif today_off:
        reason = "Suggestions are paused for today."
    elif snoozed:
        reason = f"Suggestions are snoozed for {snooze_until - now}s."
    else:
        reason = "Suggestions are available."
    return {
        "quietActive": quiet,
        "todayOff": today_off,
        "snoozed": snoozed,
        "snoozeUntil": snooze_until,
        "reason": reason,
    }


def decision_reason(
    mode: str,
    profile: str,
    cpu_temp: int,
    cpu_load: int,
    thresholds: dict[str, int | str],
    pause: dict[str, Any],
) -> str:
    if pause["reason"] != "Suggestions are available.":
        return str(pause["reason"])
    boost_limit = int(thresholds["boostTempLimit"])
    if cpu_temp >= boost_limit and profile != "performance":
        return f"Not suggesting Boost because CPU is {cpu_temp} C and the {mode} Boost limit is {boost_limit} C."
    if cpu_temp >= int(thresholds["tempHot"]) and profile == "performance":
        return f"A cooler profile is preferred because CPU is {cpu_temp} C."
    if cpu_load >= int(thresholds["loadHigh"]) and profile != "performance":
        return f"Boost can be suggested if load stays high and CPU remains below {boost_limit} C."
    if cpu_load <= int(thresholds["loadIdle"]) and profile == "performance":
        return "Powersave can be suggested if the system stays idle."
    return "Current profile looks reasonable for the active mode."


RECENT_SWITCH_WINDOW_SECONDS = 120

def recent_switch_reason(live: dict[str, Any] | None) -> str | None:
    """Explain a just-happened silent auto-switch (AC/battery/slow-charge/
    screen-lock/critical-heat) that decision_reason()'s thermal/load
    heuristic never mentions, so "why did my profile just change" has an
    answer for those paths too.
    """
    if not live:
        return None
    last_switch = live.get("lastSwitch") or {}
    text = last_switch.get("text")
    switch_time = last_switch.get("time") or 0
    if not text or time.time() - switch_time > RECENT_SWITCH_WINDOW_SECONDS:
        return None
    return str(text)


# ──────────────────────────────────────────────────────────────────────────
# System state cache (governor, EPP, turbo, services)
# ──────────────────────────────────────────────────────────────────────────
_SYS_STATE_CACHE = {}
_SYS_STATE_LOCK = threading.Lock()

def get_sys_state() -> dict[str, str]:
    global _SYS_STATE_CACHE
    now = time.time()
    with _SYS_STATE_LOCK:
        if _SYS_STATE_CACHE and now - _SYS_STATE_CACHE.get('time', 0) < 10:
            return _SYS_STATE_CACHE['val']
            
    gov = read_text("/sys/devices/system/cpu/cpufreq/policy0/scaling_governor", "") or read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", "")
    epp = read_text("/sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference", "") or read_text("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference", "") or "unsupported"
    if Path("/sys/devices/system/cpu/intel_pstate/no_turbo").exists():
        turbo = "ON" if read_text("/sys/devices/system/cpu/intel_pstate/no_turbo", "1") == "0" else "OFF"
    elif Path("/sys/devices/system/cpu/cpufreq/boost").exists():
        turbo = "ON" if read_text("/sys/devices/system/cpu/cpufreq/boost", "0") == "1" else "OFF"
    elif Path("/sys/devices/system/cpu/amd_pstate/boost").exists():
        turbo = "ON" if read_text("/sys/devices/system/cpu/amd_pstate/boost", "0") == "1" else "OFF"
    elif Path("/sys/devices/system/cpu/cpufreq/policy0/boost").exists():
        turbo = "ON" if read_text("/sys/devices/system/cpu/cpufreq/policy0/boost", "0") == "1" else "OFF"
    else:
        turbo = "unsupported"
    
    thp_raw = read_text("/sys/kernel/mm/transparent_hugepage/enabled", "")
    m = re.search(r'\[([^\]]+)\]', thp_raw)
    thp = m.group(1) if m else "unknown"

    val = {"governor": gov, "epp": epp, "turbo": turbo, "thp": thp}
    with _SYS_STATE_LOCK:
        _SYS_STATE_CACHE = {'time': time.time(), 'val': val}
    return val


_CACHE = {}
_CACHE_LOCK = threading.Lock()

def cached_run(key: str, cmd: list[str], ttl: int) -> str:
    now = time.time()
    with _CACHE_LOCK:
        if key in _CACHE and now - _CACHE[key]['time'] < ttl:
            return _CACHE[key]['val']
    try:
        res = run(cmd, timeout=3).stdout.strip()
    except Exception:
        res = ""
    with _CACHE_LOCK:
        _CACHE[key] = {'time': time.time(), 'val': res}
    return res


def active_service(name: str) -> str:
    return cached_run(f"service_{name}", ["systemctl", "is-active", name], 5) or "inactive"


# ──────────────────────────────────────────────────────────────────────────
# Live snapshot written by the auto daemon
# ──────────────────────────────────────────────────────────────────────────
def read_live_snapshot() -> dict[str, Any] | None:
    """Return the daemon's per-tick state snapshot when it's fresh, else None.

    The daemon (already root, single poll authority) writes this file every
    tick. Reading it here avoids a second independent sysfs/hwmon/nvidia-smi
    poll from this process when the daemon is already doing it.
    """
    try:
        mtime = LIVE_FILE.stat().st_mtime
    except OSError:
        return None
    if time.time() - mtime > LIVE_FRESH_SECONDS:
        return None
    try:
        data = json.loads(LIVE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


_SENSOR_CACHE: dict[str, Any] = {"time": 0.0, "groups": []}


# ──────────────────────────────────────────────────────────────────────────
# Sensors, fans and GPU power limit payloads
# ──────────────────────────────────────────────────────────────────────────
_SENSOR_LOCK = threading.Lock()


def sensor_groups(live: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Component temperatures, preferring the daemon's per-tick snapshot.

    Falling back to a direct read keeps the dashboard useful when the daemon
    is stopped, while the 5s cache stops several browser tabs from each
    walking every hwmon file.
    """
    if live and isinstance(live.get("sensors"), list):
        return live["sensors"]
    if not NATIVE_SYSFS:
        return BACKEND.get_sensors()
    if sensor_layer is None:
        return []
    now = time.time()
    with _SENSOR_LOCK:
        if _SENSOR_CACHE["groups"] and now - _SENSOR_CACHE["time"] < 5:
            return _SENSOR_CACHE["groups"]
    try:
        groups = sensor_layer.group_by_category(sensor_layer.get_all())
    except Exception:  # noqa: BLE001 - a bad sensor must not break the page
        groups = []
    with _SENSOR_LOCK:
        _SENSOR_CACHE["groups"], _SENSOR_CACHE["time"] = groups, time.time()
    return groups


_FAN_CACHE: dict[str, Any] = {"time": 0.0, "value": None}
_FAN_LOCK = threading.Lock()


def fan_payload(live: dict[str, Any] | None) -> dict[str, Any]:
    """Fan engine state for the dashboard: live status + editable config."""
    if fancontrol is None:
        return {"available": False, "enabled": False, "fans": [], "config": {}, "presets": []}
    now = time.time()
    with _FAN_LOCK:
        cached = _FAN_CACHE["value"]
        fresh = cached is not None and now - _FAN_CACHE["time"] < 5
    if not fresh:
        try:
            config = fancontrol.load_config()
            channels = [
                {"id": channel.id, "chip": channel.chip, "index": channel.index,
                 "hasRpm": bool(channel.rpm_path)}
                for channel in fancontrol.discover_channels()
            ]
            calibration = fancontrol._read_json(fancontrol.CALIBRATION_FILE) or {}
        except Exception:  # noqa: BLE001
            config, channels, calibration = {"enabled": False, "fans": {}}, [], {}
        cached = {
            "available": bool(channels),
            "enabled": bool(config.get("enabled")),
            "configError": config.get("error"),
            "config": config.get("fans", {}),
            "channels": channels,
            "calibration": calibration,
            "presets": list(fancontrol.PRESET_SHAPES.keys()),
            "profileKeys": list(fancontrol.PROFILE_KEYS),
        }
        with _FAN_LOCK:
            _FAN_CACHE["value"], _FAN_CACHE["time"] = cached, time.time()
    payload = dict(cached)
    status = (live or {}).get("fans") if isinstance((live or {}).get("fans"), dict) else None
    if status is None:
        # No daemon snapshot (service stopped, or fan control never enabled):
        # read the pwm/RPM values straight from sysfs so the cards still show
        # what the fans are actually doing under BIOS control.
        status = {"enabled": payload["enabled"], "guard": {}, "fans": _direct_fan_readings()}
    payload["status"] = status
    return payload


def _direct_fan_readings() -> list[dict[str, Any]]:
    readings = []
    try:
        for channel in fancontrol.discover_channels():
            rpm = channel.read_rpm()
            readings.append({
                "id": channel.id,
                "pwm": fancontrol.raw_to_pct(channel.read_pwm()),
                "rpm": rpm,
                "mode": "bios",
                "controlled": False,
                "note": "",
            })
    except Exception:  # noqa: BLE001
        return []
    return readings


def _invalidate_fan_cache() -> None:
    with _FAN_LOCK:
        _FAN_CACHE["value"], _FAN_CACHE["time"] = None, 0.0


def gpu_limit_payload(live: dict[str, Any] | None, config: dict[str, str]) -> dict[str, Any]:
    """Driver-reported GPU power limit range plus the user's per-profile ask."""
    live_gpu = (live or {}).get("gpu") or {}
    min_w = int(live_gpu.get("minLimit") or 0)
    max_w = int(live_gpu.get("maxLimit") or 0)
    if not max_w:
        min_w, max_w = _gpu_limit_range()
    return {
        "supported": max_w > 0,
        "minW": min_w,
        "maxW": max_w,
        "requested": {
            "boost": number_config(config, "GPU_PL_BOOST_W", 0),
            "powersave": number_config(config, "GPU_PL_POWERSAVE_W", 0),
            "silent": number_config(config, "GPU_PL_SILENT_W", 0),
        },
    }


_GPU_RANGE_CACHE: tuple[float, tuple[int, int]] = (0.0, (0, 0))


def _gpu_limit_range() -> tuple[int, int]:
    global _GPU_RANGE_CACHE
    if not NATIVE_SYSFS:
        return BACKEND.gpu_power_limit_range()
    now = time.time()
    if _GPU_RANGE_CACHE[0] and now - _GPU_RANGE_CACHE[0] < 300:
        return _GPU_RANGE_CACHE[1]
    result = (0, 0)
    out = cached_run("gpu_range", [
        "nvidia-smi", "--query-gpu=power.min_limit,power.max_limit",
        "--format=csv,noheader,nounits", "-i", "0",
    ], 300)
    if out:
        parts = [part.strip() for part in out.splitlines()[0].split(",")]
        if len(parts) == 2:
            try:
                result = (int(float(parts[0])), int(float(parts[1])))
            except ValueError:
                result = (0, 0)
    if result == (0, 0):
        amd = find_amd_gpu_hwmon()
        if amd:
            result = (
                int(read_text(f"{amd}power1_cap_min", "0") or "0") // 1_000_000,
                int(read_text(f"{amd}power1_cap_max", "0") or "0") // 1_000_000,
            )
    _GPU_RANGE_CACHE = (now, result)
    return result


# ──────────────────────────────────────────────────────────────────────────
# Hardware telemetry: profile, CPU, GPU, RAPL
# ──────────────────────────────────────────────────────────────────────────
def power_profile() -> str:
    if not NATIVE_SYSFS:
        return BACKEND.power_profile()
    ppd = cached_run("powerprofile_ppd", ["powerprofilesctl", "get"], 5)
    if ppd:
        return ppd
    tuned = cached_run("powerprofile_tuned", ["tuned-adm", "active"], 5)
    if tuned.startswith("Current active profile: "):
        tuned = tuned.replace("Current active profile: ", "", 1)
    tuned_map = {
        "throughput-performance": "performance",
        "latency-performance": "performance",
        "accelerator-performance": "performance",
        "powersave": "power-saver",
        "balanced-battery": "power-saver",
    }
    if tuned in tuned_map:
        return tuned_map[tuned]
    return tuned or "unknown"


_CACHED_TEMP_FILE: str | None = None
_CACHED_CORETEMP_DIR: Path | None = None

def _coretemp_corrected(hwmon_dir: Path, package_raw: int) -> int:
    """Reject a "Package id 0" reading inflated by a single outlier core.

    coretemp reports Package id 0 as the max of all per-core Digital
    Thermal Sensors; a single miscalibrated core can pin that value well
    above the motherboard's own PECI-based reading. Fall back to the
    median of per-core "Core N" sensors when the gap is implausible.
    """
    core_raws = []
    for label_file in hwmon_dir.glob("temp*_label"):
        if read_text(label_file, "").startswith("Core "):
            try:
                core_raws.append(int(read_text(str(label_file).replace("_label", "_input"), "0") or "0"))
            except ValueError:
                pass
    if len(core_raws) < 2:
        return package_raw
    core_raws.sort()
    mid = len(core_raws) // 2
    median_raw = core_raws[mid] if len(core_raws) % 2 else (core_raws[mid - 1] + core_raws[mid]) // 2
    return median_raw if package_raw - median_raw > 15000 else package_raw

def cpu_temp_c() -> int:
    global _CACHED_TEMP_FILE, _CACHED_CORETEMP_DIR
    if not NATIVE_SYSFS:
        return BACKEND.get_cpu_temp()
    if _CACHED_TEMP_FILE:
        raw = int(read_text(_CACHED_TEMP_FILE, "0") or "0")
        if raw > 0:
            if _CACHED_CORETEMP_DIR:
                raw = _coretemp_corrected(_CACHED_CORETEMP_DIR, raw)
            return raw // 1000

    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        name = read_text(hwmon / "name", "")
        if name not in {"coretemp", "k10temp", "zenpower", "amd_energy", "macsmc_hwmon"}:
            continue
        if name == "macsmc_hwmon":
            best_target = ""
            best_raw = 0
            for input_file in hwmon.glob("temp*_input"):
                raw = int(read_text(input_file, "0") or "0")
                if raw > best_raw:
                    best_raw = raw
                    best_target = str(input_file)
            if best_target:
                _CACHED_TEMP_FILE = best_target
                return best_raw // 1000
        for label_file in hwmon.glob("temp*_label"):
            label = read_text(label_file, "")
            if label in {
                "Package id 0", "Tctl", "Tdie", "Tccd1", "Tccd2",
                "WiFi/BT Module Temp", "NAND Flash Temperature",
                "Composite", "Battery Hotspot",
            }:
                target = str(label_file).replace("_label", "_input")
                raw = int(read_text(target, "0") or "0")
                _CACHED_TEMP_FILE = target
                if name == "coretemp" and label == "Package id 0":
                    _CACHED_CORETEMP_DIR = hwmon
                    raw = _coretemp_corrected(hwmon, raw)
                return raw // 1000
        target = str(hwmon / "temp1_input")
        raw = int(read_text(target, "0") or "0")
        if raw > 0:
            _CACHED_TEMP_FILE = target
            return raw // 1000
    return 0


def cpu_totals() -> tuple[int, int]:
    parts = read_text("/proc/stat", "").splitlines()[0].split()
    values = [int(value) for value in parts[1:]]
    if len(values) < 5:
        return 0, 0
    idle = values[3] + values[4]
    return sum(values), idle


_CPU_LOCK = threading.Lock()
_LAST_CPU_TOTAL = 0
_LAST_CPU_IDLE = 0

def cpu_load_percent() -> int:
    global _LAST_CPU_TOTAL, _LAST_CPU_IDLE
    if not NATIVE_SYSFS:
        return BACKEND.get_cpu_load()
    total, idle = cpu_totals()
    
    with _CPU_LOCK:
        if _LAST_CPU_TOTAL == 0:  # First run
            _LAST_CPU_TOTAL = total
            _LAST_CPU_IDLE = idle
            return 0
            
        delta_total = total - _LAST_CPU_TOTAL
        delta_idle = idle - _LAST_CPU_IDLE
        _LAST_CPU_TOTAL = total
        _LAST_CPU_IDLE = idle
        
    if delta_total <= 0:
        return 0
    return int((delta_total - delta_idle) * 100 / delta_total)


_AMD_GPU_HWMON: str | None = None
_AMD_GPU_HWMON_CHECKED = False

def find_amd_gpu_hwmon() -> str | None:
    global _AMD_GPU_HWMON, _AMD_GPU_HWMON_CHECKED
    if _AMD_GPU_HWMON_CHECKED:
        return _AMD_GPU_HWMON
    _AMD_GPU_HWMON_CHECKED = True
    drm = Path("/sys/class/drm")
    if not drm.exists():
        return None
    for card in drm.iterdir():
        hwmon_dir = card / "device" / "hwmon"
        if not hwmon_dir.is_dir():
            continue
        for hwmon in hwmon_dir.iterdir():
            if read_text(hwmon / "name", "") == "amdgpu":
                _AMD_GPU_HWMON = str(hwmon) + "/"
                return _AMD_GPU_HWMON
    return None


def gpu_stats() -> dict[str, str]:
    # NVIDIA first
    out = cached_run("gpu", [
        "nvidia-smi",
        "--query-gpu=temperature.gpu,power.draw,power.limit",
        "--format=csv,noheader,nounits"
    ], 5)
    if out:
        parts = [part.strip() for part in out.splitlines()[0].split(",")]
        if len(parts) == 3:
            temp, power, limit = parts
            return {"temp": temp, "power": power, "limit": limit, "vendor": "nvidia"}
    # AMD GPU via amdgpu sysfs (values in µW → convert to W)
    amd = find_amd_gpu_hwmon()
    if amd:
        try:
            temp = int(read_text(f"{amd}temp1_input", "0") or "0") // 1000
            power_uw = int(read_text(f"{amd}power1_average", "0") or "0")
            cap_uw = int(read_text(f"{amd}power1_cap", "0") or "0")
            return {
                "temp": str(temp),
                "power": f"{power_uw / 1_000_000:.1f}",
                "limit": f"{cap_uw / 1_000_000:.1f}",
                "vendor": "amd",
            }
        except Exception:
            pass
    return {"temp": "0", "power": "0", "limit": "0", "vendor": "none"}


_RAPL_CACHE: dict[int, dict[str, Any]] = {}
_RAPL_LOCK = threading.Lock()
_RAPL_BASE = "/sys/class/powercap/intel-rapl/intel-rapl:0"

def rapl_w(constraint: int) -> int:
    now = time.time()
    with _RAPL_LOCK:
        if constraint in _RAPL_CACHE and now - _RAPL_CACHE[constraint]['time'] < 10:
            return _RAPL_CACHE[constraint]['val']

    if not Path(_RAPL_BASE).is_dir():
        # AMD CPU or no Intel RAPL — return 0 gracefully
        with _RAPL_LOCK:
            _RAPL_CACHE[constraint] = {'time': now, 'val': 0}
        return 0

    path = f"{_RAPL_BASE}/constraint_{constraint}_power_limit_uw"
    val = int(read_text(path, "0") or "0") // 1_000_000

    with _RAPL_LOCK:
        _RAPL_CACHE[constraint] = {'time': time.time(), 'val': val}
    return val


# ──────────────────────────────────────────────────────────────────────────
# Statistics history and battery
# ──────────────────────────────────────────────────────────────────────────
_HISTORY_LOCK = threading.Lock()
_HISTORY_CACHE_MTIME: float = -1
_HISTORY_CACHE_LIMIT: int = 0
_HISTORY_CACHE_DATA: list[dict[str, str]] = []

def history(limit: int = 80) -> list[dict[str, str]]:
    global _HISTORY_CACHE_MTIME, _HISTORY_CACHE_LIMIT, _HISTORY_CACHE_DATA
    try:
        current_mtime = STATS_FILE.stat().st_mtime
    except OSError:
        current_mtime = 0

    with _HISTORY_LOCK:
        if current_mtime != 0 and current_mtime == _HISTORY_CACHE_MTIME and limit <= _HISTORY_CACHE_LIMIT:
            return _HISTORY_CACHE_DATA[-limit:] if limit > 0 else _HISTORY_CACHE_DATA.copy()

    if current_mtime == 0:
        with _HISTORY_LOCK:
            _HISTORY_CACHE_MTIME = 0
            _HISTORY_CACHE_LIMIT = limit
            _HISTORY_CACHE_DATA = []
        return []

    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []

    if len(lines) <= 1:
        with _HISTORY_LOCK:
            _HISTORY_CACHE_MTIME = current_mtime
            _HISTORY_CACHE_LIMIT = limit
            _HISTORY_CACHE_DATA = []
        return []

    # Slice only data rows so a short file never feeds the header in twice
    data = list(csv.DictReader([lines[0]] + lines[1:][-limit:]))
    with _HISTORY_LOCK:
        _HISTORY_CACHE_DATA = data
        _HISTORY_CACHE_MTIME = current_mtime
        _HISTORY_CACHE_LIMIT = limit
    return data.copy()


def summary(rows: list[dict[str, str]]) -> dict[str, float]:
    if not rows:
        return {"avg_cpu": 0, "avg_temp": 0, "avg_gpu": 0, "max_temp": 0, "max_cpu": 0}

    def number(row: dict[str, str], key: str) -> float:
        try:
            return float(row.get(key, "0") or "0")
        except ValueError:
            return 0

    sum_cpu = sum_temp = sum_gpu = 0.0
    max_temp = max_cpu = 0.0
    for row in rows:
        cpu = number(row, "cpu_load")
        temp = number(row, "cpu_temp")
        gpu = number(row, "gpu_power")
        
        sum_cpu += cpu
        sum_temp += temp
        sum_gpu += gpu
        
        if temp > max_temp: max_temp = temp
        if cpu > max_cpu: max_cpu = cpu

    count = len(rows)
    return {
        "avg_cpu": sum_cpu / count,
        "avg_temp": sum_temp / count,
        "avg_gpu": sum_gpu / count,
        "max_temp": max_temp,
        "max_cpu": max_cpu,
    }


def _extract_profile_switches(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    switches = []
    prev = None
    for row in rows:
        p = row.get("profile", "")
        if p and p != prev:
            if prev is not None:
                switches.append({"iso": row.get("iso", ""), "profile": p})
            prev = p
    return switches[-5:]  # last 5 transitions for the dashboard log


# ── Battery helpers ──────────────────────────────────────────────────

_BATTERY_SUPPLY: str | None = None

def find_battery_supply() -> str | None:
    global _BATTERY_SUPPLY
    if _BATTERY_SUPPLY is not None:
        return _BATTERY_SUPPLY if _BATTERY_SUPPLY else None
    psu_dir = Path("/sys/class/power_supply")
    if not psu_dir.is_dir():
        _BATTERY_SUPPLY = ""
        return None
    for entry in psu_dir.iterdir():
        type_path = entry / "type"
        try:
            if type_path.read_text(encoding="utf-8").strip() == "Battery":
                _BATTERY_SUPPLY = str(entry)
                return str(entry)
        except OSError:
            continue
    _BATTERY_SUPPLY = ""
    return None

def battery_pct() -> int | None:
    supply = find_battery_supply()
    if not supply:
        return None
    try:
        val = int(read_text(f"{supply}/capacity", "0") or "0")
        return val if val > 0 else None
    except (ValueError, OSError):
        return None

def battery_status_text() -> str:
    supply = find_battery_supply()
    if not supply:
        return "Unknown"
    return read_text(f"{supply}/status", "Unknown")

def ac_online() -> int | None:
    psu_dir = Path("/sys/class/power_supply")
    if not psu_dir.is_dir():
        return None
    for entry in psu_dir.iterdir():
        type_path = entry / "type"
        try:
            if type_path.read_text(encoding="utf-8").strip() == "Mains":
                online = int(read_text(str(entry / "online"), "0") or "0")
                return online
        except OSError:
            continue
    return None

def battery_drain_rate(rows: list[dict[str, str]]) -> float | None:
    """Return drain rate in %/hour from recent history while discharging, else None."""
    discharge_rows = [
        r for r in rows
        if r.get("battery_status") == "Discharging" and r.get("battery_pct", "").lstrip('-').isdigit()
    ]
    if len(discharge_rows) < 2:
        return None
    try:
        first, last = discharge_rows[0], discharge_rows[-1]
        delta_pct = float(first["battery_pct"]) - float(last["battery_pct"])
        delta_sec = float(last["epoch"]) - float(first["epoch"])
        if delta_sec <= 60 or delta_pct <= 0:
            return None
        return round(delta_pct / delta_sec * 3600, 1)
    except (ValueError, KeyError, ZeroDivisionError):
        return None


# ──────────────────────────────────────────────────────────────────────────
# Aggregated payloads served by /api/status
# ──────────────────────────────────────────────────────────────────────────
SILENT_PENDING_FILE = boost_paths.SILENT_PENDING_FILE


def interlock_payload(
    live: dict[str, Any] | None, config: dict[str, str], cpu_temp: int, cpu_load: int
) -> dict[str, Any]:
    """Why Eco/Silent is (or is not) available right now.

    The daemon publishes this in the live snapshot because it also tracks how
    long the load has been high; without the snapshot we fall back to the same
    instantaneous rule bin/silent uses.
    """
    if live and isinstance(live.get("interlock"), dict):
        data = dict(live["interlock"])
    else:
        boost_limit = number_config(config, "BOOST_TEMP_LIMIT", 78)
        load_high = number_config(config, "LOAD_HIGH", 75)
        if cpu_temp >= boost_limit:
            data = {"silentBlocked": True, "reason": f"the CPU is {cpu_temp} C (limit {boost_limit} C)"}
        elif cpu_load >= load_high:
            data = {"silentBlocked": True, "reason": f"the CPU is {cpu_load}% busy (busy threshold {load_high}%)"}
        else:
            data = {"silentBlocked": False, "reason": ""}
        data["pending"] = SILENT_PENDING_FILE.exists()
    waited = ""
    since = int(data.get("pendingSince") or 0)
    if since:
        seconds = max(0, int(time.time()) - since)
        waited = f" (waiting {seconds // 60}m {seconds % 60}s so far)"
    if data.get("silentBlocked"):
        queued = f"Eco Mode is already queued{waited}. " if data.get("pending") else ""
        data["hint"] = (
            f"{queued}Eco Mode is held back because {data.get('reason', 'the machine is busy')}. "
            "Pick it anyway and Boost will switch over by itself once things cool down."
        )
    elif data.get("pending"):
        data["hint"] = f"Eco Mode is queued{waited} and will apply as soon as the machine cools down."
    else:
        data["hint"] = ""
    return data


def status_payload() -> dict[str, Any]:
    config = read_config()
    rows = history()
    live = read_live_snapshot()
    if live:
        live_gpu = live.get("gpu", {})
        live_limits = live.get("limits", {})
        gpu = {
            "temp": str(live_gpu.get("temp", "0")),
            "power": str(live_gpu.get("power", "0")),
            "limit": str(live_gpu.get("limit", "0")),
            "vendor": "daemon",
        }
        profile = str(live.get("profile") or power_profile())
        cpu_load = int(live.get("cpu", {}).get("load", 0) or 0)
        cpu_temp = int(live.get("cpu", {}).get("temp", 0) or 0)
        pl1 = int(live_limits.get("pl1", 0) or 0)
        pl2 = int(live_limits.get("pl2", 0) or 0)
    else:
        gpu = gpu_stats()
        profile = power_profile()
        cpu_load = cpu_load_percent()
        cpu_temp = cpu_temp_c()
        pl1, pl2 = rapl_w(0), rapl_w(1)
    mode = config.get("AUTO_MODE", "dynamic")
    ambient = ambient_temp(config)
    base_thresholds = mode_thresholds(mode, config)
    thresholds = apply_ambient_adjustment(base_thresholds, ambient)
    pause = pause_payload(config)
    low_pct = number_config(config, "BATTERY_LOW_PCT", 20)
    critical_pct = number_config(config, "BATTERY_CRITICAL_PCT", 10)
    return {
        "ok": True,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "auto": {
            "mode": mode,
            "service": active_service("boost-auto.service"),
            "quietStart": config.get("QUIET_HOURS_START", "22:00"),
            "quietEnd": config.get("QUIET_HOURS_END", "08:00"),
            "summerSilentNights": config.get("SUMMER_SILENT_NIGHTS", "no"),
            "thresholds": thresholds,
            "modes": [mode_thresholds(item, config) for item in ("dynamic", "gaming", "creator", "quiet", "off")],
            "pause": pause,
            "ambient": ambient,
            "decision": recent_switch_reason(live) or decision_reason(mode, profile, cpu_temp, cpu_load, thresholds, pause),
        },
        "web": {"service": active_service("boost-web.service"), "url": f"http://{HOST}:{PORT}"},
        "profile": profile,
        "friendlyProfile": {"performance": "Performance", "balanced": "Balanced", "power-saver": "Eco Mode"}.get(profile, profile),
        "cpu": {"load": cpu_load, "temp": cpu_temp},
        "gpu": gpu,
        "limits": {"pl1": pl1, "pl2": pl2},
        "system": get_sys_state(),
        "sensors": sensor_groups(live),
        "fans": fan_payload(live),
        "interlock": interlock_payload(live, config, cpu_temp, cpu_load),
        "gpuLimit": gpu_limit_payload(live, config),
        "report": {"latestExists": LATEST_REPORT.exists(), "path": str(LATEST_REPORT)},
        "summary": summary(rows),
        "history": rows[-30:],
        "profileSwitches": _extract_profile_switches(rows),
        "battery": {
            "pct": battery_pct(),
            "status": battery_status_text(),
            "acOnline": ac_online(),
            "drainRatePctPerHour": battery_drain_rate(rows),
            "acProfile": config.get("AC_PROFILE", "restore"),
            "batteryProfile": config.get("BATTERY_PROFILE", "powersave"),
            "lowPct": low_pct,
            "criticalPct": critical_pct,
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# Actions triggered from the dashboard (POST /api/action)
# ──────────────────────────────────────────────────────────────────────────
PROFILE_ACTIONS = ("boost", "powersave", "silent", "restore")
AUTO_ACTIONS = {"auto-mode", "snooze", "today-off", "resume", "quiet-hours", "summer-nights"}
FAN_ACTIONS = {"fan-enable", "fan-config", "fan-preset", "fan-test", "fan-calibrate"}


def fan_or_gpu_action(action: str, value: str | None) -> dict[str, Any]:
    """Fan engine and GPU power limit actions.

    Everything that touches a fan goes through lib/fancontrol.py so the
    validation rules (monotonic curve, mandatory 80%-by-85C tail, driver
    clamping) are the same whether the request came from the dashboard, the
    CLI, or a hand-edited /etc/boost-fans.json.
    """
    global _CONFIG_CACHE_MTIME

    if action == "gpu-limit":
        try:
            payload = json.loads(value or "{}")
        except (json.JSONDecodeError, TypeError):
            return {"ok": False, "message": "Invalid JSON for gpu-limit."}
        profile = str(payload.get("profile", "boost"))
        if profile not in {"boost", "powersave", "silent"}:
            return {"ok": False, "message": "GPU limit profile must be boost, powersave or silent."}
        watts = str(payload.get("watts", "")).strip()
        key = f"GPU_PL_{profile.upper()}_W"
        updates, error = validate_config_updates({key: watts})
        if error:
            return {"ok": False, "message": error}
        if not write_config(updates):
            return {"ok": False, "message": "Failed to write config."}
        with _CONFIG_LOCK:
            _CONFIG_CACHE_MTIME = -1
        if updates[key]:
            return BACKEND.set_gpu_power_limit(int(updates[key]), profile)
        return {"ok": True, "message": f"GPU limit for {profile} is back to automatic."}

    if fancontrol is None:
        return {"ok": False, "message": "Fan control is not installed on this system."}

    if action == "fan-enable":
        if value not in {"on", "off"}:
            return {"ok": False, "message": "Fan control must be turned on or off."}
        result = run(["/usr/local/bin/auto", "fans", "on" if value == "on" else "off"], timeout=20)
        _invalidate_fan_cache()
        message = (result.stdout or result.stderr).strip().splitlines()
        return {
            "ok": result.returncode == 0,
            "message": message[-1] if message else "Fan control updated.",
        }

    if action == "fan-config":
        try:
            payload = json.loads(value or "{}")
        except (json.JSONDecodeError, TypeError):
            return {"ok": False, "message": "Invalid JSON for the fan curve."}
        if not isinstance(payload, dict):
            return {"ok": False, "message": "Fan config must be a JSON object."}
        config = fancontrol.load_config()
        config.pop("error", None)
        fan_id = payload.get("fan")
        if fan_id:
            fan = config.get("fans", {}).get(str(fan_id))
            if fan is None:
                return {"ok": False, "message": f"Unknown fan: {fan_id}"}
            for field in ("min_pwm", "stop_allowed", "hyst_up", "hyst_down",
                          "response_delay_s", "step_limit", "enabled", "name"):
                if field in payload:
                    fan[field] = payload[field]
            if isinstance(payload.get("source"), dict):
                fan["source"] = payload["source"]
            for key, points in (payload.get("profiles") or {}).items():
                if key not in fancontrol.PROFILE_KEYS:
                    return {"ok": False, "message": f"Unknown fan profile: {key}"}
                fan["profiles"][key] = points
                fan.setdefault("preset", {})[key] = "custom"
        elif isinstance(payload.get("fans"), dict):
            config["fans"] = payload["fans"]
        else:
            return {"ok": False, "message": "Nothing to save."}
        ok, error = fancontrol.save_config(config)
        _invalidate_fan_cache()
        return {"ok": ok, "message": "Fan curve saved." if ok else str(error)}

    if action == "fan-preset":
        try:
            payload = json.loads(value or "{}")
        except (json.JSONDecodeError, TypeError):
            return {"ok": False, "message": "Invalid JSON for the fan preset."}
        fan_id = str(payload.get("fan", ""))
        preset = str(payload.get("preset", ""))
        profile = payload.get("profile")
        if preset not in fancontrol.PRESET_SHAPES:
            return {"ok": False, "message": "Unknown fan preset."}
        args = ["/usr/local/bin/auto", "fans", "preset", fan_id, preset]
        if profile in fancontrol.PROFILE_KEYS:
            args.append(str(profile))
        result = run(args, timeout=20)
        _invalidate_fan_cache()
        message = (result.stdout or result.stderr).strip().splitlines()
        return {
            "ok": result.returncode == 0,
            "message": message[-1] if message else "Preset applied.",
        }

    if action == "fan-test":
        try:
            payload = json.loads(value or "{}")
        except (json.JSONDecodeError, TypeError):
            return {"ok": False, "message": "Invalid JSON for the fan test."}
        fan_id = str(payload.get("fan", ""))
        pwm = str(payload.get("pwm", "50"))
        seconds = str(payload.get("seconds", "10"))
        if not re.fullmatch(r"[0-9]{1,3}", pwm) or not re.fullmatch(r"[0-9]{1,3}", seconds):
            return {"ok": False, "message": "Test speed and duration must be whole numbers."}
        result = run(["/usr/local/bin/auto", "fans", "test", fan_id, pwm, seconds], timeout=15)
        message = (result.stdout or result.stderr).strip().splitlines()
        return {
            "ok": result.returncode == 0,
            "message": message[-1] if message else f"Testing {fan_id} at {pwm}%.",
        }

    if action == "fan-calibrate":
        # Minutes of spinning fans: fire and forget, the dashboard polls the
        # calibration file for the result.
        try:
            subprocess.Popen(
                ["/usr/local/bin/auto", "fans", "calibrate"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            return {"ok": False, "message": f"Could not start calibration: {exc}"}
        return {
            "ok": True,
            "message": "Calibration started. Fans will spin up and down for a minute or two.",
        }

    return {"ok": False, "message": "Unknown action."}


def run_action(action: str, value: str | None = None) -> dict[str, Any]:
    global _CONFIG_CACHE_MTIME
    global _SNOOZE_WEB_CACHE
    allowed_modes = {"dynamic", "gaming", "creator", "quiet", "off"}
    allowed_durations = {"30m", "1h", "2h", "4h"}
    if action in PROFILE_ACTIONS:
        outcome = BACKEND.apply_profile(action)
        if outcome.get("ok"):
            with _SYS_STATE_LOCK:
                _SYS_STATE_CACHE.clear()
            with _CACHE_LOCK:
                _CACHE.pop("powerprofile_ppd", None)
                _CACHE.pop("powerprofile_tuned", None)
        return outcome
    if action in AUTO_ACTIONS and not BACKEND.supports_auto_daemon:
        return BACKEND.unsupported("The auto daemon")
    if action == "report" and not BACKEND.supports_auto_daemon:
        return BACKEND.unsupported("HTML reports")
    if action == "auto-mode" and value in allowed_modes:
        result = run(["/usr/local/bin/auto", "mode", value], timeout=10)
    elif action == "snooze" and value in allowed_durations:
        result = run(["/usr/local/bin/auto", "snooze", value], timeout=10)
        with _SNOOZE_WEB_LOCK:
            _SNOOZE_WEB_CACHE = (0, 0, False)
    elif action == "today-off":
        result = run(["/usr/local/bin/auto", "today-off"], timeout=10)
        with _SNOOZE_WEB_LOCK:
            _SNOOZE_WEB_CACHE = (0, 0, False)
    elif action == "resume":
        result = run(["/usr/local/bin/auto", "resume"], timeout=10)
        with _SNOOZE_WEB_LOCK:
            _SNOOZE_WEB_CACHE = (0, 0, False)
    elif action == "quiet-hours":
        try:
            payload = json.loads(value or "{}")
        except (json.JSONDecodeError, TypeError):
            return {"ok": False, "message": "Invalid JSON for quiet-hours."}
        start = str(payload.get("start", "22:00"))
        end = str(payload.get("end", "08:00"))
        if not valid_hhmm(start) or not valid_hhmm(end):
            return {"ok": False, "message": "Quiet hours must use HH:MM."}
        result = run(["/usr/local/bin/auto", "quiet-hours", start, end], timeout=10)
    elif action == "summer-nights" and value in {"on", "off"}:
        result = run(["/usr/local/bin/auto", "summer-nights", value], timeout=10)
    elif action in FAN_ACTIONS or action == "gpu-limit":
        if action in FAN_ACTIONS and not BACKEND.supports_fan_control:
            return BACKEND.unsupported("Fan control")
        return fan_or_gpu_action(action, value)
    elif action == "report":
        result = run(["/usr/local/bin/power-report"], timeout=10)
    elif action == "save-config" and value is not None:
        try:
            updates = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {"ok": False, "message": "Invalid JSON for config update."}
        if not isinstance(updates, dict):
            return {"ok": False, "message": "Config must be a JSON object."}
        updates, error = validate_config_updates(updates)
        if error:
            return {"ok": False, "message": error}
        if write_config(updates):
            with _CONFIG_LOCK:
                _CONFIG_CACHE_MTIME = -1  # force re-read
            return {"ok": True, "message": "Configuration saved."}
        return {"ok": False, "message": "Failed to write config."}
    else:
        return {"ok": False, "message": "Unknown action."}

    if result.returncode == 0:
        return {"ok": True, "message": f"{action.capitalize()} applied successfully."}

    # On error, just return the last line of stderr or stdout so it fits in a toast
    full_err = (result.stderr or result.stdout).strip()
    short_err = full_err.split("\n")[-1] if full_err else "Unknown error"
    return {"ok": False, "message": short_err}


def valid_hhmm(value: str) -> bool:
    try:
        hour, minute = value.split(":", 1)
        return 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59 and len(value) == 5
    except ValueError:
        return False


# ──────────────────────────────────────────────────────────────────────────
# Dashboard page assembled from lib/webui/
# ──────────────────────────────────────────────────────────────────────────
WEBUI_DIR = Path(__file__).resolve().parent / "webui"


def load_index_html() -> str:
    """Assemble the dashboard page from webui/{index.html,app.css,app.js}.

    The three sources are read once at import time and inlined into a single
    document, so the browser still gets a one-request, zero-dependency page
    while the markup, styles and behaviour stay separately editable.
    """
    try:
        template = (WEBUI_DIR / "index.html").read_text(encoding="utf-8")
        css = (WEBUI_DIR / "app.css").read_text(encoding="utf-8")
        js = (WEBUI_DIR / "app.js").read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(
            f"boost-web: dashboard assets are missing from {WEBUI_DIR} ({exc}).\n"
            "Reinstall with ./install.sh so lib/webui/ lands next to boost-web.py."
        ) from exc
    return template.replace("{{APP_CSS}}", css.rstrip("\n")).replace("{{APP_JS}}", js.rstrip("\n"))


INDEX_HTML = load_index_html()

INDEX_HTML_BYTES = INDEX_HTML.encode("utf-8")


# ──────────────────────────────────────────────────────────────────────────
# HTTP server
# ──────────────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "BoostWeb/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.client_address[0]} - {fmt % args}")

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        self.send_bytes(json.dumps(payload).encode("utf-8"), "application/json", status)

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self.send_bytes(INDEX_HTML_BYTES, "text/html; charset=utf-8")
            elif parsed.path == "/favicon.ico":
                self.send_bytes(b"", "image/x-icon")
            elif parsed.path == "/api/status":
                self.send_json(status_payload())
            elif parsed.path == "/api/config":
                self.send_json(config_payload())
            elif parsed.path == "/api/sensors":
                live = read_live_snapshot()
                self.send_json({"ok": True, "groups": sensor_groups(live)})
            elif parsed.path == "/api/fans":
                self.send_json({"ok": True, **fan_payload(read_live_snapshot())})
            elif parsed.path == "/api/stream":
                self.stream_status()
            elif parsed.path == "/report":
                if LATEST_REPORT.exists():
                    self.send_bytes(LATEST_REPORT.read_bytes(), "text/html; charset=utf-8")
                else:
                    self.send_bytes(b"No report yet. Click Generate report first.", "text/plain; charset=utf-8", 404)
            else:
                self.send_bytes(b"Not found", "text/plain; charset=utf-8", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-response; nothing to do
        except Exception as exc:  # noqa: BLE001 - keep the server thread alive
            try:
                self.send_json({"ok": False, "message": html.escape(str(exc))}, 500)
            except OSError:
                pass

    def stream_status(self) -> None:
        """Server-Sent Events: push a status payload when the state changes.

        The dashboard used to re-poll /api/status every 2s from every open
        tab, which kept both the browser and this server awake around the
        clock. Here the daemon's live.json mtime is the trigger, so an idle
        machine costs one cheap stat() per second and nothing else. Clients
        that cannot use EventSource keep polling.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_mtime = -1.0
        last_push = 0.0
        try:
            while True:
                try:
                    mtime = LIVE_FILE.stat().st_mtime
                except OSError:
                    mtime = 0.0
                now = time.time()
                # Push on a snapshot change, and at least every 10s so a
                # stopped daemon still refreshes the clock and service state.
                if mtime != last_mtime or now - last_push >= 10:
                    last_mtime, last_push = mtime, now
                    body = json.dumps(status_payload())
                    self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
                    self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # browser navigated away
        finally:
            # No Content-Length was sent, so this connection cannot be reused.
            self.close_connection = True

    def _csrf_ok(self) -> bool:
        host, port = self.server.server_address
        allowed = {
            (host, port),
            ("localhost", port),
            ("127.0.0.1", port),
        }
        origin = self.headers.get("Origin", "")
        referer = self.headers.get("Referer", "")
        for header in (origin, referer):
            if not header:
                continue
            parsed = urllib.parse.urlparse(header)
            if parsed.scheme != "http":
                continue
            try:
                parsed_port = parsed.port or 80
            except ValueError:
                continue
            if (parsed.hostname, parsed_port) in allowed:
                return True
        return False

    def do_POST(self) -> None:
        if urllib.parse.urlparse(self.path).path != "/api/action":
            self.send_json({"ok": False, "message": "Not found"}, 404)
            return
        if not self._csrf_ok():
            self.send_json({"ok": False, "message": "Forbidden"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 64 * 1024:
                self.send_json({"ok": False, "message": "Request body too large"}, 413)
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                self.send_json({"ok": False, "message": "JSON body must be an object"}, 400)
                return
            action = str(payload.get("action", ""))
            value = payload.get("value")
            self.send_json(run_action(action, None if value is None else str(value)))
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-response; nothing to do
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self.send_json({"ok": False, "message": "Invalid JSON request"}, 400)
        except Exception as exc:  # noqa: BLE001 - local UI should return readable errors
            try:
                self.send_json({"ok": False, "message": html.escape(str(exc))}, 500)
            except OSError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Local boost web dashboard")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Boost web dashboard: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
