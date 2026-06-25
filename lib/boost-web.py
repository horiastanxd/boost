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
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOST = "127.0.0.1"
PORT = 8765
CONF_FILE = Path("/etc/boost-auto.conf")
STATS_FILE = Path("/var/lib/power-profile/stats.csv")
LATEST_REPORT = Path("/var/lib/power-profile/reports/latest.html")
SNOOZE_FILE = Path("/var/lib/power-profile/auto-snooze-until")
SKIP_TODAY_FILE = Path("/var/lib/power-profile/auto-skip-date")


def run(cmd: list[str], timeout: float = 4.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)


def read_text(path: str | Path, default: str = "unknown") -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return default


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
        },
    }



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


def number_config(config: dict[str, str], key: str, default: int) -> int:
    try:
        return int(float(config.get(key, str(default)) or default))
    except ValueError:
        return default


def mode_thresholds(mode: str, config: dict[str, str] | None = None) -> dict[str, int | str]:
    thresholds: dict[str, int | str] = dict(DEFAULT_THRESHOLDS)
    if mode == "dynamic":
        thresholds.update(
            {
                "tempHot": 78,
                "boostTempLimit": 78,
                "loadHigh": 75,
                "loadHighDuration": 120,
                "loadIdle": 8,
                "loadIdleDuration": 600,
                "promptCooldown": 900,
            }
        )
    elif mode == "gaming":
        thresholds.update(
            {
                "tempHot": 80,
                "boostTempLimit": 80,
                "loadHigh": 50,
                "loadHighDuration": 30,
                "loadIdle": 10,
                "loadIdleDuration": 600,
                "promptCooldown": 900,
            }
        )
    elif mode == "creator":
        thresholds.update(
            {
                "tempHot": 82,
                "boostTempLimit": 82,
                "loadHigh": 85,
                "loadHighDuration": 30,
                "loadIdle": 15,
                "loadIdleDuration": 1200,
                "promptCooldown": 300,
            }
        )
    elif mode == "quiet":
        thresholds.update(
            {
                "tempHot": 70,
                "boostTempLimit": 70,
                "loadHigh": 90,
                "loadHighDuration": 600,
                "loadIdle": 5,
                "loadIdleDuration": 120,
                "promptCooldown": 3600,
            }
        )
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
    now = time.localtime()
    now_m = now.tm_hour * 60 + now.tm_min
    start_h, start_m = [int(part) for part in start.split(":", 1)]
    end_h, end_m = [int(part) for part in end.split(":", 1)]
    start_total = start_h * 60 + start_m
    end_total = end_h * 60 + end_m
    if start_total < end_total:
        return start_total <= now_m < end_total
    return now_m >= start_total or now_m < end_total


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


_SYS_STATE_CACHE = {}
_SYS_STATE_LOCK = threading.Lock()

def get_sys_state() -> dict[str, str]:
    global _SYS_STATE_CACHE
    now = time.time()
    with _SYS_STATE_LOCK:
        if _SYS_STATE_CACHE and now - _SYS_STATE_CACHE.get('time', 0) < 10:
            return _SYS_STATE_CACHE['val']
            
    gov = read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    epp = read_text("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference")
    if Path("/sys/devices/system/cpu/intel_pstate/no_turbo").exists():
        turbo = "ON" if read_text("/sys/devices/system/cpu/intel_pstate/no_turbo", "1") == "0" else "OFF"
    elif Path("/sys/devices/system/cpu/cpufreq/boost").exists():
        turbo = "ON" if read_text("/sys/devices/system/cpu/cpufreq/boost", "0") == "1" else "OFF"
    elif Path("/sys/devices/system/cpu/amd_pstate/boost").exists():
        turbo = "ON" if read_text("/sys/devices/system/cpu/amd_pstate/boost", "0") == "1" else "OFF"
    else:
        turbo = "OFF"
    
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


def power_profile() -> str:
    return cached_run("powerprofile", ["powerprofilesctl", "get"], 5) or "unknown"


_CACHED_TEMP_FILE: str | None = None

def cpu_temp_c() -> int:
    global _CACHED_TEMP_FILE
    if _CACHED_TEMP_FILE:
        raw = int(read_text(_CACHED_TEMP_FILE, "0") or "0")
        if raw > 0:
            return raw // 1000

    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        name = read_text(hwmon / "name", "")
        if name not in {"coretemp", "k10temp", "zenpower", "amd_energy"}:
            continue
        for label_file in hwmon.glob("temp*_label"):
            label = read_text(label_file, "")
            if label in {"Package id 0", "Tctl", "Tdie", "Tccd1", "Tccd2"}:
                target = str(label_file).replace("_label", "_input")
                raw = int(read_text(target, "0") or "0")
                _CACHED_TEMP_FILE = target
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

    data = list(csv.DictReader([lines[0]] + lines[-limit:]))
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


def status_payload() -> dict[str, Any]:
    config = read_config()
    rows = history()
    gpu = gpu_stats()
    profile = power_profile()
    mode = config.get("AUTO_MODE", "dynamic")
    cpu_load = cpu_load_percent()
    cpu_temp = cpu_temp_c()
    ambient = ambient_temp(config)
    base_thresholds = mode_thresholds(mode, config)
    thresholds = apply_ambient_adjustment(base_thresholds, ambient)
    pause = pause_payload(config)
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
            "decision": decision_reason(mode, profile, cpu_temp, cpu_load, thresholds, pause),
        },
        "web": {"service": active_service("boost-web.service"), "url": f"http://{HOST}:{PORT}"},
        "profile": profile,
        "friendlyProfile": {"performance": "Performance", "balanced": "Balanced", "power-saver": "Eco Mode"}.get(profile, profile),
        "cpu": {"load": cpu_load, "temp": cpu_temp},
        "gpu": gpu,
        "limits": {"pl1": rapl_w(0), "pl2": rapl_w(1)},
        "system": get_sys_state(),
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
            "lowPct": int(config.get("BATTERY_LOW_PCT", "20")),
            "criticalPct": int(config.get("BATTERY_CRITICAL_PCT", "10")),
        },
    }


def run_action(action: str, value: str | None = None) -> dict[str, Any]:
    global _SNOOZE_WEB_CACHE
    allowed_modes = {"dynamic", "gaming", "creator", "quiet", "off"}
    allowed_durations = {"30m", "1h", "2h", "4h"}
    if action == "boost":
        result = run(["/usr/local/bin/boost"], timeout=30)
    elif action == "powersave":
        result = run(["/usr/local/bin/powersave"], timeout=30)
    elif action == "silent":
        result = run(["/usr/local/bin/silent", "--auto"], timeout=30)
    elif action == "restore":
        result = run(["/usr/local/bin/restore"], timeout=30)
    elif action == "auto-mode" and value in allowed_modes:
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
    elif action == "report":
        result = run(["/usr/local/bin/power-report"], timeout=10)
    elif action == "save-config" and value is not None:
        try:
            updates = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {"ok": False, "message": "Invalid JSON for config update."}
        if not isinstance(updates, dict):
            return {"ok": False, "message": "Config must be a JSON object."}
        # Validate known keys
        known_keys = {
            "TEMP_CRITICAL", "TEMP_HOT", "BOOST_TEMP_LIMIT",
            "LOAD_HIGH", "LOAD_HIGH_DURATION", "LOAD_IDLE", "LOAD_IDLE_DURATION",
            "PROMPT_COOLDOWN", "QUIET_HOURS_START", "QUIET_HOURS_END",
            "SUMMER_SILENT_NIGHTS", "ALLOW_CRITICAL_AUTO",
            "POLL_INTERVAL", "STATS_INTERVAL",
            "AC_PROFILE", "BATTERY_PROFILE", "BATTERY_LOW_PCT", "BATTERY_CRITICAL_PCT", "BATTERY_LOW_NOTIFY",
        }
        for key in updates:
            if key not in known_keys:
                return {"ok": False, "message": f"Unknown config key: {key}"}
        if write_config(updates):
            with _CONFIG_LOCK:
                _CONFIG_CACHE_MTIME = -1  # force re-read
            return {"ok": True, "message": "Configuration saved."}
        return {"ok": False, "message": "Failed to write config."}
    else:
        return {"ok": False, "message": "Unknown action."}

    if result.returncode == 0:
        if action in ("boost", "powersave", "silent", "restore"):
            with _SYS_STATE_LOCK:
                _SYS_STATE_CACHE.clear()
            with _CACHE_LOCK:
                _CACHE.pop("powerprofile", None)
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


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Boost Power Manager — premium Linux power profile control dashboard">
<title>Boost Control Dashboard</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>⚡</text></svg>">
<style>
:root{
  color-scheme:dark;
  --bg-deep:#050912;
  --bg-surface:rgba(13,24,45,0.55);
  --bg-surface-hover:rgba(13,24,45,0.75);
  --panel-border:rgba(255,255,255,0.06);
  --panel-border-hover:rgba(255,255,255,0.12);
  --text-main:#f1f5f9;
  --text-secondary:#cbd5e1;
  --text-muted:#64748b;
  --accent:#0ea5e9;
  --accent-glow:rgba(14,165,233,0.3);
  --color-boost:#f43f5e;
  --color-boost-glow:rgba(244,63,94,0.35);
  --color-powersave:#10b981;
  --color-powersave-glow:rgba(16,185,129,0.35);
  --color-silent:#8b5cf6;
  --color-silent-glow:rgba(139,92,246,0.35);
  --color-warn:#f59e0b;
  --color-danger:#ef4444;
  --color-ok:#10b981;
  --font-title:'Outfit',system-ui,sans-serif;
  --font-body:'Inter',system-ui,sans-serif;
  --radius:16px;
  --radius-sm:10px;
  --transition:0.3s cubic-bezier(0.4,0,0.2,1);
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
  background:var(--bg-deep);
  color:var(--text-main);
  font-family:var(--font-body);
  line-height:1.6;
  -webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;
  overflow-x:hidden;
}
/* Animated mesh gradient background */
body::before{
  content:'';position:fixed;inset:0;z-index:-1;
  background:
    radial-gradient(ellipse 80% 50% at 20% 20%, rgba(14,165,233,0.08) 0%, transparent 60%),
    radial-gradient(ellipse 60% 40% at 80% 80%, rgba(139,92,246,0.06) 0%, transparent 50%),
    radial-gradient(ellipse 50% 60% at 50% 0%, rgba(244,63,94,0.04) 0%, transparent 50%);
  animation:bg-drift 20s ease-in-out infinite alternate;
}
@keyframes bg-drift{
  0%{opacity:0.6;transform:scale(1) translate(0,0)}
  50%{opacity:1;transform:scale(1.05) translate(-1%,2%)}
  100%{opacity:0.7;transform:scale(1) translate(1%,-1%)}
}
button,input,select{font:inherit}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

/* Layout */
main{max-width:1240px;margin:0 auto;padding:40px 24px 60px}
@media(max-width:640px){main{padding:20px 12px 40px}}

/* Header */
.header{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:20px;margin-bottom:36px}
.header-left h1{
  font-family:var(--font-title);font-weight:800;font-size:clamp(28px,5vw,40px);
  background:linear-gradient(135deg,#38bdf8 0%,#818cf8 40%,#f43f5e 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text;letter-spacing:-0.03em;line-height:1.2;
}
.header-left .tagline{color:var(--text-muted);font-size:14px;margin-top:4px;font-weight:400}
.status-badge{
  display:flex;align-items:center;gap:10px;
  background:var(--bg-surface);border:1px solid var(--panel-border);
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border-radius:var(--radius);padding:14px 20px;
  animation:fade-up 0.6s var(--transition) both;animation-delay:0.1s;
}
.status-indicator{width:10px;height:10px;border-radius:50%;flex-shrink:0;animation:pulse-dot 2s ease-in-out infinite}
.status-indicator.active{background:var(--color-ok);box-shadow:0 0 12px var(--color-powersave-glow)}
.status-indicator.inactive{background:var(--color-danger);box-shadow:0 0 12px var(--color-boost-glow)}
@keyframes pulse-dot{0%,100%{transform:scale(0.9);opacity:0.7}50%{transform:scale(1.15);opacity:1}}
.status-text{font-weight:600;font-size:14px}
.status-sub{color:var(--text-muted);font-size:11px;margin-top:2px}

/* Connection bar */
.conn-bar{
  position:fixed;top:0;left:0;right:0;z-index:10000;
  background:rgba(239,68,68,0.9);color:#fff;text-align:center;
  padding:8px;font-size:13px;font-weight:600;
  transform:translateY(-100%);transition:transform 0.3s ease;
}
.conn-bar.show{transform:translateY(0)}

/* Cards */
.card{
  background:var(--bg-surface);
  border:1px solid var(--panel-border);
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border-radius:var(--radius);padding:24px;
  box-shadow:0 4px 24px rgba(0,0,0,0.2);
  transition:border-color var(--transition),box-shadow var(--transition),transform var(--transition);
}
.card:hover{
  border-color:var(--panel-border-hover);
  box-shadow:0 8px 32px rgba(0,0,0,0.3);
  transform:translateY(-2px);
}
/* Staggered entrance */
.card{animation:fade-up 0.5s var(--transition) both}
@keyframes fade-up{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}

/* Gauges */
.gauges-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px;margin-bottom:32px}
.gauge-card{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:28px 20px}
.gauge-card:nth-child(1){animation-delay:0.05s}.gauge-card:nth-child(2){animation-delay:0.1s}
.gauge-card:nth-child(3){animation-delay:0.15s}.gauge-card:nth-child(4){animation-delay:0.2s}
.gauge-wrapper{position:relative;width:130px;height:130px;margin-bottom:14px}
.gauge-svg{transform:rotate(-90deg);width:130px;height:130px}
.gauge-bg{fill:none;stroke:rgba(255,255,255,0.04);stroke-width:10}
.gauge-fill{fill:none;stroke-width:10;stroke-linecap:round;transition:stroke-dashoffset 0.8s cubic-bezier(0.4,0,0.2,1),stroke 0.4s ease}
.gauge-value{
  position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  font-family:var(--font-title);font-weight:700;font-size:26px;
  transition:color 0.4s ease;
}
.gauge-value small{font-size:13px;font-weight:500;color:var(--text-muted)}
.gauge-label{color:var(--text-muted);font-size:11px;text-transform:uppercase;letter-spacing:0.1em;font-weight:600}

/* Temp danger glow */
.gauge-card.temp-alert{
  border-color:rgba(239,68,68,0.4) !important;
  box-shadow:0 0 40px rgba(239,68,68,0.15),0 4px 24px rgba(0,0,0,0.2) !important;
  animation:fade-up 0.5s var(--transition) both, temp-pulse 2s ease-in-out infinite !important;
}
@keyframes temp-pulse{0%,100%{box-shadow:0 0 30px rgba(239,68,68,0.1)}50%{box-shadow:0 0 50px rgba(239,68,68,0.25)}}

/* Detail items */
.stats-details{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:16px;width:100%;margin-top:16px;border-top:1px solid rgba(255,255,255,0.04);padding-top:16px}
.detail-item{display:flex;flex-direction:column;gap:4px}
.detail-lbl{font-size:10px;text-transform:uppercase;color:var(--text-muted);letter-spacing:0.08em;font-weight:600}
.detail-val{font-size:15px;font-weight:600;font-variant-numeric:tabular-nums}

/* Split layout */
.split{display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,400px);gap:24px}
@media(max-width:960px){.split{grid-template-columns:1fr}}

/* Section titles */
.section-title{
  font-family:var(--font-title);font-weight:700;font-size:18px;
  margin:0 0 16px;display:flex;align-items:center;gap:8px;
  letter-spacing:-0.01em;
}

/* Controls */
.control-group{margin-bottom:24px}.control-group:last-child{margin-bottom:0}
.control-label{font-size:11px;text-transform:uppercase;letter-spacing:0.08em;color:var(--text-muted);margin-bottom:8px;font-weight:600}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px}

/* Buttons */
.btn{
  border:1px solid rgba(255,255,255,0.08);
  background:rgba(255,255,255,0.03);color:var(--text-main);
  border-radius:var(--radius-sm);padding:10px 18px;
  font-weight:500;cursor:pointer;font-size:13px;
  transition:all 0.2s ease;position:relative;overflow:hidden;
  user-select:none;
}
.btn::after{
  content:'';position:absolute;inset:0;
  background:linear-gradient(135deg,rgba(255,255,255,0.05),transparent);
  opacity:0;transition:opacity 0.2s ease;
}
.btn:hover{background:rgba(255,255,255,0.07);border-color:rgba(255,255,255,0.15);transform:translateY(-1px)}
.btn:hover::after{opacity:1}
.btn:active{transform:translateY(1px);transition-duration:0.05s}
.btn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

.btn.primary-boost{background:linear-gradient(135deg,#f43f5e,#e11d48);border-color:transparent;box-shadow:0 4px 16px var(--color-boost-glow)}
.btn.primary-boost:hover{box-shadow:0 6px 24px var(--color-boost-glow);background:linear-gradient(135deg,#fb7185,#f43f5e)}
.btn.good-save{background:linear-gradient(135deg,#10b981,#059669);border-color:transparent;box-shadow:0 4px 16px var(--color-powersave-glow)}
.btn.good-save:hover{box-shadow:0 6px 24px var(--color-powersave-glow);background:linear-gradient(135deg,#34d399,#10b981)}
.btn.silent-mode{background:linear-gradient(135deg,#8b5cf6,#7c3aed);border-color:transparent;box-shadow:0 4px 16px var(--color-silent-glow)}
.btn.silent-mode:hover{box-shadow:0 6px 24px var(--color-silent-glow);background:linear-gradient(135deg,#a78bfa,#8b5cf6)}
.btn.restore-bios{background:rgba(30,41,59,0.6);border-color:rgba(75,85,99,0.2)}
.btn.restore-bios:hover{background:rgba(51,65,85,0.6)}
.btn.active-preset{border-color:var(--accent) !important;box-shadow:0 0 16px var(--accent-glow) !important;background:rgba(14,165,233,0.12) !important}

/* Keyboard shortcut hints */
.btn .kbd{
  display:inline-block;background:rgba(0,0,0,0.3);border:1px solid rgba(255,255,255,0.1);
  border-radius:4px;padding:1px 6px;font-size:10px;margin-left:8px;
  font-family:var(--font-body);font-weight:600;color:var(--text-muted);
  vertical-align:middle;
}

/* Field rows */
.field-row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;background:rgba(255,255,255,0.015);border:1px solid rgba(255,255,255,0.04);padding:14px;border-radius:12px}
.field-row label{display:flex;flex-direction:column;gap:4px;font-size:10px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.06em;font-weight:600}
.field-row input{width:90px;background:rgba(5,9,18,0.8);color:var(--text-main);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:8px 10px;text-align:center;transition:border-color 0.2s ease;font-variant-numeric:tabular-nums}
.field-row input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}

/* Decision reason */
.reason{
  border-left:3px solid var(--accent);
  background:linear-gradient(135deg,rgba(14,165,233,0.04),rgba(14,165,233,0.01));
  padding:14px 18px;border-radius:0 12px 12px 0;margin:0 0 16px;font-size:13px;
  color:var(--text-secondary);line-height:1.6;
}

/* Chart */
.chart-container{background:rgba(5,9,18,0.4);border:1px solid rgba(255,255,255,0.04);border-radius:12px;padding:16px;margin-top:12px}
.chart-svg{width:100%;height:200px;display:block}
.chart-legend{display:flex;gap:20px;justify-content:center;margin-top:10px;font-size:11px;color:var(--text-muted)}
.legend-item{display:flex;align-items:center;gap:6px}
.legend-dot{width:10px;height:10px;border-radius:3px}

/* Tables */
.table-wrap{overflow-x:auto;border-radius:12px;border:1px solid rgba(255,255,255,0.04);margin-top:12px}
table{width:100%;border-collapse:collapse;background:rgba(5,9,18,0.3)}
th,td{text-align:left;padding:12px 16px;border-bottom:1px solid rgba(255,255,255,0.03);white-space:nowrap;font-variant-numeric:tabular-nums}
th{color:var(--text-muted);font-size:10px;text-transform:uppercase;letter-spacing:0.08em;background:rgba(255,255,255,0.015);font-weight:600}
tr:last-child td{border-bottom:0}
tr:hover td{background:rgba(255,255,255,0.015)}
tr.active-preset td{background:rgba(14,165,233,0.06)}

/* Toast notifications */
#toast-container{position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;flex-direction:column-reverse;gap:10px;pointer-events:none}
.toast{
  pointer-events:auto;
  background:rgba(13,24,45,0.9);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border:1px solid var(--panel-border);border-left:3px solid var(--accent);
  padding:14px 20px;border-radius:12px;color:#fff;
  box-shadow:0 16px 48px rgba(0,0,0,0.4);
  font-weight:500;font-size:13px;
  opacity:0;transform:translateX(100%);
  animation:toast-slide-in 0.4s cubic-bezier(0.16,1,0.3,1) forwards;
  max-width:360px;
}
.toast.error{border-left-color:var(--color-danger)}
.toast.hide{animation:toast-slide-out 0.3s ease forwards}
@keyframes toast-slide-in{to{opacity:1;transform:translateX(0)}}
@keyframes toast-slide-out{to{opacity:0;transform:translateX(100%)}}

/* Footer */
.footer{
  text-align:center;padding:32px 0 16px;
  color:var(--text-muted);font-size:11px;
  border-top:1px solid rgba(255,255,255,0.03);margin-top:48px;
}
.footer kbd{
  display:inline-block;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);
  border-radius:4px;padding:2px 6px;font-size:10px;font-family:var(--font-body);
}

/* Skeleton loading */
.skeleton{background:linear-gradient(90deg,rgba(255,255,255,0.03) 0%,rgba(255,255,255,0.06) 50%,rgba(255,255,255,0.03) 100%);background-size:200% 100%;animation:shimmer 1.5s infinite;border-radius:6px}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}

/* Scrollbar */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.08);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,0.15)}
</style>
</head>
<body>
<div class="conn-bar" id="connBar">⚠ Connection lost — reconnecting...</div>
<div id="toast-container"></div>
<main>
<div class="header">
  <div class="header-left">
    <h1>Boost Control Panel</h1>
    <div class="tagline">Linux power profile manager • Intel / AMD + NVIDIA</div>
  </div>
  <div class="status-badge">
    <div>
      <div style="display:flex;align-items:center;gap:8px"><span id="serviceDot" class="status-indicator active"></span><span class="status-text" id="serviceText">Initializing...</span></div>
      <div class="status-sub" id="updatedText">Connecting to telemetry...</div>
    </div>
  </div>
</div>

<section class="gauges-grid" aria-label="Live system metrics">
  <div class="card gauge-card" id="cpuLoadCard">
    <div class="gauge-wrapper">
      <svg class="gauge-svg" viewBox="0 0 130 130">
        <circle class="gauge-bg" cx="65" cy="65" r="54" />
        <circle id="cpuLoadCircle" class="gauge-fill" cx="65" cy="65" r="54" style="stroke:#0ea5e9;filter:drop-shadow(0 0 8px rgba(14,165,233,0.4))" />
      </svg>
      <div class="gauge-value"><span id="cpuLoadText">—</span><small>%</small></div>
    </div>
    <div class="gauge-label">CPU Load</div>
  </div>

  <div class="card gauge-card" id="cpuTempCard">
    <div class="gauge-wrapper">
      <svg class="gauge-svg" viewBox="0 0 130 130">
        <circle class="gauge-bg" cx="65" cy="65" r="54" />
        <circle id="cpuTempCircle" class="gauge-fill" cx="65" cy="65" r="54" style="stroke:#f59e0b;filter:drop-shadow(0 0 8px rgba(245,158,11,0.4))" />
      </svg>
      <div class="gauge-value"><span id="cpuTempText">—</span><small>°C</small></div>
    </div>
    <div class="gauge-label">CPU Temperature</div>
  </div>

  <div class="card gauge-card" style="animation-delay:0.15s">
    <div class="gauge-label" style="margin-bottom:12px;font-size:12px">GPU Metrics</div>
    <div style="font-family:var(--font-title);font-size:28px;font-weight:700;line-height:1.2"><span id="gpuPowerText">—</span><small style="font-size:14px;color:var(--text-muted);font-weight:400"> W</small></div>
    <div style="color:var(--text-muted);font-size:12px;margin-top:4px">of <span id="gpuLimitText">—</span> W limit</div>
    <div style="margin-top:12px;font-size:13px;color:var(--text-secondary)">🌡️ GPU Temp: <strong id="gpuTempText" style="color:var(--text-main)">—</strong> °C</div>
  </div>

  <div class="card gauge-card" style="animation-delay:0.18s">
    <div class="gauge-label" style="margin-bottom:12px;font-size:12px">Battery</div>
    <div style="font-family:var(--font-title);font-size:28px;font-weight:700;line-height:1.2"><span id="batteryPctText">—</span><small style="font-size:14px;color:var(--text-muted);font-weight:400"> %</small></div>
    <div style="color:var(--text-muted);font-size:12px;margin-top:4px">Status: <span id="batteryStatusText">—</span></div>
    <div style="margin-top:8px;font-size:13px;color:var(--text-secondary)">🔌 AC: <strong id="acOnlineText" style="color:var(--text-main)">—</strong></div>
    <div style="margin-top:4px;font-size:12px;color:var(--text-muted)" id="batteryTimeRow" hidden>⏱ <span id="batteryTimeText">—</span></div>
  </div>

  <div class="card gauge-card" style="animation-delay:0.2s">
    <div class="gauge-label" style="margin-bottom:12px;font-size:12px">System Config</div>
    <div class="stats-details" style="margin-top:0;border:none;padding:0;grid-template-columns:1fr 1fr;gap:12px">
      <div class="detail-item"><div class="detail-lbl">Profile</div><div class="detail-val" id="profile">—</div></div>
      <div class="detail-item"><div class="detail-lbl">Auto Mode</div><div class="detail-val" id="autoMode">—</div></div>
      <div class="detail-item"><div class="detail-lbl">Turbo</div><div class="detail-val" id="turbo">—</div></div>
      <div class="detail-item"><div class="detail-lbl">RAPL PL1/PL2</div><div class="detail-val" id="limits">—</div></div>
      <div class="detail-item"><div class="detail-lbl">THP</div><div class="detail-val" id="thp">—</div></div>
    </div>
  </div>
</section>

<div class="split">
  <div style="display:flex;flex-direction:column;gap:24px">
    <section class="card" style="animation-delay:0.25s">
      <div class="section-title">⚡ Power Profiles & Auto Modes</div>

      <div class="control-group">
        <div class="control-label">Manual Profile Override</div>
        <div style="color:var(--text-muted);font-size:12px;margin-bottom:12px">Selecting a manual profile disables Auto mode</div>
        <div class="actions">
          <button class="btn primary-boost" id="btn-boost" data-action="boost" aria-label="Activate Performance Profile" title="Switch to maximum power limit for demanding tasks">🚀 Performance <span class="kbd">1</span></button>
          <button class="btn good-save" id="btn-powersave" data-action="powersave" aria-label="Activate Balanced Profile" title="Switch to balanced power, ideal for 95% of daily use">⚖️ Balanced <span class="kbd">2</span></button>
          <button class="btn silent-mode" id="btn-silent" data-action="silent" aria-label="Activate Eco Mode" title="Strict thermal and noise constraints, best for night time">🍃 Eco Mode <span class="kbd">3</span></button>
          <button class="btn restore-bios" id="btn-restore" data-action="restore" aria-label="Restore BIOS Defaults" title="Reset all changes back to BIOS defaults">♻️ Default <span class="kbd">4</span></button>
        </div>
      </div>

      <div class="control-group" aria-labelledby="heading-auto-modes">
        <h2 id="heading-auto-modes" class="control-label" style="font-size:14px;color:var(--text-main);margin-bottom:4px;">🤖 Smart Auto Modes</h2>
        <div class="reason">
          <strong style="color:var(--text-main);display:block;margin-bottom:4px;font-size:14px" id="decisionReasonCard">Loading...</strong>
        </div>
        <div class="actions">
          <button class="btn" id="mode-dynamic" data-action="auto-mode" data-value="dynamic" aria-label="Enable Dynamic Mode" title="Balanced suggestions adapted to everyday workloads">🧠 Dynamic</button>
          <button class="btn" id="mode-gaming" data-action="auto-mode" data-value="gaming" aria-label="Enable Gaming Mode" title="Optimized for gaming sessions">🎮 Gaming</button>
          <button class="btn" id="mode-creator" data-action="auto-mode" data-value="creator" aria-label="Enable Creator Mode" title="Optimized for 3D rendering and AI training limits">🎬 Creator (Render/AI)</button>
          <button class="btn" id="mode-quiet" data-action="auto-mode" data-value="quiet" aria-label="Enable Quiet Mode" title="Strict low noise and heat profile">🤫 Quiet</button>
          <button class="btn" id="mode-off" data-action="auto-mode" data-value="off" style="color:var(--color-danger)" aria-label="Disable Auto Mode" title="Disable background automation daemon completely">🚫 Off</button>
        </div>
      </div>
    </section>

    <section class="card" style="animation-delay:0.3s">
      <div class="section-title">📊 Live Telemetry <span style="font-size:12px;color:var(--text-muted);font-weight:400;font-family:var(--font-body)">(last 30 samples)</span></div>
      <div class="chart-container">
        <svg id="historyChart" class="chart-svg" viewBox="0 0 1000 200" preserveAspectRatio="none">
          <text x="50%" y="50%" text-anchor="middle" fill="#64748b" font-size="13" font-family="Inter,sans-serif">Collecting telemetry data...</text>
        </svg>
        <div class="chart-legend">
          <div class="legend-item"><span class="legend-dot" style="background:#0ea5e9"></span><span>CPU Load (%)</span></div>
          <div class="legend-item"><span class="legend-dot" style="background:#f59e0b"></span><span>CPU Temp (°C)</span></div>
          <div class="legend-item"><span class="legend-dot" style="background:#ec4899"></span><span>GPU Power (W/200)</span></div>
          <div class="legend-item"><span class="legend-dot" style="background:#22c55e"></span><span>Battery (%)</span></div>
        </div>
      </div>
    </section>
  </div>

  <aside style="display:flex;flex-direction:column;gap:24px">
    <section class="card" style="animation-delay:0.3s">
      <div class="section-title">⏳ Auto Pause & Snooze</div>
      <div class="control-group">
        <div class="control-label">Snooze suggestions for:</div>
        <div class="actions" style="margin-bottom:16px">
          <button class="btn" data-action="snooze" data-value="30m">30m</button>
          <button class="btn" data-action="snooze" data-value="1h">1h</button>
          <button class="btn" data-action="snooze" data-value="2h">2h</button>
          <button class="btn" data-action="snooze" data-value="4h">4h</button>
          <button class="btn" data-action="today-off">All Today</button>
        </div>
        <button class="btn good-save" style="width:100%;text-align:center" data-action="resume">▶ Resume Auto</button>
      </div>
      <div class="stats-details" style="margin-top:16px">
        <div class="detail-item"><div class="detail-lbl">Status</div><div class="detail-val" id="pauseState">—</div></div>
        <div class="detail-item"><div class="detail-lbl">Reason</div><div class="detail-val" style="font-size:12px;color:var(--text-muted)" id="pauseReason">—</div></div>
      </div>
    </section>

    <section class="card" style="animation-delay:0.35s">
      <div class="section-title">🌙 Quiet Hours & Summer Nights</div>
      <div class="control-group">
        <div class="control-label">Quiet hours schedule</div>
        <div style="color:var(--text-muted);font-size:12px;margin-bottom:10px">No prompts during quiet hours</div>
        <div class="field-row">
          <label>Start <input id="quietStart" value="22:00" placeholder="HH:MM"></label>
          <label>End <input id="quietEnd" value="08:00" placeholder="HH:MM"></label>
          <button class="btn active-preset" id="saveQuiet" style="margin-top:14px;width:100%">Save Schedule</button>
        </div>
      </div>
      <div class="control-group" style="margin-top:20px">
        <div class="control-label">Summer Nights</div>
        <div style="color:var(--text-muted);font-size:12px;margin-bottom:10px">Auto-enable Silent overnight in Summer mode</div>
        <div class="actions">
          <button class="btn good-save" id="summer-nights-on" data-action="summer-nights" data-value="on">Enable</button>
          <button class="btn" id="summer-nights-off" data-action="summer-nights" data-value="off">Disable</button>
        </div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:8px">State: <strong id="summerNights" style="color:var(--text-main)">—</strong></div>
      </div>
    </section>

    <section class="card" style="animation-delay:0.4s">
      <div class="section-title">📋 Reports & Averages</div>
      <div class="stats-details" style="margin-top:0;border:none;padding:0;grid-template-columns:1fr 1fr;gap:12px">
        <div class="detail-item"><div class="detail-lbl">Avg CPU</div><div class="detail-val" id="avgCpu">—</div></div>
        <div class="detail-item"><div class="detail-lbl">Max Temp</div><div class="detail-val" id="maxTemp">—</div></div>
        <div class="detail-item"><div class="detail-lbl">Avg GPU</div><div class="detail-val" id="avgGpu">—</div></div>
        <div class="detail-item"><div class="detail-lbl">Governor</div><div class="detail-val" id="epp">—</div></div>
      </div>
      <div class="actions" style="margin-top:20px;flex-direction:column;gap:8px">
        <button class="btn" style="width:100%" data-action="report">Generate HTML Report</button>
        <a class="btn" style="width:100%;text-align:center;display:block" href="/report" target="_blank" rel="noreferrer">Open Latest Report ↗</a>
      </div>
      <p style="font-size:10px;color:var(--text-muted);margin-top:8px;word-break:break-all" id="reportPath">—</p>
    </section>
  </aside>
</div>

<section class="card" style="margin-top:24px;animation-delay:0.45s">
  <div class="section-title">🧠 Auto Switch Decision Engine</div>
  <p class="reason" id="decisionReason">—</p>
  <div class="stats-details" style="grid-template-columns:repeat(auto-fit,minmax(130px,1fr));border-top:none;padding-top:0">
    <div class="detail-item"><div class="detail-lbl">Warm Threshold</div><div class="detail-val" id="tempHot">—</div></div>
    <div class="detail-item"><div class="detail-lbl">Critical Temp</div><div class="detail-val" id="tempCritical">—</div></div>
    <div class="detail-item"><div class="detail-lbl">Boost Below</div><div class="detail-val" id="boostLimit">—</div></div>
    <div class="detail-item"><div class="detail-lbl">Busy Trigger</div><div class="detail-val" id="busyTrigger">—</div></div>
    <div class="detail-item"><div class="detail-lbl">Idle Trigger</div><div class="detail-val" id="idleTrigger">—</div></div>
    <div class="detail-item"><div class="detail-lbl">Cooldown</div><div class="detail-val" id="cooldown">—</div></div>
  </div>
</section>

<section style="margin-top:32px">
  <h2 style="font-family:var(--font-title);font-size:22px;font-weight:700;margin-bottom:12px">Preset Threshold Reference</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>Mode</th><th>Warm</th><th>Critical</th><th>Boost Below</th><th>Busy Trigger</th><th>Idle Trigger</th><th>Cooldown</th></tr></thead>
    <tbody id="modes"></tbody>
  </table></div>
</section>

<section style="margin-top:32px">
  <h2 style="font-family:var(--font-title);font-size:22px;font-weight:700;margin-bottom:12px">Sensor History Log</h2>
  <div style="margin-bottom:10px;padding:8px 12px;background:rgba(13,24,45,0.4);border:1px solid rgba(255,255,255,0.05);border-radius:8px" id="profileSwitchLog"></div>
  <div class="table-wrap"><table>
    <thead><tr><th>Time</th><th>Profile</th><th>CPU Load</th><th>CPU Temp</th><th>GPU</th><th>RAPL</th></tr></thead>
    <tbody id="history"></tbody>
  </table></div>
</section>

<section style="margin-top:32px">
  <h2 style="font-family:var(--font-title);font-size:22px;font-weight:700;margin-bottom:12px">⚙️ Configuration</h2>
  <div class="card" style="padding:20px">
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px" id="configGrid">
      <div class="detail-item">
        <div class="detail-lbl">Critical Temp</div>
        <input id="cfg_TEMP_CRITICAL" type="number" min="60" max="100" value="85" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Warm Threshold</div>
        <input id="cfg_TEMP_HOT" type="number" min="50" max="95" value="78" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Boost Below</div>
        <input id="cfg_BOOST_TEMP_LIMIT" type="number" min="50" max="95" value="78" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Busy Load %</div>
        <input id="cfg_LOAD_HIGH" type="number" min="10" max="100" value="75" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Busy Duration (s)</div>
        <input id="cfg_LOAD_HIGH_DURATION" type="number" min="10" max="3600" value="120" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Idle Load %</div>
        <input id="cfg_LOAD_IDLE" type="number" min="0" max="50" value="8" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Idle Duration (s)</div>
        <input id="cfg_LOAD_IDLE_DURATION" type="number" min="10" max="3600" value="600" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Cooldown (s)</div>
        <input id="cfg_PROMPT_COOLDOWN" type="number" min="10" max="7200" value="900" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Poll Interval (s)</div>
        <input id="cfg_POLL_INTERVAL" type="number" min="1" max="60" value="5" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Stats Interval (s)</div>
        <input id="cfg_STATS_INTERVAL" type="number" min="10" max="600" value="60" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Critical Auto Protect</div>
        <select id="cfg_ALLOW_CRITICAL_AUTO" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
          <option value="yes">Yes</option>
          <option value="no">No</option>
        </select>
      </div>
    </div>
      <div class="detail-item">
        <div class="detail-lbl">AC Profile</div>
        <select id="cfg_AC_PROFILE" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
          <option value="restore">Default</option>
          <option value="boost">Performance</option>
          <option value="powersave">Balanced</option>
          <option value="silent">Power Saver</option>
        </select>
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Battery Profile</div>
        <select id="cfg_BATTERY_PROFILE" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
          <option value="powersave">Balanced</option>
          <option value="silent">Power Saver</option>
          <option value="restore">Default</option>
          <option value="boost">Performance</option>
        </select>
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Battery Low %</div>
        <input id="cfg_BATTERY_LOW_PCT" type="number" min="5" max="50" value="20" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Battery Critical %</div>
        <input id="cfg_BATTERY_CRITICAL_PCT" type="number" min="3" max="30" value="10" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Low Battery Notify</div>
        <select id="cfg_BATTERY_LOW_NOTIFY" style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:6px;padding:6px 10px;color:var(--text-main);width:100%;font-size:14px">
          <option value="yes">Yes</option>
          <option value="no">No</option>
        </select>
      </div>
    <div class="actions" style="margin-top:20px">
      <button class="btn active-preset" id="saveConfigBtn" style="width:100%">💾 Save Configuration</button>
      <span style="font-size:11px;color:var(--text-muted);margin-top:8px;display:block">Changes take effect immediately. The daemon re-reads config on the next poll cycle.</span>
    </div>
  </div>
</section>

<footer class="footer">
  Boost Power Manager v1.4.0 — Keyboard: <kbd>1</kbd> Boost <kbd>2</kbd> Powersave <kbd>3</kbd> Silent <kbd>4</kbd> Restore <kbd>R</kbd> Refresh
</footer>
</main>

<script>
const $ = id => document.getElementById(id);
let _prevData = null;
let _failCount = 0;

function secondsText(s) {
  if (s >= 3600) return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;
  if (s >= 60) return `${Math.floor(s/60)}m`;
  return `${s}s`;
}

function showToast(text, isError = false) {
  const c = $('toast-container');
  const t = document.createElement('div');
  t.className = isError ? 'toast error' : 'toast';
  t.textContent = text;
  c.appendChild(t);
  setTimeout(() => { t.classList.add('hide'); setTimeout(() => t.remove(), 300); }, 4000);
}

function setGauge(id, value, max = 100) {
  const circle = $(`${id}Circle`);
  const text = $(`${id}Text`);
  if (!circle || !text) return;
  const r = 54, circ = 2 * Math.PI * r;
  circle.style.strokeDasharray = circ;
  const pct = Math.min(Math.max(value, 0), max) / max;
  circle.style.strokeDashoffset = circ - pct * circ;
  text.textContent = Math.round(value);
}

function tempColor(temp) {
  if (temp >= 85) return '#ef4444';
  if (temp >= 75) return '#f59e0b';
  if (temp >= 60) return '#fb923c';
  return '#10b981';
}

function loadColor(load) {
  if (load >= 90) return '#ef4444';
  if (load >= 70) return '#f59e0b';
  if (load >= 40) return '#0ea5e9';
  return '#10b981';
}

function drawChart(history) {
  const svg = $('historyChart');
  if (!history || !history.length) {
    svg.innerHTML = '<text x="50%" y="50%" text-anchor="middle" fill="#64748b" font-size="13" font-family="Inter,sans-serif">Waiting for telemetry data...</text>';
    return;
  }
  const W = 1000, H = 200, pL = 45, pR = 15, pT = 15, pB = 25;
  const cW = W - pL - pR, cH = H - pT - pB;
  const n = history.length;

  function makePath(data, key, max, color) {
    let pts = [], areaPts = [], circles = [];
    for (let i = 0; i < n; i++) {
      const x = pL + (n > 1 ? (i/(n-1)) * cW : cW/2);
      const v = Math.min(parseFloat(data[i][key] || 0), max);
      const y = H - pB - (v/max) * cH;
      pts.push(`${x},${y}`);
      areaPts.push(`${x},${y}`);
      const timeStr = data[i].iso ? data[i].iso.split('T')[1].substring(0,5) : '';
      circles.push(`<circle cx="${x}" cy="${y}" r="3" fill="${color}" opacity="0">
                      <title>${timeStr} | ${key}: ${parseFloat(data[i][key]||0).toFixed(1)}</title>
                    </circle>`);
    }
    const lineD = `M ${pts.join(' L ')}`;
    const areaD = `${lineD} L ${pL + cW},${H - pB} L ${pL},${H - pB} Z`;
    return `<path d="${areaD}" fill="url(#grad-${color})" opacity="0.15"/>
            <path d="${lineD}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <g class="chart-points" style="pointer-events:all;cursor:crosshair">
              ${circles.join('')}
              <style>.chart-points circle:hover { opacity: 1 !important; stroke: #fff; stroke-width: 1px; }</style>
            </g>`;
  }

  let grid = '';
  for (let p = 0; p <= 100; p += 25) {
    const y = H - pB - (p/100) * cH;
    grid += `<line x1="${pL}" y1="${y}" x2="${W-pR}" y2="${y}" stroke="rgba(255,255,255,0.04)" stroke-width="1"/>`;
    grid += `<text x="${pL-8}" y="${y+4}" fill="#475569" font-size="9" font-family="Inter,sans-serif" text-anchor="end">${p}</text>`;
  }

  // GPU power mapped to 0-200W scale shown as percentage
  let gpuData = history.map(r => ({...r, gpu_pct: String((parseFloat(r.gpu_power||0)/200)*100)}));

  // Profile transition bands — colored vertical strips when profile changes
  const profileColors = {performance: '#f43f5e', balanced: '#10b981', 'power-saver': '#8b5cf6'};
  let bands = '';
  let prevProfile = history[0]?.profile;
  for (let i = 1; i < n; i++) {
    const p = history[i].profile;
    if (p && p !== prevProfile) {
      const x = pL + (i/(n-1)) * cW;
      const color = profileColors[p] || '#94a3b8';
      bands += `<line x1="${x}" y1="${pT}" x2="${x}" y2="${H-pB}" stroke="${color}" stroke-width="1.5" stroke-dasharray="3,2" opacity="0.6">
                  <title>→ ${p}</title>
                </line>`;
      prevProfile = p;
    }
  }

  svg.innerHTML = `
    <defs>
      <linearGradient id="grad-#0ea5e9" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0ea5e9"/><stop offset="1" stop-color="#0ea5e9" stop-opacity="0"/></linearGradient>
      <linearGradient id="grad-#f59e0b" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f59e0b"/><stop offset="1" stop-color="#f59e0b" stop-opacity="0"/></linearGradient>
      <linearGradient id="grad-#ec4899" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ec4899"/><stop offset="1" stop-color="#ec4899" stop-opacity="0"/></linearGradient>
      <linearGradient id="grad-#22c55e" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#22c55e"/><stop offset="1" stop-color="#22c55e" stop-opacity="0"/></linearGradient>
    </defs>
    ${grid}
    ${bands}
    ${makePath(history, 'cpu_load', 100, '#0ea5e9')}
    ${makePath(history, 'cpu_temp', 100, '#f59e0b')}
    ${makePath(gpuData, 'gpu_pct', 100, '#ec4899')}
    ${makePath(history, 'battery_pct', 100, '#22c55e')}
  `;
}

function render(data) {
  // Connection status
  $('connBar').classList.remove('show');
  _failCount = 0;

  // Service status
  const isActive = data.auto.service === 'active';
  $('serviceDot').className = `status-indicator ${isActive ? 'active' : 'inactive'}`;
  $('serviceText').textContent = `Auto: ${data.auto.service} • Web: ${data.web.service}`;
  $('updatedText').textContent = `Live • ${data.time}`;

  // Gauges with dynamic colors
  setGauge('cpuLoad', data.cpu.load);
  setGauge('cpuTemp', data.cpu.temp);

  const loadCircle = $('cpuLoadCircle');
  const lc = loadColor(data.cpu.load);
  loadCircle.style.stroke = lc;
  loadCircle.style.filter = `drop-shadow(0 0 8px ${lc}40)`;

  const tempCircle = $('cpuTempCircle');
  const tc = tempColor(data.cpu.temp);
  tempCircle.style.stroke = tc;
  tempCircle.style.filter = `drop-shadow(0 0 8px ${tc}40)`;

  // Temp danger state
  const tempCard = $('cpuTempCard');
  if (data.cpu.temp >= 80) { tempCard.classList.add('temp-alert'); }
  else { tempCard.classList.remove('temp-alert'); }

  // GPU
  $('gpuPowerText').textContent = data.gpu.power;
  $('gpuLimitText').textContent = data.gpu.limit;
  $('gpuTempText').textContent = data.gpu.temp;


  // Battery
  const bat = data.battery;
  if (bat && bat.pct !== null && bat.pct !== undefined) {
    $('batteryPctText').textContent = bat.pct;
    $('batteryStatusText').textContent = bat.status;
    $('acOnlineText').textContent = bat.acOnline === 1 ? 'Connected' : bat.acOnline === 0 ? 'On Battery' : '—';
    const batEl = $('batteryPctText');
    if (bat.pct <= bat.criticalPct) batEl.style.color = '#ef4444';
    else if (bat.pct <= bat.lowPct) batEl.style.color = '#f59e0b';
    else batEl.style.color = '';
    // Show drain rate and estimated time remaining when on battery
    const timeRow = $('batteryTimeRow');
    if (bat.acOnline === 0 && bat.drainRatePctPerHour && bat.pct) {
      const hoursLeft = bat.pct / bat.drainRatePctPerHour;
      const h = Math.floor(hoursLeft);
      const m = Math.round((hoursLeft - h) * 60);
      const rateStr = `${bat.drainRatePctPerHour.toFixed(1)}%/h`;
      $('batteryTimeText').textContent = h > 0
        ? `~${h}h ${m}m remaining (${rateStr})`
        : `~${m}m remaining (${rateStr})`;
      timeRow.hidden = false;
    } else {
      timeRow.hidden = true;
    }
  } else {
    $('batteryPctText').textContent = '—';
    $('batteryStatusText').textContent = 'No battery';
    $('acOnlineText').textContent = '—';
    $('batteryTimeRow').hidden = true;
  }
  // System config
  $('profile').textContent = data.friendlyProfile;
  $('autoMode').textContent = data.auto.mode;
  $('limits').textContent = `${data.limits.pl1}/${data.limits.pl2} W`;
  $('turbo').textContent = data.system.turbo;
  $('thp').textContent = data.system.thp || '—';

  // Pause
  const p = data.auto.pause;
  $('pauseState').textContent = p.snoozed ? '⏸ Snoozed' : p.todayOff ? '⏸ Today off' : p.quietActive ? '🌙 Quiet hours' : '✅ Available';
  $('pauseReason').textContent = p.reason;

  // Quiet hours
  $('quietStart').value = data.auto.quietStart;
  $('quietEnd').value = data.auto.quietEnd;
  $('summerNights').textContent = data.auto.summerSilentNights.toUpperCase();

  // Decision
  $('decisionReasonCard').textContent = data.auto.decision;
  $('decisionReason').textContent = data.auto.decision;
  const t = data.auto.thresholds;
  $('tempHot').textContent = `${t.tempHot}°C`;
  $('tempCritical').textContent = `${t.tempCritical}°C`;
  $('boostLimit').textContent = `${t.boostTempLimit}°C`;
  $('busyTrigger').textContent = `${t.loadHigh}% / ${secondsText(t.loadHighDuration)}`;
  $('idleTrigger').textContent = `${t.loadIdle}% / ${secondsText(t.loadIdleDuration)}`;
  $('cooldown').textContent = secondsText(t.promptCooldown);

  // Summary
  $('avgCpu').textContent = `${Math.round(data.summary.avg_cpu)}%`;
  $('maxTemp').textContent = `${Math.round(data.summary.max_temp)}°C`;
  $('avgGpu').textContent = `${Number(data.summary.avg_gpu).toFixed(1)} W`;
  $('epp').textContent = `${data.system.governor} (${data.system.epp})`;
  $('reportPath').textContent = data.report.latestExists ? data.report.path : 'No report generated yet';

  // Chart & History Table
  const hChanged = !_prevData || !_prevData.history || data.history.length !== _prevData.history.length || (data.history.length > 0 && data.history[data.history.length-1].iso !== _prevData.history[_prevData.history.length-1].iso);
  if (hChanged) {
    drawChart(data.history);
    // Profile switch log
    const switchLog = $('profileSwitchLog');
    if (switchLog && data.profileSwitches && data.profileSwitches.length) {
      const profileLabels = {performance: 'Boost', balanced: 'Balanced', 'power-saver': 'Eco'};
      const profileDots = {performance: '#f43f5e', balanced: '#10b981', 'power-saver': '#8b5cf6'};
      switchLog.innerHTML = data.profileSwitches.slice().reverse().map(s => {
        const label = profileLabels[s.profile] || s.profile;
        const color = profileDots[s.profile] || '#94a3b8';
        const time = s.iso ? s.iso.split('T')[1].substring(0,5) : '—';
        return `<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px;font-size:12px;color:#94a3b8"><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color}"></span>${time} → <strong style="color:#f1f5f9">${label}</strong></span>`;
      }).join('');
    } else if (switchLog) {
      switchLog.innerHTML = '<span style="color:#475569;font-size:12px">No profile changes in current window</span>';
    }
    $('history').innerHTML = data.history.slice().reverse().map(r => `
      <tr>
        <td>${r.iso ? r.iso.split('T')[1].substring(0,8) : '—'}</td>
        <td style="text-transform:capitalize">${{performance:'Boost',balanced:'Balanced','power-saver':'Silent'}[r.profile]||r.profile}</td>
        <td><strong>${r.cpu_load||0}%</strong></td>
        <td style="color:${tempColor(parseInt(r.cpu_temp||0))}">${r.cpu_temp||0}°C</td>
        <td>${r.gpu_temp||0}°C / ${r.gpu_power||0}W</td>
        <td>${r.pl1||0}/${r.pl2||0}W</td>
      </tr>`).join('');
  }

  // Active profile highlight
  ['boost','powersave','silent'].forEach(a => { const b = $(`btn-${a}`); if(b) b.classList.remove('active-preset'); });
  if (data.profile === 'performance') $('btn-boost')?.classList.add('active-preset');
  else if (data.profile === 'balanced') $('btn-powersave')?.classList.add('active-preset');
  else if (data.profile === 'power-saver') $('btn-silent')?.classList.add('active-preset');

  // Active auto mode highlight
  ['dynamic','gaming','creator','quiet','off'].forEach(m => { const b = $(`mode-${m}`); if(b) b.classList.remove('active-preset'); });
  $(`mode-${data.auto.mode}`)?.classList.add('active-preset');

  // Summer nights
  if (data.auto.summerSilentNights === 'yes') {
    $('summer-nights-on').classList.add('active-preset');
    $('summer-nights-off').classList.remove('active-preset');
  } else {
    $('summer-nights-on').classList.remove('active-preset');
    $('summer-nights-off').classList.add('active-preset');
  }

  // Modes table
  const mChanged = !_prevData || _prevData.auto.mode !== data.auto.mode || JSON.stringify(_prevData.auto.modes) !== JSON.stringify(data.auto.modes);
  if (mChanged) {
    $('modes').innerHTML = data.auto.modes.map(m => `
      <tr class="${data.auto.mode === m.mode ? 'active-preset' : ''}">
        <td style="font-weight:600;text-transform:capitalize">${m.mode}</td>
        <td>${m.tempHot}°C</td><td>${m.tempCritical}°C</td><td>${m.boostTempLimit}°C</td>
        <td>${m.loadHigh}% / ${secondsText(m.loadHighDuration)}</td>
        <td>${m.loadIdle}% / ${secondsText(m.loadIdleDuration)}</td>
        <td>${secondsText(m.promptCooldown)}</td>
      </tr>`).join('');
  }

  _prevData = data;
}

async function refresh() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    render(await res.json());
  } catch (e) {
    _failCount++;
    if (_failCount >= 3) $('connBar').classList.add('show');
  }
}

async function sendAction(action, value = null) {
  // Optimistic UI Updates
  if (['boost', 'powersave', 'silent', 'restore'].includes(action)) {
    ['boost', 'powersave', 'silent'].forEach(a => { const b = $(`btn-${a}`); if(b) b.classList.remove('active-preset'); });
    if (action !== 'restore') {
      $(`btn-${action}`)?.classList.add('active-preset');
      const profileNames = {boost: 'Performance', powersave: 'Balanced', silent: 'Power-Saver'};
      if ($('profile')) $('profile').textContent = profileNames[action] || action;
    }
  } else if (action === 'auto-mode') {
    ['dynamic','gaming','creator','quiet','off'].forEach(m => { const b = $(`mode-${m}`); if(b) b.classList.remove('active-preset'); });
    $(`mode-${value}`)?.classList.add('active-preset');
    if ($('autoMode')) $('autoMode').textContent = value;
    // Also update table row highlight
    document.querySelectorAll('#modes tr').forEach(tr => tr.classList.remove('active-preset'));
    const matchedRow = Array.from(document.querySelectorAll('#modes tr')).find(tr => tr.firstElementChild?.textContent?.toLowerCase() === value);
    if (matchedRow) matchedRow.classList.add('active-preset');
  } else if (action === 'snooze') {
    if ($('pauseState')) $('pauseState').textContent = '⏸ Snoozed';
  } else if (action === 'today-off') {
    if ($('pauseState')) $('pauseState').textContent = '⏸ Today off';
  } else if (action === 'resume') {
    if ($('pauseState')) $('pauseState').textContent = '✅ Available';
  } else if (action === 'summer-nights') {
    if (value === 'on') {
      $('summer-nights-on')?.classList.add('active-preset');
      $('summer-nights-off')?.classList.remove('active-preset');
      if ($('summerNights')) $('summerNights').textContent = 'YES';
    } else {
      $('summer-nights-on')?.classList.remove('active-preset');
      $('summer-nights-off')?.classList.add('active-preset');
      if ($('summerNights')) $('summerNights').textContent = 'NO';
    }
  }

  try {
    const r = await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, value})
    });
    const result = await r.json();
    showToast(result.message || (result.ok ? 'Applied' : 'Error'), !result.ok);
    await refresh();
  } catch (e) { showToast(e.message, true); }
}

// Config UI
async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) return;
    const data = await res.json();
    if (!data.ok) return;
    const cfg = data.config;
    for (const [key, val] of Object.entries(cfg)) {
      const el = document.getElementById('cfg_' + key);
      if (el) {
        if (el.tagName === 'SELECT') {
          el.value = val;
        } else {
          el.value = val;
        }
      }
    }
  } catch (e) { /* silent */ }
}

$('saveConfigBtn')?.addEventListener('click', async () => {
  const updates = {};
  const fields = ['TEMP_CRITICAL','TEMP_HOT','BOOST_TEMP_LIMIT','LOAD_HIGH','LOAD_HIGH_DURATION','LOAD_IDLE','LOAD_IDLE_DURATION','PROMPT_COOLDOWN','POLL_INTERVAL','STATS_INTERVAL','ALLOW_CRITICAL_AUTO','AC_PROFILE','BATTERY_PROFILE','BATTERY_LOW_PCT','BATTERY_CRITICAL_PCT','BATTERY_LOW_NOTIFY'];
  for (const key of fields) {
    const el = document.getElementById('cfg_' + key);
    if (el) updates[key] = el.value;
  }
  try {
    const r = await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'save-config', value: JSON.stringify(updates)})
    });
    const result = await r.json();
    showToast(result.message || (result.ok ? 'Configuration saved' : 'Error'), !result.ok);
  } catch (e) { showToast(e.message, true); }
});

// Load config on startup
loadConfig();

// Event delegation
document.addEventListener('click', e => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  e.preventDefault();
  sendAction(btn.dataset.action, btn.dataset.value || null);
});

$('saveQuiet').addEventListener('click', () => {
  sendAction('quiet-hours', JSON.stringify({start: $('quietStart').value, end: $('quietEnd').value}));
});

// Keyboard shortcuts
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  const key = e.key.toLowerCase();
  if (key === '1') sendAction('boost');
  else if (key === '2') sendAction('powersave');
  else if (key === '3') sendAction('silent');
  else if (key === '4') sendAction('restore');
  else if (key === 'r') refresh();
});

// Start polling
refresh();
setInterval(() => {
  if (!document.hidden) refresh();
}, 2000);
</script>
</body>
</html>"""

INDEX_HTML_BYTES = INDEX_HTML.encode("utf-8")

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
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_bytes(INDEX_HTML_BYTES, "text/html; charset=utf-8")
        elif parsed.path == "/favicon.ico":
            self.send_bytes(b"", "image/x-icon")
        elif parsed.path == "/api/status":
            self.send_json(status_payload())
        elif parsed.path == "/api/config":
            self.send_json(config_payload())
        elif parsed.path == "/report":
            if LATEST_REPORT.exists():
                self.send_bytes(LATEST_REPORT.read_bytes(), "text/html; charset=utf-8")
            else:
                self.send_bytes(b"No report yet. Click Generate report first.", "text/plain; charset=utf-8", 404)
        else:
            self.send_bytes(b"Not found", "text/plain; charset=utf-8", 404)

    def _csrf_ok(self) -> bool:
        host, port = self.server.server_address
        allowed = (
            f"http://{host}:{port}",
            f"http://localhost:{port}",
            f"http://127.0.0.1:{port}",
        )
        origin = self.headers.get("Origin", "")
        referer = self.headers.get("Referer", "")
        for header in (origin, referer):
            if any(header.startswith(prefix) for prefix in allowed):
                return True
        return False

    def do_POST(self) -> None:
        if urllib.parse.urlparse(self.path).path != "/api/action":
            self.send_json({"ok": False, "message": "Not found"}, 404)
            return
        if not self._csrf_ok():
            self.send_json({"ok": False, "message": "Forbidden"}, 403)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            action = str(payload.get("action", ""))
            value = payload.get("value")
            self.send_json(run_action(action, None if value is None else str(value)))
        except Exception as exc:  # noqa: BLE001 - local UI should return readable errors
            self.send_json({"ok": False, "message": html.escape(str(exc))}, 500)


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
