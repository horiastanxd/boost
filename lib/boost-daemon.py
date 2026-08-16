#!/usr/bin/env python3
"""
Boost Power Daemon
Unified async daemon replacing the bash loop for maximum efficiency.
Features: O(1) thermal/load polling, Game Mode detection.

The O(1) claim covers temperature/load reads only (fixed sysfs paths,
cached after first resolution). read_process_set() used by Game/Creator/
Meeting detection is O(n_procs) (os.listdir + open per pid under /proc),
cached once per poll_interval rather than re-scanned every check.
"""
import os
import sys
import time
import json
import csv
import subprocess
import shlex
import syslog
import threading
from datetime import datetime

STATE_DIR = "/var/lib/power-profile"
CONF_FILE = "/etc/boost-auto.conf"
STATS_FILE = os.path.join(STATE_DIR, "stats.csv")
SNOOZE_FILE = os.path.join(STATE_DIR, "auto-snooze-until")
SKIP_TODAY_FILE = os.path.join(STATE_DIR, "auto-skip-date")
LIVE_FILE = os.path.join(STATE_DIR, "live.json")
DAILY_FILE = os.path.join(STATE_DIR, "daily.csv")
GPU_HEAVY_UTIL_PCT = 80
GPU_HEAVY_DURATION_S = 30

# Canonical mode presets, shared with lib/boost-web.py and bin/auto so the
# three implementations can never drift apart (see CHANGELOG v1.2.0).
PRESETS_FILE_CANDIDATES = [
    "/usr/local/share/boost/presets.json",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "presets.json"),
]

_presets_cache: dict = {}
_presets_cache_mtime: float = -1
_presets_cache_path: str | None = None

def load_presets() -> dict:
    global _presets_cache, _presets_cache_mtime, _presets_cache_path
    path = _presets_cache_path if _presets_cache_path and os.path.isfile(_presets_cache_path) else None
    if path is None:
        path = next((candidate for candidate in PRESETS_FILE_CANDIDATES if os.path.isfile(candidate)), None)
    if path is None:
        _presets_cache, _presets_cache_mtime, _presets_cache_path = {}, -1, None
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _presets_cache
    if mtime == _presets_cache_mtime and path == _presets_cache_path:
        return _presets_cache
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _presets_cache
    if not isinstance(data, dict):
        return _presets_cache
    _presets_cache, _presets_cache_mtime, _presets_cache_path = data, mtime, path
    return data

# Known process names for automatic Game Mode
GAME_PROCESSES = ['wine-preloader', 'wine64-preloader', 'proton', 'steam', 'cs2', 'dota2', 'hl2_linux']

# Creator workloads — suggest Performance profile for sustained rendering/compilation
CREATOR_PROCESSES = ['ffmpeg', 'blender', 'HandBrakeCLI', 'kdenlive', 'davinci', 'cargo', 'cmake', 'nvcc', 'julia']

# Video call / meeting apps — suggest Quiet mode to keep fans down and latency low
MEETING_PROCESSES = ['zoom', '.zoom', 'teams', 'slack', 'discord', 'obs', 'pipewire-camera']
AUTO_MODES = {"dynamic", "gaming", "creator", "quiet", "off", "custom"}
PROFILE_COMMANDS = {"boost", "powersave", "silent", "restore"}
YES_NO = {"yes", "no"}
LOG_WARNING = getattr(syslog, "LOG_WARNING", syslog.LOG_INFO)


class BoostDaemon:
    def __init__(self):
        syslog.openlog("boost-auto", syslog.LOG_PID, syslog.LOG_USER)
        self.mode = "dynamic"
        self.poll_interval = 5
        self.stats_interval = 60
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
        self.ac_profile = "restore"
        self.battery_profile = "powersave"
        self.battery_low_pct = 20
        self.battery_critical_pct = 10
        self.battery_low_notify = "yes"
        self.slow_charge_threshold_uw = 2_000_000  # 2W default
        self.slow_charge_battery_pct = 25
        self.slow_charge_recovery_pct = 35
        self._battery_supply = None
        self._battery_notified_low = False
        self._battery_notified_critical = False
        self._last_battery_pct = 100
        self._last_ac_online = None
        self._slow_charge_active = False
        self._slow_charge_notified = False
        self._power_samples = []
        self._screen_locked = False
        self._pre_lock_profile = None
        self.screen_lock_powersave = "yes"
        self._battery_charge_limit = 0
        self._process_cache = (0, set())

        self.cpu_temp_path, self.cpu_temp_coretemp_dir = self.find_cpu_temp_path()
        self.amd_gpu_hwmon = self.find_amd_gpu_hwmon()
        self._meeting_notified = False
        self.prev_total = 0
        self.prev_idle = 0
        
        self.high_since = 0
        self.idle_since = 0
        self.gpu_high_since = 0
        self._gpu_stats_cache = (0, ["0", "0", "0", "0"])
        self.last_switch_reason = ""
        self.last_switch_reason_text = ""
        self.last_switch_time = 0
        self._last_rollup_date = datetime.now().strftime("%Y-%m-%d")
        self.last_prompt = 0
        self.last_auto = 0
        self.last_stats = 0
        self.last_conf_mtime = -1
        self.cached_session = None
        self.cached_env = None
        self._cached_profile = None
        self._profile_cycle_count = 0
        self._snooze_cache = (0, 0, False)
        
    def log(self, msg, level=syslog.LOG_INFO):
        syslog.syslog(level, msg)

    def _config_int(self, key, value, current, minimum=None, maximum=None):
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            self.log(f"Ignoring invalid config {key}={value!r}; expected number", LOG_WARNING)
            return current
        if minimum is not None and parsed < minimum:
            self.log(f"Ignoring invalid config {key}={value!r}; below {minimum}", LOG_WARNING)
            return current
        if maximum is not None and parsed > maximum:
            self.log(f"Ignoring invalid config {key}={value!r}; above {maximum}", LOG_WARNING)
            return current
        return parsed

    def _config_choice(self, key, value, current, allowed):
        if value in allowed:
            return value
        self.log(f"Ignoring invalid config {key}={value!r}; expected one of {sorted(allowed)}", LOG_WARNING)
        return current

    def _config_hhmm(self, key, value, current):
        parts = value.split(":", 1)
        if len(parts) == 2:
            try:
                hour, minute = int(parts[0]), int(parts[1])
                if len(value) == 5 and 0 <= hour <= 23 and 0 <= minute <= 59:
                    return value
            except ValueError:
                pass
        self.log(f"Ignoring invalid config {key}={value!r}; expected HH:MM", LOG_WARNING)
        return current

    def _config_watts_uw(self, key, value, current):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            self.log(f"Ignoring invalid config {key}={value!r}; expected watts", LOG_WARNING)
            return current
        if parsed < 0:
            self.log(f"Ignoring invalid config {key}={value!r}; below 0", LOG_WARNING)
            return current
        return int(parsed * 1_000_000)

    def find_amd_gpu_hwmon(self):
        drm_base = "/sys/class/drm"
        if not os.path.exists(drm_base):
            return None
        for card in os.listdir(drm_base):
            hwmon_dir = os.path.join(drm_base, card, "device", "hwmon")
            if not os.path.isdir(hwmon_dir):
                continue
            for hwmon in os.listdir(hwmon_dir):
                hwmon_path = os.path.join(hwmon_dir, hwmon)
                name_file = os.path.join(hwmon_path, "name")
                try:
                    with open(name_file, 'r') as f:
                        if f.read().strip() == "amdgpu":
                            return hwmon_path + "/"
                except Exception:
                    pass
        return None

    def find_cpu_temp_path(self):
        """Returns (temp_input_path, coretemp_hwmon_dir_or_None).

        The second element is set only when the resolved sensor is
        coretemp's "Package id 0" label, which is defined as the max of
        all per-core Digital Thermal Sensors. A single miscalibrated
        core can pin that reading well above the motherboard's own
        PECI-based package temp; read_cpu_temp() uses the hwmon dir to
        sanity-check against the per-core median before trusting it.
        """
        hwmon_base = "/sys/class/hwmon"
        if not os.path.exists(hwmon_base):
            return None, None
        for hwmon in os.listdir(hwmon_base):
            hwmon_path = os.path.join(hwmon_base, hwmon)
            name_file = os.path.join(hwmon_path, "name")
            if os.path.exists(name_file):
                with open(name_file, 'r') as f:
                    name = f.read().strip()
                if name in ['coretemp', 'k10temp', 'zenpower', 'amd_energy', 'macsmc_hwmon']:
                    if name == "macsmc_hwmon":
                        best_path = None
                        best_temp = 0
                        for f in os.listdir(hwmon_path):
                            if f.startswith("temp") and f.endswith("_input"):
                                temp_path = os.path.join(hwmon_path, f)
                                try:
                                    temp = int(self.read_text(temp_path, "0") or "0")
                                except Exception:
                                    temp = 0
                                if temp > best_temp:
                                    best_temp = temp
                                    best_path = temp_path
                        if best_path:
                            return best_path, None
                    for f in os.listdir(hwmon_path):
                        if f.endswith('_label') and f.startswith('temp'):
                            try:
                                with open(os.path.join(hwmon_path, f), 'r') as lbl:
                                    label = lbl.read().strip()
                                if label in {
                                    "Package id 0", "Tctl", "Tdie", "Tccd1", "Tccd2",
                                    "WiFi/BT Module Temp", "NAND Flash Temperature",
                                    "Composite", "Battery Hotspot",
                                }:
                                    path = os.path.join(hwmon_path, f.replace('_label', '_input'))
                                    return path, (hwmon_path if name == "coretemp" and label == "Package id 0" else None)
                            except Exception: pass
                    fallback = os.path.join(hwmon_path, "temp1_input")
                    if os.path.exists(fallback):
                        return fallback, None
        return None, None

    def read_coretemp_corrected(self, package_raw):
        """Reject a package reading inflated by a single outlier core.

        Compares against the median of per-core "Core N" sensors; if the
        package value exceeds that median by more than 15C, the median is
        used instead (mirrors how motherboard PECI/BIOS readings behave).
        """
        core_raws = []
        try:
            for f in os.listdir(self.cpu_temp_coretemp_dir):
                if f.startswith("temp") and f.endswith("_label"):
                    label = self.read_text(os.path.join(self.cpu_temp_coretemp_dir, f), "")
                    if label.startswith("Core "):
                        try:
                            core_raws.append(int(self.read_text(
                                os.path.join(self.cpu_temp_coretemp_dir, f.replace("_label", "_input")), "0") or "0"))
                        except (ValueError, OSError):
                            pass
        except OSError:
            return package_raw
        if len(core_raws) < 2:
            return package_raw
        core_raws.sort()
        mid = len(core_raws) // 2
        median_raw = core_raws[mid] if len(core_raws) % 2 else (core_raws[mid - 1] + core_raws[mid]) // 2
        if package_raw - median_raw > 15000:
            return median_raw
        return package_raw


    # ── Battery helpers ──────────────────────────────────────────────

    def find_battery_supply(self):
        """Discover the battery power supply sysfs directory."""
        if self._battery_supply is not None:
            return self._battery_supply
        psu_dir = "/sys/class/power_supply"
        if not os.path.exists(psu_dir):
            self._battery_supply = ""
            return None
        for name in os.listdir(psu_dir):
            type_path = os.path.join(psu_dir, name, "type")
            try:
                with open(type_path) as f:
                    if f.read().strip() == "Battery":
                        self._battery_supply = os.path.join(psu_dir, name)
                        return self._battery_supply
            except Exception:
                continue
        self._battery_supply = ""
        return None

    def read_battery_pct(self):
        """Return battery capacity percentage (0-100), or None."""
        supply = self.find_battery_supply()
        if not supply:
            return None
        cap_path = os.path.join(supply, "capacity")
        try:
            return int(self.read_text(cap_path, "0") or "0")
        except (ValueError, OSError):
            return None

    def read_ac_online(self):
        """Return 1 if AC is online, 0 if on battery, None if unknown."""
        psu_dir = "/sys/class/power_supply"
        if not os.path.exists(psu_dir):
            return None
        for name in os.listdir(psu_dir):
            type_path = os.path.join(psu_dir, name, "type")
            try:
                with open(type_path) as f:
                    if f.read().strip() == "Mains":
                        online_path = os.path.join(psu_dir, name, "online")
                        return int(self.read_text(online_path, "0") or "0")
            except Exception:
                continue
        return None

    def read_battery_status_text(self):
        """Return 'Charging', 'Discharging', 'Full', or 'Unknown'."""
        supply = self.find_battery_supply()
        if not supply:
            return "Unknown"
        return self.read_text(os.path.join(supply, "status"), "Unknown")

    def read_battery_power_uw(self):
        """Return absolute charging power in µW, or None if unavailable."""
        supply = self.find_battery_supply()
        if not supply:
            return None
        try:
            val = int(self.read_text(os.path.join(supply, "power_now"), "") or "0")
            return abs(val)
        except (ValueError, OSError):
            return None

    def read_config(self):
        if not os.path.exists(CONF_FILE): return
        try:
            with open(CONF_FILE, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'): continue
                    if '=' in line:
                        k, v = [x.strip() for x in line.split('=', 1)]
                        if k == "AUTO_MODE": self.mode = self._config_choice(k, v, self.mode, AUTO_MODES)
                        elif k == "ALLOW_CRITICAL_AUTO": self.allow_critical = self._config_choice(k, v, self.allow_critical, YES_NO)
                        elif k == "TEMP_CRITICAL": self.temp_critical = self._config_int(k, v, self.temp_critical, 50, 110)
                        elif k == "TEMP_HOT": self.temp_hot = self._config_int(k, v, self.temp_hot, 40, 100)
                        elif k == "BOOST_TEMP_LIMIT": self.boost_temp_limit = self._config_int(k, v, self.boost_temp_limit, 40, 100)
                        elif k == "LOAD_HIGH": self.load_high = self._config_int(k, v, self.load_high, 1, 100)
                        elif k == "LOAD_HIGH_DURATION": self.load_high_duration = self._config_int(k, v, self.load_high_duration, 5, 86400)
                        elif k == "LOAD_IDLE": self.load_idle = self._config_int(k, v, self.load_idle, 0, 100)
                        elif k == "LOAD_IDLE_DURATION": self.load_idle_duration = self._config_int(k, v, self.load_idle_duration, 5, 86400)
                        elif k == "PROMPT_COOLDOWN": self.prompt_cooldown = self._config_int(k, v, self.prompt_cooldown, 0, 86400)
                        elif k == "POLL_INTERVAL": self.poll_interval = self._config_int(k, v, self.poll_interval, 1, 3600)
                        elif k == "STATS_INTERVAL": self.stats_interval = self._config_int(k, v, self.stats_interval, 10, 86400)
                        elif k == "QUIET_HOURS_START": self.quiet_start = self._config_hhmm(k, v, self.quiet_start)
                        elif k == "QUIET_HOURS_END": self.quiet_end = self._config_hhmm(k, v, self.quiet_end)
                        elif k == "SUMMER_SILENT_NIGHTS": self.summer_nights = self._config_choice(k, v, self.summer_nights, YES_NO)
                        elif k == "AC_PROFILE": self.ac_profile = self._config_choice(k, v, self.ac_profile, PROFILE_COMMANDS)
                        elif k == "BATTERY_PROFILE": self.battery_profile = self._config_choice(k, v, self.battery_profile, PROFILE_COMMANDS)
                        elif k == "BATTERY_LOW_PCT": self.battery_low_pct = self._config_int(k, v, self.battery_low_pct, 1, 100)
                        elif k == "BATTERY_CRITICAL_PCT": self.battery_critical_pct = self._config_int(k, v, self.battery_critical_pct, 1, 100)
                        elif k == "BATTERY_LOW_NOTIFY": self.battery_low_notify = self._config_choice(k, v, self.battery_low_notify, YES_NO)
                        elif k == "SLOW_CHARGE_THRESHOLD_W": self.slow_charge_threshold_uw = self._config_watts_uw(k, v, self.slow_charge_threshold_uw)
                        elif k == "SLOW_CHARGE_BATTERY_PCT": self.slow_charge_battery_pct = self._config_int(k, v, self.slow_charge_battery_pct, 1, 100)
                        elif k == "SLOW_CHARGE_RECOVERY_PCT": self.slow_charge_recovery_pct = self._config_int(k, v, self.slow_charge_recovery_pct, 1, 100)
                        elif k == "SCREEN_LOCK_POWERSAVE": self.screen_lock_powersave = self._config_choice(k, v, self.screen_lock_powersave, YES_NO)
                        elif k == "BATTERY_CHARGE_LIMIT": self._battery_charge_limit = self._config_int(k, v, self._battery_charge_limit, 0, 100)
        except Exception: pass

    def apply_preset(self):
        preset = load_presets().get(self.mode)
        if not isinstance(preset, dict):
            return
        self.temp_hot = preset.get("tempHot", self.temp_hot)
        self.boost_temp_limit = preset.get("boostTempLimit", self.boost_temp_limit)
        self.load_high = preset.get("loadHigh", self.load_high)
        self.load_high_duration = preset.get("loadHighDuration", self.load_high_duration)
        self.load_idle = preset.get("loadIdle", self.load_idle)
        self.load_idle_duration = preset.get("loadIdleDuration", self.load_idle_duration)
        self.prompt_cooldown = preset.get("promptCooldown", self.prompt_cooldown)

    def read_turbo_state(self):
        if os.path.exists('/sys/devices/system/cpu/intel_pstate/no_turbo'):
            return "ON" if self.read_text('/sys/devices/system/cpu/intel_pstate/no_turbo', '1') == '0' else "OFF"
        if os.path.exists('/sys/devices/system/cpu/cpufreq/boost'):
            return "ON" if self.read_text('/sys/devices/system/cpu/cpufreq/boost', '0') == '1' else "OFF"
        if os.path.exists('/sys/devices/system/cpu/amd_pstate/boost'):
            return "ON" if self.read_text('/sys/devices/system/cpu/amd_pstate/boost', '0') == '1' else "OFF"
        if os.path.exists('/sys/devices/system/cpu/cpufreq/policy0/boost'):
            return "ON" if self.read_text('/sys/devices/system/cpu/cpufreq/policy0/boost', '0') == '1' else "OFF"
        return "unsupported"

    def read_cpu_temp(self):
        if not self.cpu_temp_path: return 0
        try:
            with open(self.cpu_temp_path, 'r') as f:
                raw = int(f.read().strip())
            if self.cpu_temp_coretemp_dir:
                raw = self.read_coretemp_corrected(raw)
            return raw // 1000
        except Exception:
            return 0

    def read_cpu_load(self):
        try:
            with open('/proc/stat', 'r') as f:
                line = f.readline().strip()
            parts = [int(x) for x in line.split()[1:]]
            idle = parts[3] + parts[4]
            total = sum(parts)
            delta_total = total - self.prev_total
            delta_idle = idle - self.prev_idle
            self.prev_total = total
            self.prev_idle = idle
            if delta_total <= 0: return 0
            return int((delta_total - delta_idle) * 100 / delta_total)
        except Exception:
            return 0

    def read_process_set(self):
        """Return set of running process names, cached for one poll cycle."""
        now = int(time.time())
        if now - self._process_cache[0] < self.poll_interval:
            return self._process_cache[1]
        procs = set()
        try:
            for pid in os.listdir('/proc'):
                if not pid.isdigit():
                    continue
                try:
                    with open(f'/proc/{pid}/comm') as f:
                        procs.add(f.read().strip())
                except (OSError, IOError):
                    pass
        except Exception:
            pass
        self._process_cache = (now, procs)
        return procs

    def is_game_running(self):
        procs = self.read_process_set()
        return bool(procs.intersection(GAME_PROCESSES))

    def get_ppd_profile(self):
        self._profile_cycle_count += 1
        if self._cached_profile is None or self._profile_cycle_count >= 3:
            self._profile_cycle_count = 0
            try:
                self._cached_profile = subprocess.check_output(['powerprofilesctl', 'get'], text=True).strip()
            except Exception:
                try:
                    tuned = subprocess.check_output(['tuned-adm', 'active'], text=True).strip()
                    tuned = tuned.replace("Current active profile: ", "")
                    self._cached_profile = {
                        "throughput-performance": "performance",
                        "latency-performance": "performance",
                        "accelerator-performance": "performance",
                        "powersave": "power-saver",
                        "balanced-battery": "power-saver",
                    }.get(tuned, "balanced")
                except Exception:
                    self._cached_profile = "balanced"
        return self._cached_profile

    def read_text(self, path, default=""):
        try:
            with open(path, 'r') as f: return f.read().strip()
        except: return default

    def get_gpu_stats(self):
        """Return [temp, power_draw_w, power_limit_w, util_pct] as strings.

        Cached for poll_interval so the per-tick heavy-workload check (see
        GPU_HEAVY_UTIL_PCT) and the stats-interval CSV write share one
        nvidia-smi/sysfs read instead of spawning it twice.
        """
        now = time.time()
        cache_age, cached = self._gpu_stats_cache
        if now - cache_age < max(self.poll_interval, 1):
            return cached
        stats = self._read_gpu_stats()
        self._gpu_stats_cache = (now, stats)
        return stats

    def _read_gpu_stats(self):
        # NVIDIA first
        try:
            out = subprocess.check_output(
                ['nvidia-smi', '--query-gpu=temperature.gpu,power.draw,power.limit,utilization.gpu',
                 '--format=csv,noheader,nounits'], text=True).strip()
            if out:
                parts = [x.strip() for x in out.split('\n')[0].split(',')]
                if len(parts) == 4:
                    return parts
        except Exception:
            pass
        # AMD GPU via amdgpu sysfs (µW → W)
        if self.amd_gpu_hwmon:
            try:
                temp = int(self.read_text(self.amd_gpu_hwmon + "temp1_input", "0") or "0") // 1000
                power_uw = int(self.read_text(self.amd_gpu_hwmon + "power1_average", "0") or "0")
                cap_uw = int(self.read_text(self.amd_gpu_hwmon + "power1_cap", "0") or "0")
                busy_path = os.path.join(self.amd_gpu_hwmon.split("/hwmon/")[0], "gpu_busy_percent")
                util = self.read_text(busy_path, "0") or "0"
                return [str(temp), f"{power_uw / 1_000_000:.2f}", f"{cap_uw / 1_000_000:.2f}", util]
            except Exception:
                pass
        return ["0", "0", "0", "0"]

    def is_creator_running(self):
        return bool(self.read_process_set().intersection(CREATOR_PROCESSES))

    def is_meeting_running(self):
        return bool(self.read_process_set().intersection(MEETING_PROCESSES))

    def is_screen_locked(self):
        """Check if GNOME screen is locked via loginctl."""
        try:
            if not self.cached_session:
                self.get_user_env()
            if not self.cached_session:
                return False
            locked = subprocess.check_output(
                ['loginctl', 'show-session', self.cached_session,
                 '--property=LockedHint', '--value'],
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            return locked == "yes"
        except Exception:
            return False

    def apply_charge_limit(self):
        """Write charge_control_end_threshold if BATTERY_CHARGE_LIMIT is set."""
        if self._battery_charge_limit <= 0:
            return
        supply = self.find_battery_supply()
        if not supply:
            return
        end_path = os.path.join(supply, "charge_control_end_threshold")
        start_path = os.path.join(supply, "charge_control_start_threshold")
        if not os.path.exists(end_path):
            return
        try:
            current = int(self.read_text(end_path, "100"))
            if current != self._battery_charge_limit:
                with open(end_path, 'w') as f:
                    f.write(str(self._battery_charge_limit))
                start_val = max(self._battery_charge_limit - 5, 0)
                if os.path.exists(start_path):
                    with open(start_path, 'w') as f:
                        f.write(str(start_val))
                self.log(f"Battery charge limit set to {self._battery_charge_limit}%")
        except Exception as e:
            self.log(f"Failed to set charge limit: {e}")

    def read_rapl_limits(self):
        """Return (pl1_watts_str, pl2_watts_str); ("0", "0") when RAPL is unavailable (AMD CPUs)."""
        rapl_base = '/sys/class/powercap/intel-rapl/intel-rapl:0'
        if not os.path.isdir(rapl_base):
            return '0', '0'
        pl1 = str(int(self.read_text(f'{rapl_base}/constraint_0_power_limit_uw', '0')) // 1000000)
        pl2 = str(int(self.read_text(f'{rapl_base}/constraint_1_power_limit_uw', '0')) // 1000000)
        return pl1, pl2

    def maybe_roll_up_daily_stats(self):
        """Append yesterday's rollup to daily.csv the first time record_stats
        runs after the local date changes.

        stats.csv rotates at 250KB / ~2000 rows (~1.5 days at the default
        60s STATS_INTERVAL), too little history for week/month thermal or
        battery trends. This one-row-per-day file grows far slower and is
        written independently of that hot-path rotation.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if today == self._last_rollup_date:
            return
        prior_date = self._last_rollup_date
        self._last_rollup_date = today
        try:
            if not os.path.exists(STATS_FILE):
                return
            temps, loads, profile_counts, battery_deltas = [], [], {}, []
            prev_battery = None
            with open(STATS_FILE, newline='', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    if not row.get("iso", "").startswith(prior_date):
                        continue
                    try:
                        temps.append(int(row["cpu_temp"]))
                    except (KeyError, ValueError):
                        pass
                    try:
                        loads.append(int(row["cpu_load"]))
                    except (KeyError, ValueError):
                        pass
                    prof = row.get("profile", "")
                    if prof:
                        profile_counts[prof] = profile_counts.get(prof, 0) + 1
                    bp = row.get("battery_pct", "")
                    if bp.lstrip('-').isdigit():
                        bp = int(bp)
                        if prev_battery is not None:
                            battery_deltas.append(abs(bp - prev_battery))
                        prev_battery = bp
            if not temps and not loads and not profile_counts:
                return  # daemon wasn't running that day; nothing to roll up

            total_rows = sum(profile_counts.values()) or 1
            share = ";".join(
                f"{name}:{count * 100 // total_rows}%"
                for name, count in sorted(profile_counts.items())
            )
            # Rough charge-cycle proxy: total %-points of battery movement / 200
            # (100% discharge + 100% charge == one full cycle).
            cycles_proxy = round(sum(battery_deltas) / 200, 2) if battery_deltas else 0

            is_new = not os.path.exists(DAILY_FILE)
            with open(DAILY_FILE, 'a') as f:
                if is_new:
                    f.write("date,min_temp,avg_temp,max_temp,avg_load,profile_share,battery_cycles_proxy\n")
                min_t = min(temps) if temps else 0
                max_t = max(temps) if temps else 0
                avg_t = round(sum(temps) / len(temps), 1) if temps else 0
                avg_l = round(sum(loads) / len(loads), 1) if loads else 0
                f.write(f"{prior_date},{min_t},{avg_t},{max_t},{avg_l},{share},{cycles_proxy}\n")
        except Exception as e:
            self.log(f"Error writing daily rollup: {e}")

    def record_stats(self, load, temp, profile, gpu_stats=None):
        try:
            if not getattr(self, '_state_dir_created', False):
                os.makedirs(STATE_DIR, exist_ok=True)
                self._state_dir_created = True

            self.maybe_roll_up_daily_stats()
            gpu_temp, gpu_power, gpu_limit, _gpu_util = gpu_stats if gpu_stats is not None else self.get_gpu_stats()
            pl1, pl2 = self.read_rapl_limits()
            gov = self.read_text('/sys/devices/system/cpu/cpufreq/policy0/scaling_governor', '') or self.read_text('/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor', 'unknown')
            epp = self.read_text('/sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference', '') or self.read_text('/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference', 'unsupported')
            turbo = self.read_turbo_state()
            
            pct = self.read_battery_pct()
            battery_pct = "" if pct is None else str(pct)
            battery_status = self.read_battery_status_text()
            iso_time = datetime.now().astimezone().replace(microsecond=0).isoformat()
            row = f"{int(time.time())},{iso_time},{profile},{load},{temp},{gpu_temp},{gpu_power},{gpu_limit},{pl1},{pl2},{gov},{epp},{turbo},{battery_pct},{battery_status}\n"
            
            if not os.path.exists(STATS_FILE):
                with open(STATS_FILE, 'w') as f:
                    f.write("epoch,iso,profile,cpu_load,cpu_temp,gpu_temp,gpu_power,gpu_limit,pl1,pl2,governor,epp,turbo,battery_pct,battery_status\n")
            
            with open(STATS_FILE, 'a') as f:
                f.write(row)
                
            # Prevent infinite growth: if file > 250KB, keep header + last 2000 lines (~1.5 days at 1 min interval)
            if os.path.getsize(STATS_FILE) > 250 * 1024:
                with open(STATS_FILE, 'r') as f:
                    lines = f.readlines()
                if len(lines) > 2000:
                    with open(f"{STATS_FILE}.tmp", 'w') as f:
                        f.write(lines[0])
                        f.writelines(lines[-2000:])
                    os.rename(f"{STATS_FILE}.tmp", STATS_FILE)
        except Exception as e:
            self.log(f"Error recording stats: {e}")

    def write_live_snapshot(self, temp, load, profile, gpu_stats, pl1, pl2, ac_online, battery_pct):
        """Write the single-poll-authority state snapshot other processes read.

        boost-web.py and boost-tray.py read this file (when fresh) instead of
        independently re-polling sysfs/hwmon/nvidia-smi, so temperature reads
        (including the coretemp outlier correction) and GPU/RAPL subprocess
        spawns happen once per tick, not once per poller.
        """
        try:
            if not getattr(self, '_state_dir_created', False):
                os.makedirs(STATE_DIR, exist_ok=True)
                self._state_dir_created = True
            gpu_temp, gpu_power, gpu_limit, gpu_util = gpu_stats
            snapshot = {
                "time": int(time.time()),
                "cpu": {"temp": temp, "load": load},
                "gpu": {"temp": gpu_temp, "power": gpu_power, "limit": gpu_limit, "util": gpu_util},
                "profile": profile,
                "mode": self.mode,
                "limits": {"pl1": pl1, "pl2": pl2},
                "battery": {"acOnline": ac_online, "pct": battery_pct},
                "lastSwitch": {
                    "reason": self.last_switch_reason,
                    "text": self.last_switch_reason_text,
                    "time": self.last_switch_time,
                },
            }
            tmp = f"{LIVE_FILE}.tmp"
            with open(tmp, 'w') as f:
                json.dump(snapshot, f)
            os.rename(tmp, LIVE_FILE)
        except Exception as e:
            self.log(f"Error writing live snapshot: {e}")

    def note_switch(self, code, text):
        """Record why the daemon just silently changed something.

        Surfaced via live.json/status_payload's auto.decision so "why did
        my profile just change" is answerable for the AC/battery/slow-charge/
        screen-lock/critical-heat paths, not just the thermal/load heuristic
        decision_reason() already covers.
        """
        self.last_switch_reason = code
        self.last_switch_reason_text = text
        self.last_switch_time = int(time.time())

    def run_command(self, cmd):
        try:
            proc = subprocess.Popen(shlex.split(cmd), env=dict(os.environ, AUTO_HELPER_INTERNAL="1"))
        except OSError as e:
            self.log(f"Failed to run {cmd}: {e}", LOG_WARNING)
            return
        # Reap the child in the background so it never lingers as a zombie
        threading.Thread(target=proc.wait, daemon=True).start()
        self._cached_profile = None

    def get_user_env(self):
        try:
            out = subprocess.check_output(['loginctl', 'list-sessions', '--no-legend'], text=True)
            session = None
            first_session = None
            for line in out.strip().split('\n'):
                if not line: continue
                parts = line.split()
                if not first_session and parts:
                    first_session = parts[0]
                if 'active' in parts:
                    session = parts[0]
                    break
            if not session:
                session = first_session
            if not session: return None
            
            if self.cached_session == session and self.cached_env is not None:
                return self.cached_env.copy()
            
            user = subprocess.check_output(['loginctl', 'show-session', session, '-p', 'Name', '--value'], text=True).strip()
            uid = subprocess.check_output(['id', '-u', user], text=True).strip()
            
            x11 = subprocess.check_output(['loginctl', 'show-session', session, '-p', 'Display', '--value'], text=True).strip()
            if not x11: x11 = ":0"
            
            wayland = ""
            run_dir = f"/run/user/{uid}"
            if os.path.exists(run_dir):
                for f in os.listdir(run_dir):
                    if f.startswith('wayland-') and not f.endswith('.lock'):
                        wayland = f
                        break
            
            env = {
                'XDG_RUNTIME_DIR': run_dir,
                'DBUS_SESSION_BUS_ADDRESS': f"unix:path={run_dir}/bus",
                'USER': user,
                'UID': uid
            }
            if wayland:
                env['WAYLAND_DISPLAY'] = wayland
                env['GDK_BACKEND'] = 'wayland'
            else:
                env['DISPLAY'] = x11
                env['GDK_BACKEND'] = 'x11'
                
            self.cached_session = session
            self.cached_env = env.copy()
            return env
        except Exception as e:
            self.log(f"Error getting user env: {e}")
            return None

    def send_notification(self, title, body, action_label=None, action_cmd=None, level="normal"):
        env_vars = self.get_user_env()
        if not env_vars: return
            
        user = env_vars.pop('USER')
        env_vars.pop('UID', None)  # don't pass UID as env var to notify-send
        
        cmd_env = dict(os.environ)
        cmd_env.update(env_vars)
        
        if action_label:
            env_args = [f"{k}={v}" for k, v in env_vars.items()]
            cmd = ['sudo', '-u', user, 'env'] + env_args + [
                'notify-send', '--wait', '-u', level, '-a', 'Auto power helper',
                '--expire-time=45000', f'--action=switch={action_label}',
                '--action=snooze=Later', '--action=today=Not today', title, body]
            
            def handle_notify():
                try:
                    action = subprocess.check_output(cmd, env=cmd_env, text=True).strip()
                    if action == "switch":
                        self.log(f"Action accepted: {action_cmd}")
                        self.run_command(action_cmd)
                    elif action == "snooze":
                        until = int(time.time()) + 7200
                        tmp = f"{SNOOZE_FILE}.tmp"
                        with open(tmp, 'w') as f:
                            f.write(str(until))
                        os.rename(tmp, SNOOZE_FILE)
                        self._snooze_cache = (time.time(), until, False)
                    elif action == "today":
                        tmp = f"{SKIP_TODAY_FILE}.tmp"
                        with open(tmp, 'w') as f:
                            f.write(datetime.now().strftime("%Y-%m-%d"))
                        os.rename(tmp, SKIP_TODAY_FILE)
                        self._snooze_cache = (time.time(), 0, True)
                except Exception:
                    pass
            threading.Thread(target=handle_notify, daemon=True).start()
        else:
            env_args = [f"{k}={v}" for k, v in env_vars.items()]
            cmd = ['sudo', '-u', user, 'env'] + env_args + [
                'notify-send', '-u', level, '-a', 'Auto power helper', title, body]
            try:
                proc = subprocess.Popen(cmd, env=cmd_env)
                threading.Thread(target=proc.wait, daemon=True).start()
            except OSError:
                pass
        
        self.log(f"NOTIFY: {title}")

    def in_quiet_hours(self):
        if self.quiet_start == self.quiet_end: return False
        try:
            now = datetime.now()
            now_m = now.hour * 60 + now.minute
            h, m = map(int, self.quiet_start.split(':'))
            start_m = h * 60 + m
            h, m = map(int, self.quiet_end.split(':'))
            end_m = h * 60 + m
            if start_m < end_m:
                return start_m <= now_m < end_m
            else:
                return now_m >= start_m or now_m < end_m
        except Exception:
            return False

    def suggestions_paused(self):
        if self.mode in ("quiet", "off"): return True
        if self.in_quiet_hours(): return True
        
        now = time.time()
        if now - self._snooze_cache[0] < 30:
            ttl, until_epoch, skipped = self._snooze_cache
            if skipped or until_epoch > now: return True
            return False

        skipped = False
        until_epoch = 0
        try:
            if os.path.exists(SKIP_TODAY_FILE):
                with open(SKIP_TODAY_FILE, 'r') as f:
                    if f.read().strip() == datetime.now().strftime("%Y-%m-%d"):
                        skipped = True
            if os.path.exists(SNOOZE_FILE):
                with open(SNOOZE_FILE, 'r') as f:
                    until_epoch = int(f.read().strip())
                if until_epoch <= now:
                    os.remove(SNOOZE_FILE)
        except Exception:
            pass
            
        self._snooze_cache = (now, until_epoch, skipped)
        return skipped or until_epoch > now

    def loop(self):
        self.log("Boost Daemon started in Python High-Performance mode")
        while True:
            time.sleep(self.poll_interval)
            try:
                self._tick()
            except Exception as e:
                # Never let a single bad poll cycle kill the daemon
                self.log(f"Poll cycle error (recovered): {e!r}", LOG_WARNING)

    def _tick(self):
        try:
            current_mtime = os.stat(CONF_FILE).st_mtime if os.path.exists(CONF_FILE) else 0
        except Exception:
            current_mtime = 0

        if current_mtime != self.last_conf_mtime or self.last_conf_mtime == -1:
            self.read_config()    # get new mode and all values
            self.apply_preset()   # apply mode defaults
            self.read_config()    # re-apply explicit config overrides on top
            self.last_conf_mtime = current_mtime

        now = int(time.time())
        temp = self.read_cpu_temp()
        load = self.read_cpu_load()
        profile = self.get_ppd_profile()
        is_game = self.is_game_running()
        is_creator = self.is_creator_running()
        is_meeting = self.is_meeting_running()

        # GPU-bound heavy workload: catches unlisted GPU apps (Stable
        # Diffusion/ComfyUI, Lutris/Heroic games, niche engines) that
        # is_creator_running()'s static process allowlist misses.
        gpu_stats = self.get_gpu_stats()
        try:
            gpu_util = int(float(gpu_stats[3]))
        except (ValueError, IndexError):
            gpu_util = 0
        if gpu_util >= GPU_HEAVY_UTIL_PCT:
            if self.gpu_high_since == 0:
                self.gpu_high_since = now
        else:
            self.gpu_high_since = 0
        is_gpu_heavy = self.gpu_high_since != 0 and now - self.gpu_high_since >= GPU_HEAVY_DURATION_S

        if now - self.last_stats >= self.stats_interval:
            self.record_stats(load, temp, profile, gpu_stats=gpu_stats)
            self.last_stats = now

        # ── Battery & AC monitoring ──────────────────────────────
        ac_online = self.read_ac_online()
        battery_pct = self.read_battery_pct()

        pl1, pl2 = self.read_rapl_limits()
        self.write_live_snapshot(temp, load, profile, gpu_stats, pl1, pl2, ac_online, battery_pct)

        # AC plug/unplug event: switch profiles
        if ac_online is not None and ac_online != self._last_ac_online:
            first_observation = self._last_ac_online is None
            self._last_ac_online = ac_online
            if first_observation:
                # Daemon (re)start: record state only. ac-event/udev already
                # applied the right profile at boot; re-applying here would
                # force a profile switch and notification on every restart.
                pass
            elif ac_online == 1:
                self.log(f"AC connected. Applying profile: {self.ac_profile}")
                self.run_command(f"/usr/local/bin/{self.ac_profile}")
                self.note_switch("ac-connected", f"AC plugged in. Applied the {self.ac_profile} profile.")
                self.send_notification("AC Power Connected",
                    f"Switched to {self.ac_profile} profile.")
            elif ac_online == 0:
                self.log(f"On battery. Applying profile: {self.battery_profile}")
                self.run_command(f"/usr/local/bin/{self.battery_profile}")
                self.note_switch("on-battery", f"Unplugged from AC. Applied the {self.battery_profile} profile.")
                if battery_pct is not None and battery_pct <= self.battery_critical_pct:
                    self.send_notification("Battery Critical",
                        f"Only {battery_pct}% remaining. Maximum power saving.", level="critical")
                elif battery_pct is not None and battery_pct <= self.battery_low_pct:
                    self.send_notification("Battery Low",
                        f"{battery_pct}% remaining. Switched to {self.battery_profile} profile.")
                else:
                    self.send_notification("On Battery",
                        f"Switched to {self.battery_profile} profile.")
            self._battery_notified_low = False
            self._battery_notified_critical = False

        # Track battery drain while on battery
        if ac_online == 0 and battery_pct is not None:
            self._last_battery_pct = battery_pct

            # Critical battery: auto-switch to powersave/silent
            if battery_pct <= self.battery_critical_pct and not self._battery_notified_critical:
                self._battery_notified_critical = True
                self._battery_notified_low = True  # don't also fire low notification
                if profile != "power-saver":
                    self.log(f"Battery critical ({battery_pct}%). Auto powersave.")
                    self.run_command("/usr/local/bin/powersave")
                    self.note_switch("battery-critical", f"Battery critical at {battery_pct}%. Forced powersave.")
                if self.battery_low_notify == "yes":
                    self.send_notification("Battery Critical",
                        f"Only {battery_pct}% remaining. System is in maximum power saving mode.",
                        level="critical")

            # Low battery: notify once
            elif battery_pct <= self.battery_low_pct and not self._battery_notified_low:
                self._battery_notified_low = True
                if self.battery_low_notify == "yes":
                    self.send_notification("Battery Low",
                        f"{battery_pct}% remaining. Consider plugging in your charger.")

            # Reset notifications if battery goes back above thresholds (e.g. plugged in briefly)
            if battery_pct > self.battery_low_pct:
                self._battery_notified_low = False
            if battery_pct > self.battery_critical_pct:
                self._battery_notified_critical = False
        elif ac_online == 1:
            # Reset battery notifications when plugged in
            self._battery_notified_low = False
            self._battery_notified_critical = False

        # ── Slow charge protection ───────────────────────────────
        if ac_online == 1 and battery_pct is not None:
            power_uw = self.read_battery_power_uw()
            if power_uw is not None:
                self._power_samples.append(power_uw)
                if len(self._power_samples) > 12:
                    self._power_samples.pop(0)
                avg_uw = sum(self._power_samples) / len(self._power_samples)
                batt_status = self.read_battery_status_text()

                if not self._slow_charge_active:
                    if (batt_status == "Charging"
                            and battery_pct < self.slow_charge_battery_pct
                            and len(self._power_samples) >= 6
                            and avg_uw < self.slow_charge_threshold_uw
                            and profile != "power-saver"):
                        self._slow_charge_active = True
                        self._slow_charge_notified = True
                        avg_w = avg_uw / 1_000_000
                        self.log(f"Slow charge detected ({avg_w:.1f}W avg). Switching to powersave.")
                        self.run_command("/usr/local/bin/powersave")
                        self.note_switch("slow-charge", f"Charger barely keeping up ({avg_w:.1f}W net). Switched to powersave.")
                        self.send_notification(
                            "Slow Charging Detected",
                            f"Charger barely keeping up ({avg_w:.1f}W net to battery). "
                            f"Switched to Eco Mode to speed up charging.",
                        )
                else:
                    if (battery_pct >= self.slow_charge_recovery_pct
                            or avg_uw >= self.slow_charge_threshold_uw * 2):
                        self._slow_charge_active = False
                        self._power_samples.clear()
                        restore = self.ac_profile if self.ac_profile in ("boost", "powersave", "silent") else "boost"
                        self.log(f"Slow charge resolved. Battery at {battery_pct}%. Restoring {restore}.")
                        self.run_command(f"/usr/local/bin/{restore}")
                        self.note_switch("slow-charge-recovered", f"Charging recovered at {battery_pct}%. Restored {restore}.")
                        self.send_notification(
                            "Charging Recovered",
                            f"Battery at {battery_pct}%. Switched back to {restore.capitalize()} profile.",
                        )
        else:
            if self._slow_charge_active:
                self._slow_charge_active = False
            self._power_samples.clear()

        # ── Screen lock → silent powersave ──────────────────────
        if self.screen_lock_powersave == "yes" and self.mode != "off":
            locked = self.is_screen_locked()
            if locked and not self._screen_locked:
                self._screen_locked = True
                self._pre_lock_profile = profile
                if profile != "power-saver":
                    self.log("Screen locked. Silently switching to powersave.")
                    self.run_command("/usr/local/bin/powersave")
                    self.note_switch("screen-lock", "Screen locked. Switched to powersave.")
            elif not locked and self._screen_locked:
                self._screen_locked = False
                if self._pre_lock_profile == "performance":
                    self.log("Screen unlocked. Restoring performance profile.")
                    self.run_command("/usr/local/bin/boost")
                    self.note_switch("screen-unlock", "Screen unlocked. Restored the performance profile.")
                self._pre_lock_profile = None

        # ── Charge limit enforcement ─────────────────────────────
        self.apply_charge_limit()

        if self.mode == "off":
            return

        # Summer Night Mode Auto-Silent
        if self.summer_nights == "yes" and self.in_quiet_hours() and profile != "power-saver":
            if now - self.last_auto > 3600:
                self.log("Summer quiet hours: auto silent")
                self.run_command("/usr/local/bin/silent --auto")
                self.note_switch("summer-quiet-hours", "Summer quiet hours are active. Switched to Silent.")
                self.send_notification("Summer night mode", "Quiet hours are active, so Auto applied Silent mode.")
                self.last_auto = now
                self.last_prompt = now
                return
            
        # Game Mode Auto-Switching (skip if slow charge protection is active)
        if is_game and not self._slow_charge_active and profile != "performance" and temp < self.boost_temp_limit:
            if now - self.last_auto > 60:
                self.log("Game detected. Switching to boost automatically.")
                self.run_command("/usr/local/bin/boost")
                self.note_switch("game-detected", "Game detected. Switched to maximum performance.")
                self.send_notification("Game Mode Enabled", "Detected a game running. Switched to maximum performance.")
                self.last_auto = now
                return

        # Creator Workload Detection — suggest Performance for heavy rendering/compilation.
        # Triggers on the known-process allowlist OR sustained GPU utilization, so
        # unlisted GPU-bound apps (Stable Diffusion/ComfyUI, Lutris/Heroic games,
        # niche engines) are caught too.
        if (is_creator or is_gpu_heavy) and not is_game and profile != "performance" and temp < self.boost_temp_limit:
            if now - self.last_prompt > self.prompt_cooldown:
                self.send_notification(
                    "Heavy workload detected",
                    "Rendering or compilation in progress. Enable Boost for faster results.",
                    "Enable Boost", "/usr/local/bin/boost"
                )
                self.last_prompt = now

        # Meeting / Video Call Detection
        # On battery: auto-switch to powersave (quiet fans, save battery)
        # On AC: suggest once per session
        if is_meeting and not self._meeting_notified and profile == "performance":
            self._meeting_notified = True
            if ac_online == 0:
                self.log("Meeting detected on battery. Auto-switching to powersave.")
                self.run_command("/usr/local/bin/powersave")
                self.note_switch("meeting-on-battery", "Video call detected on battery. Switched to powersave.")
                self.send_notification(
                    "Video call — Eco Mode",
                    "Switched to Eco Mode to keep fans quiet and save battery during your call.",
                )
            else:
                self.send_notification(
                    "Video call detected",
                    "Switch to Balanced mode to reduce fan noise during your call.",
                    "Go Quiet", "/usr/local/bin/powersave"
                )
        elif not is_meeting:
            self._meeting_notified = False
            
        # Critical Protection
        if temp >= self.temp_critical and profile == "performance" and self.allow_critical == "yes":
            if now - self.last_auto > 120:
                self.log(f"Critical heat {temp}C. Emergency powersave.")
                self.run_command("/usr/local/bin/powersave")
                self.note_switch("critical-heat", f"CPU hit {temp}C. Forced powersave to protect hardware.")
                self.send_notification("Critical Heat Warning", f"CPU reached {temp}C. Switched to cooler mode to protect hardware.", level="critical")
                self.last_auto = now
                self.last_prompt = now
            
        if self.suggestions_paused():
            return
                
        # Hot Warning
        if temp >= self.temp_hot and profile == "performance":
            if now - self.last_prompt > self.prompt_cooldown:
                self.send_notification("The computer is getting warm", "I can switch to a cooler mode.", "Cool it down", "/usr/local/bin/powersave")
                self.last_prompt = now
            
        # High Load Warning (Non-game)
        elif load >= self.load_high and not is_game:
            if self.high_since == 0: self.high_since = now
            elif now - self.high_since >= self.load_high_duration and profile != "performance" and temp < self.boost_temp_limit:
                if now - self.last_prompt > self.prompt_cooldown:
                    self.send_notification("It looks like you need more power", "I can enable Boost for heavy work.", "Enable Boost", "/usr/local/bin/boost")
                    self.last_prompt = now
                    self.high_since = 0
            self.idle_since = 0
            
        # Idle Warning
        elif load <= self.load_idle and not is_game:
            if self.idle_since == 0: self.idle_since = now
            elif now - self.idle_since >= self.load_idle_duration and profile == "performance":
                if now - self.last_prompt > self.prompt_cooldown:
                    self.send_notification("The PC looks quiet now", "I can leave Boost to reduce heat.", "Cool down", "/usr/local/bin/powersave")
                    self.last_prompt = now
                    self.idle_since = 0
            self.high_since = 0
        else:
            self.high_since = 0
            self.idle_since = 0

if __name__ == "__main__":
    daemon = BoostDaemon()
    daemon.loop()
