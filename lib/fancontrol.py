#!/usr/bin/env python3
"""Fan curve engine for Boost.

Design notes (each one answers a complaint people actually file against the
existing Linux fan tools):

* Fans are addressed by a stable ``chip:pwmN`` id built from the chip *name*,
  never hwmonN — hwmon numbering is probe-order dependent and reshuffles after
  a reboot, which silently repoints a curve at the wrong fan (CoolerControl
  issue class #1).
* Curves are *requests*, not commands. Every tick the engine re-derives a
  failsafe floor from CPU/GPU/NVMe temperature and raises the fan above the
  requested curve when the hardware needs it, so a Silent curve can never cook
  the machine (see guard_floor()).
* One writer per pwm channel: an external tool moving a pwm we own is detected
  by read-back mismatch, logged, and the channel is backed off instead of
  fought over (fan2go behaviour).
* Losing the daemon must not leave fans pinned: failsafe_all() puts every
  channel back to its original pwmN_enable (BIOS/Smart Fan control) and is
  wired to the unit's ExecStopPost.
* Boards that expose pwm read-only (several Dell/server designs) are detected
  on first write and reported as unsupported instead of erroring every tick.

Run standalone for the CLI used by `auto fans ...`:
    fancontrol.py discover|status|init|calibrate|failsafe|test|preset
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sensors  # noqa: E402  (same-directory sibling module)

CONFIG_FILE = "/etc/boost-fans.json"
STATE_DIR = "/var/lib/power-profile"
CALIBRATION_FILE = os.path.join(STATE_DIR, "fans-calibration.json")
ORIGINAL_ENABLE_FILE = os.path.join(STATE_DIR, "fans-original-enable.json")
OVERRIDE_FILE = os.path.join(STATE_DIR, "fan-override.json")
PAUSE_FILE = os.path.join(STATE_DIR, "fan-engine-pause")

# GPU fans are deliberately out of scope: nvidia needs coolbits/X and amdgpu
# has a firmware zero-RPM floor that fighting produces exactly the "fan won't
# spin below X%" bug reports LACT/CoolerControl users hit.
SKIP_CHIPS = ("amdgpu", "nouveau", "nvidia", "i915", "xe")

CONFLICTING_SERVICES = ("fancontrol.service", "coolercontrold.service", "thinkfan.service")

PROFILE_KEYS = ("boost", "balanced", "silent")

# ppd/tuned profile name -> fan profile key
PROFILE_MAP = {
    "performance": "boost",
    "throughput-performance": "boost",
    "latency-performance": "boost",
    "accelerator-performance": "boost",
    "balanced": "balanced",
    "power-saver": "silent",
    "powersave": "silent",
    "balanced-battery": "silent",
}

# Preset shapes: (temperature, fraction between min_pwm and 100%).
# The 85C+/>=80% tail is mandatory (validate_curve enforces it), so even the
# quietest preset ends in a curve that can actually cool the machine.
PRESET_SHAPES = {
    "whisper": [(52, 0.00), (66, 0.10), (77, 0.32), (85, 0.80), (92, 1.00)],
    "hush": [(48, 0.00), (63, 0.15), (74, 0.40), (83, 0.75), (90, 1.00)],
    "silent": [(45, 0.00), (60, 0.20), (72, 0.50), (82, 0.80), (90, 1.00)],
    "balanced": [(40, 0.10), (55, 0.35), (70, 0.65), (80, 0.90), (90, 1.00)],
    "aggressive": [(35, 0.30), (50, 0.55), (65, 0.80), (75, 0.95), (85, 1.00)],
}

DEFAULT_PRESET_FOR_PROFILE = {"boost": "aggressive", "balanced": "balanced", "silent": "silent"}

MIN_PWM_FLOOR = 15          # percent; below this most 4-pin fans stall
DEFAULT_STEP_LIMIT = 12     # max percent change per tick (anti-oscillation)
DEFAULT_HYST_UP = 2         # C the source must rise before speeding up
DEFAULT_HYST_DOWN = 4       # C it must fall before slowing down
DEFAULT_RESPONSE_DELAY = 6  # s a slow-down must be sustained for
CONFLICT_TOLERANCE = 6      # raw pwm units of read-back drift we accept
CONFLICT_BACKOFF_S = 120


def read_text(path: str, default: str = "") -> str:
    return sensors.read_text(path, default)


def read_int(path: str, default: int = 0) -> int:
    return sensors.read_int(path, default)


def pct_to_raw(pct: float) -> int:
    return max(0, min(255, int(round(pct * 255 / 100))))


def raw_to_pct(raw: int) -> int:
    return max(0, min(100, int(round(raw * 100 / 255))))


def _write_json_atomic(path: str, payload) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


# ── discovery ────────────────────────────────────────────────────────


class FanChannel:
    def __init__(self, chip: str, hwmon_path: str, index: int) -> None:
        self.chip = chip
        self.hwmon_path = hwmon_path
        self.index = index
        self.id = f"{chip}:pwm{index}"
        self.pwm_path = os.path.join(hwmon_path, f"pwm{index}")
        self.enable_path = os.path.join(hwmon_path, f"pwm{index}_enable")
        rpm_path = os.path.join(hwmon_path, f"fan{index}_input")
        self.rpm_path = rpm_path if os.path.exists(rpm_path) else None
        self.read_only = False

    def read_pwm(self) -> int:
        return read_int(self.pwm_path, 0)

    def read_rpm(self) -> int | None:
        if not self.rpm_path:
            return None
        return read_int(self.rpm_path, 0)

    def read_enable(self) -> int:
        return read_int(self.enable_path, -1)

    def write_enable(self, value: int) -> bool:
        try:
            with open(self.enable_path, "w", encoding="utf-8") as handle:
                handle.write(str(value))
            return True
        except OSError:
            return False

    def write_pwm(self, raw: int) -> bool:
        try:
            with open(self.pwm_path, "w", encoding="utf-8") as handle:
                handle.write(str(max(0, min(255, raw))))
            return True
        except OSError:
            self.read_only = True
            return False


def discover_channels() -> list[FanChannel]:
    channels: list[FanChannel] = []
    for hwmon_path, chip in sorted(sensors.chip_map().items(), key=lambda item: item[1]):
        if any(chip.lower().startswith(skip) for skip in SKIP_CHIPS):
            continue
        try:
            files = os.listdir(hwmon_path)
        except OSError:
            continue
        for filename in files:
            if not filename.startswith("pwm") or not filename[3:].isdigit():
                continue
            index = int(filename[3:])
            if not os.path.exists(os.path.join(hwmon_path, f"pwm{index}_enable")):
                continue
            channels.append(FanChannel(chip, hwmon_path, index))
    channels.sort(key=lambda channel: (channel.chip, channel.index))
    return channels


def conflicting_service() -> str | None:
    """Return the name of another fan daemon already driving these pwms."""
    import subprocess

    for service in CONFLICTING_SERVICES:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", service],
                capture_output=True, text=True, timeout=3, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.stdout.strip() == "active":
            return service
    return None


# ── curves ───────────────────────────────────────────────────────────


def validate_curve(points) -> tuple[list[list[int]], str | None]:
    """Normalize and safety-check one curve.

    Rules (the "dumb-proof" contract): at least two points, temperatures
    strictly increasing, pwm never decreasing as temperature rises, and a
    tail of at least 80% at 85C or above.
    """
    if not isinstance(points, (list, tuple)) or len(points) < 2:
        return [], "A curve needs at least two points."
    cleaned: list[list[int]] = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return [], "Each curve point must be [temperature, pwm%]."
        try:
            temp = int(round(float(point[0])))
            pwm = int(round(float(point[1])))
        except (TypeError, ValueError):
            return [], "Curve points must be numbers."
        if not 0 <= temp <= 120:
            return [], f"Curve temperature {temp} is outside 0-120 C."
        if not 0 <= pwm <= 100:
            return [], f"Curve fan speed {pwm}% is outside 0-100%."
        cleaned.append([temp, pwm])
    cleaned.sort(key=lambda item: item[0])
    for previous, current in zip(cleaned, cleaned[1:]):
        if current[0] == previous[0]:
            return [], "Two curve points cannot share the same temperature."
        if current[1] < previous[1]:
            return [], "Fan speed cannot go down as temperature goes up."
    last_temp, last_pwm = cleaned[-1]
    if last_temp < 85 or last_pwm < 80:
        return [], "The curve must reach at least 80% fan speed by 85 C."
    return cleaned, None


def curve_pwm(points, temp: float) -> int:
    """Linear interpolation with flat ends."""
    if not points:
        return 100
    if temp <= points[0][0]:
        return int(points[0][1])
    if temp >= points[-1][0]:
        return int(points[-1][1])
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if t0 <= temp <= t1:
            if t1 == t0:
                return int(p1)
            return int(round(p0 + (p1 - p0) * (temp - t0) / (t1 - t0)))
    return int(points[-1][1])


def preset_curve(preset: str, min_pwm: int) -> list[list[int]]:
    shape = PRESET_SHAPES.get(preset, PRESET_SHAPES["balanced"])
    floor = max(0, min(60, int(min_pwm)))
    return [[temp, int(round(floor + (100 - floor) * fraction))] for temp, fraction in shape]


# ── configuration ────────────────────────────────────────────────────


def default_fan_config(channel: FanChannel, calibration: dict | None = None) -> dict:
    entry = (calibration or {}).get(channel.id, {})
    start_pwm = entry.get("start_pwm")
    min_pwm = max(MIN_PWM_FLOOR, int(start_pwm) if isinstance(start_pwm, int) else MIN_PWM_FLOOR)
    stop_allowed = bool(entry.get("stop_allowed", False))
    return {
        "name": entry.get("name") or f"{channel.chip} fan {channel.index}",
        "enabled": True,
        "source": {"mode": "max", "sensors": ["cpu"]},
        "min_pwm": min_pwm,
        "stop_allowed": stop_allowed,
        "hyst_up": DEFAULT_HYST_UP,
        "hyst_down": DEFAULT_HYST_DOWN,
        "response_delay_s": DEFAULT_RESPONSE_DELAY,
        "step_limit": DEFAULT_STEP_LIMIT,
        "preset": {key: DEFAULT_PRESET_FOR_PROFILE[key] for key in PROFILE_KEYS},
        "profiles": {
            key: preset_curve(DEFAULT_PRESET_FOR_PROFILE[key], min_pwm) for key in PROFILE_KEYS
        },
    }


def default_config(channels=None, calibration=None) -> dict:
    channels = channels if channels is not None else discover_channels()
    calibration = calibration if calibration is not None else (_read_json(CALIBRATION_FILE) or {})
    return {
        "version": 1,
        "enabled": False,
        "fans": {channel.id: default_fan_config(channel, calibration) for channel in channels},
    }


def validate_config(config) -> tuple[dict, str | None]:
    if not isinstance(config, dict):
        return {}, "Fan config must be a JSON object."
    fans = config.get("fans")
    if not isinstance(fans, dict):
        return {}, "Fan config needs a 'fans' object."
    known = {channel.id for channel in discover_channels()}
    cleaned_fans: dict[str, dict] = {}
    for fan_id, raw in fans.items():
        if known and fan_id not in known:
            return {}, f"Unknown fan: {fan_id}"
        if not isinstance(raw, dict):
            return {}, f"Fan {fan_id} must be an object."
        min_pwm = _clamp_int(raw.get("min_pwm", MIN_PWM_FLOOR), 0, 100, MIN_PWM_FLOOR)
        entry = {
            "name": str(raw.get("name", fan_id))[:64],
            "enabled": bool(raw.get("enabled", True)),
            "source": _clean_source(raw.get("source")),
            "min_pwm": min_pwm,
            "stop_allowed": bool(raw.get("stop_allowed", False)),
            "hyst_up": _clamp_int(raw.get("hyst_up", DEFAULT_HYST_UP), 0, 20, DEFAULT_HYST_UP),
            "hyst_down": _clamp_int(raw.get("hyst_down", DEFAULT_HYST_DOWN), 0, 20, DEFAULT_HYST_DOWN),
            "response_delay_s": _clamp_int(
                raw.get("response_delay_s", DEFAULT_RESPONSE_DELAY), 0, 120, DEFAULT_RESPONSE_DELAY
            ),
            "step_limit": _clamp_int(raw.get("step_limit", DEFAULT_STEP_LIMIT), 1, 100, DEFAULT_STEP_LIMIT),
            "preset": {},
            "profiles": {},
        }
        presets = raw.get("preset") if isinstance(raw.get("preset"), dict) else {}
        profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
        for key in PROFILE_KEYS:
            preset_name = presets.get(key)
            entry["preset"][key] = preset_name if preset_name in PRESET_SHAPES else "custom"
            points = profiles.get(key)
            if points is None:
                fallback = DEFAULT_PRESET_FOR_PROFILE[key]
                entry["preset"][key] = fallback
                entry["profiles"][key] = preset_curve(fallback, min_pwm)
                continue
            curve, error = validate_curve(points)
            if error:
                return {}, f"{entry['name']} / {key}: {error}"
            entry["profiles"][key] = curve
        cleaned_fans[fan_id] = entry
    return {"version": 1, "enabled": bool(config.get("enabled", False)), "fans": cleaned_fans}, None


def _clamp_int(value, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _clean_source(source) -> dict:
    if not isinstance(source, dict):
        return {"mode": "max", "sensors": ["cpu"]}
    mode = source.get("mode")
    names = source.get("sensors")
    if not isinstance(names, list) or not names:
        names = ["cpu"]
    return {
        "mode": mode if mode in ("max", "avg") else "max",
        "sensors": [str(name)[:80] for name in names[:8]],
    }


def load_config() -> dict:
    config = _read_json(CONFIG_FILE)
    if config is None:
        return default_config()
    cleaned, error = validate_config(config)
    if error:
        # A hand-edited broken config must not leave fans unmanaged: fall back
        # to defaults with the engine disabled rather than raising every tick.
        fallback = default_config()
        fallback["error"] = error
        return fallback
    return cleaned


def save_config(config: dict) -> tuple[bool, str | None]:
    cleaned, error = validate_config(config)
    if error:
        return False, error
    if not _write_json_atomic(CONFIG_FILE, cleaned):
        return False, "Could not write /etc/boost-fans.json."
    return True, None


# ── temperature sources & guards ─────────────────────────────────────

SOURCE_ALIASES = {
    "cpu": "cpu",
    "gpu": "gpu",
    "nvme": "nvme",
    "ssd": "nvme",
    "ram": "ram",
    "vrm": "vrm",
    "chipset": "chipset",
    "board": "board",
}


def source_temp(source: dict, sensor_values: dict) -> tuple[float | None, list[str]]:
    """Resolve a fan's temperature source; returns (temp, resolved_names)."""
    temps: list[float] = []
    resolved: list[str] = []
    for name in source.get("sensors", ["cpu"]):
        category = SOURCE_ALIASES.get(name)
        if category:
            values = [item["temp"] for item in sensor_values.values() if item["category"] == category]
            if category == "cpu":
                values += [
                    item["temp"] for item in sensor_values.values() if item["category"] == "cpu_core"
                ]
            if values:
                temps.append(max(values))
                resolved.append(name)
            continue
        item = sensor_values.get(name)
        if item:
            temps.append(item["temp"])
            resolved.append(name)
    if not temps:
        return None, resolved
    if source.get("mode") == "avg":
        return sum(temps) / len(temps), resolved
    return max(temps), resolved


def guard_floor(sensor_values: dict, thresholds: dict) -> tuple[int, str]:
    """Minimum fan speed the hardware needs right now, whatever the curve says.

    This is what makes a Silent curve safe to pick at any time: the engine
    still speeds the fans up on its own and the dashboard explains why.
    """
    temp_hot = int(thresholds.get("tempHot", 78) or 78)
    temp_critical = int(thresholds.get("tempCritical", 85) or 85)
    floor = 0
    reason = ""

    def bump(value: int, text: str) -> None:
        nonlocal floor, reason
        if value > floor:
            floor, reason = value, text

    # Each reason says when it clears, so the UI can promise "this ends by
    # itself at N C" instead of leaving the user wondering.
    ease = temp_hot - 6
    cpu = sensors.hottest("cpu", sensor_values)
    cores = sensors.hottest("cpu_core", sensor_values)
    if cores is not None:
        cpu = max(cpu, cores) if cpu is not None else cores
    if cpu is not None:
        if cpu >= temp_critical:
            bump(100, f"CPU {cpu}C is at the critical limit ({temp_critical}C); eases off below {ease}C")
        elif cpu >= temp_hot:
            bump(85, f"CPU {cpu}C is above the warm threshold ({temp_hot}C); eases off below {ease}C")
        elif cpu >= ease:
            bump(65, f"CPU {cpu}C is approaching the warm threshold ({temp_hot}C); eases off below {ease}C")

    gpu = sensors.hottest("gpu", sensor_values)
    if gpu is not None:
        if gpu >= 85:
            bump(100, f"GPU {gpu}C is very hot; eases off below 80C")
        elif gpu >= 80:
            bump(80, f"GPU {gpu}C is hot; eases off below 80C")

    nvme = sensors.hottest("nvme", sensor_values)
    if nvme is not None:
        if nvme >= 70:
            bump(80, f"NVMe {nvme}C is throttling hot; eases off below 65C")
        elif nvme >= 65:
            bump(60, f"NVMe {nvme}C is above its warning limit; eases off below 65C")

    vrm = sensors.hottest("vrm", sensor_values)
    if vrm is not None and vrm >= 90:
        bump(90, f"VRM {vrm}C is above its warning limit; eases off below 90C")

    return floor, reason


# ── engine ───────────────────────────────────────────────────────────


class FanEngine:
    """Applies fan curves once per daemon tick. Never raises."""

    def __init__(self, log=None) -> None:
        self.log = log or (lambda message, *args: None)
        self.channels: dict[str, FanChannel] = {}
        self.config: dict = {"version": 1, "enabled": False, "fans": {}}
        self._config_mtime = -1.0
        self._state: dict[str, dict] = {}
        self._owned: dict[str, int] = {}
        self._conflict_service: str | None = None
        self._conflict_checked = 0.0
        self.last_status: dict = {"enabled": False, "supported": False, "fans": []}
        self.refresh_channels()

    # -- setup ------------------------------------------------------

    def refresh_channels(self) -> None:
        self.channels = {channel.id: channel for channel in discover_channels()}

    def reload_config(self) -> None:
        try:
            mtime = os.path.getmtime(CONFIG_FILE)
        except OSError:
            mtime = 0.0
        if mtime != self._config_mtime:
            self.config = load_config()
            self._config_mtime = mtime
            if self.config.get("error"):
                self.log(f"Fan config rejected ({self.config['error']}); engine stays off")

    def _check_conflict(self) -> str | None:
        now = time.time()
        if now - self._conflict_checked > 60:
            self._conflict_checked = now
            self._conflict_service = conflicting_service()
        return self._conflict_service

    def _take_ownership(self, channel: FanChannel) -> bool:
        """Switch a channel to manual pwm, remembering the BIOS mode first."""
        if channel.id in self._owned:
            # Suspend/resume and some BIOS events silently reset pwmN_enable
            # back to automatic; re-assert it instead of writing pwm values
            # the board is no longer reading.
            if channel.read_enable() != 1 and not channel.write_enable(1):
                del self._owned[channel.id]
                channel.read_only = True
                return False
            return True
        original = channel.read_enable()
        if original == -1:
            return False
        saved = _read_json(ORIGINAL_ENABLE_FILE) or {}
        if channel.id not in saved:
            saved[channel.id] = {"enable": original, "pwm": channel.read_pwm(), "path": channel.enable_path}
            _write_json_atomic(ORIGINAL_ENABLE_FILE, saved)
        if original != 1 and not channel.write_enable(1):
            channel.read_only = True
            self.log(f"Fan {channel.id}: pwm_enable is read-only on this board; skipping")
            return False
        self._owned[channel.id] = original
        return True

    # -- main tick --------------------------------------------------

    def tick(self, sensor_values: dict, profile: str, thresholds: dict) -> dict:
        try:
            return self._tick(sensor_values, profile, thresholds)
        except Exception as exc:  # noqa: BLE001 - a fan bug must never kill the daemon
            self.log(f"Fan engine error (recovered): {exc!r}")
            return self.last_status

    def _tick(self, sensor_values: dict, profile: str, thresholds: dict) -> dict:
        self.reload_config()
        now = time.time()
        profile_key = PROFILE_MAP.get(profile, "balanced")
        floor, floor_reason = guard_floor(sensor_values, thresholds)
        status = {
            "enabled": bool(self.config.get("enabled")),
            "supported": bool(self.channels),
            "profileKey": profile_key,
            "guard": {"floor": floor, "reason": floor_reason, "active": False},
            "conflict": None,
            "configError": self.config.get("error"),
            "fans": [],
        }

        conflict = self._check_conflict()
        if conflict:
            status["conflict"] = f"{conflict} is already controlling the fans"

        if not self.channels:
            self.last_status = status
            return status

        paused = self._paused(now)
        override = self._override(now)
        engine_on = status["enabled"] and not conflict and not paused

        for fan_id, channel in self.channels.items():
            fan_config = self.config.get("fans", {}).get(fan_id)
            entry = {
                "id": fan_id,
                "name": (fan_config or {}).get("name", fan_id),
                "rpm": channel.read_rpm(),
                "pwm": raw_to_pct(channel.read_pwm()),
                "controlled": False,
                "readOnly": channel.read_only,
                "mode": "bios",
                "sourceTemp": None,
                "target": None,
                "note": "",
            }
            if not fan_config or not fan_config.get("enabled", True) or not engine_on:
                if paused:
                    entry["note"] = "Engine paused (calibration or manual test in progress)"
                    entry["mode"] = "paused"
                elif conflict:
                    entry["note"] = status["conflict"]
                status["fans"].append(entry)
                continue

            state = self._state.setdefault(
                fan_id,
                {"temp": None, "pwm": None, "changed": 0.0, "mismatch": 0, "backoff": 0.0, "written": None},
            )

            # External writer detection: someone else moved a pwm we own.
            if state["written"] is not None and now > state["changed"] + 1:
                actual = channel.read_pwm()
                if abs(actual - state["written"]) > CONFLICT_TOLERANCE:
                    state["mismatch"] += 1
                    if state["mismatch"] >= 2 and now > state["backoff"]:
                        state["backoff"] = now + CONFLICT_BACKOFF_S
                        self.log(
                            f"Fan {fan_id}: another program is writing this pwm "
                            f"(expected {state['written']}, found {actual}); backing off"
                        )
                else:
                    state["mismatch"] = 0
            if now < state["backoff"]:
                entry["note"] = "Another program is writing this fan; Boost backed off"
                entry["mode"] = "backoff"
                status["fans"].append(entry)
                continue

            temp, resolved = source_temp(fan_config.get("source", {}), sensor_values)
            if temp is None:
                entry["note"] = "Temperature source not available"
                status["fans"].append(entry)
                continue
            entry["sourceTemp"] = int(round(temp))
            entry["source"] = ",".join(resolved)

            # Hysteresis: the *control* temperature only follows the sensor
            # once it has moved far enough, which is what stops the 1C
            # wobble everybody complains about.
            control_temp = state["temp"]
            hyst_up = fan_config.get("hyst_up", DEFAULT_HYST_UP)
            hyst_down = fan_config.get("hyst_down", DEFAULT_HYST_DOWN)
            if control_temp is None or temp >= control_temp + hyst_up or temp <= control_temp - hyst_down:
                control_temp = temp
                state["temp"] = temp

            requested = curve_pwm(fan_config["profiles"].get(profile_key, []), control_temp)
            min_pwm = int(fan_config.get("min_pwm", MIN_PWM_FLOOR))
            if requested <= 0 and fan_config.get("stop_allowed"):
                target = 0
            else:
                target = max(requested, min_pwm)

            # "Guarded" means the safety floor is what is holding this fan up,
            # whether it had to raise the curve or the curve happens to agree.
            guarded = floor > 0 and floor >= target
            if floor > target:
                target = floor
            if guarded:
                status["guard"]["active"] = True

            override_pwm = override.get(fan_id)
            if override_pwm is not None:
                target = max(override_pwm, floor)
                entry["mode"] = "test"
                entry["note"] = f"Manual test at {override_pwm}% (guard floor {floor}%)"

            current = state["pwm"] if state["pwm"] is not None else raw_to_pct(channel.read_pwm())
            step = int(fan_config.get("step_limit", DEFAULT_STEP_LIMIT))
            delay = int(fan_config.get("response_delay_s", DEFAULT_RESPONSE_DELAY))
            if target < current and not guarded and override_pwm is None:
                # Slowing down is delayed; speeding up never is.
                if now - state["changed"] < delay:
                    target = current
            if target > current:
                target = min(target, current + step)
            elif target < current:
                target = max(target, current - step)

            if not self._take_ownership(channel):
                entry["readOnly"] = True
                entry["note"] = "This board exposes pwm read-only"
                status["fans"].append(entry)
                continue

            raw = pct_to_raw(target)
            if state["written"] is None or abs(raw - state["written"]) >= 2 or state["pwm"] != target:
                if channel.write_pwm(raw):
                    state["written"] = raw
                    state["changed"] = now
                else:
                    entry["readOnly"] = True
                    entry["note"] = "This board exposes pwm read-only"
                    status["fans"].append(entry)
                    continue
            state["pwm"] = target

            entry["pwm"] = target
            entry["target"] = target
            entry["controlled"] = True
            if entry["mode"] == "bios":
                entry["mode"] = "guard" if guarded else "curve"
            if guarded and not entry["note"]:
                entry["note"] = floor_reason
            entry["rpm"] = channel.read_rpm()
            status["fans"].append(entry)

        self.last_status = status
        return status

    # -- helpers ----------------------------------------------------

    def _paused(self, now: float) -> bool:
        raw = read_text(PAUSE_FILE, "")
        if not raw:
            return False
        try:
            return float(raw) > now
        except ValueError:
            return False

    def _override(self, now: float) -> dict[str, int]:
        data = _read_json(OVERRIDE_FILE)
        if not isinstance(data, dict):
            return {}
        try:
            if float(data.get("until", 0)) <= now:
                return {}
        except (TypeError, ValueError):
            return {}
        fan_id = data.get("fan")
        pwm = data.get("pwm")
        if not isinstance(fan_id, str) or not isinstance(pwm, (int, float)):
            return {}
        return {fan_id: max(0, min(100, int(pwm)))}

    def release(self) -> None:
        """Hand every owned channel back to BIOS control."""
        failsafe_all(self.log)
        self._owned.clear()


def failsafe_all(log=None) -> int:
    """Restore original pwmN_enable for every channel Boost ever took over.

    Wired to ExecStopPost so a crash, a stop, or a package upgrade can never
    leave fans stuck at whatever pwm was last written.
    """
    log = log or (lambda message: None)
    saved = _read_json(ORIGINAL_ENABLE_FILE) or {}
    restored = 0
    channels = {channel.id: channel for channel in discover_channels()}
    for fan_id, entry in saved.items():
        channel = channels.get(fan_id)
        if channel is None:
            continue
        value = entry.get("enable") if isinstance(entry, dict) else entry
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        # 1 is manual pwm — restoring that would leave the fan pinned, so hand
        # it to the board's automatic mode instead.
        target = value if value not in (1, -1) else _automatic_enable_value(channel)
        if channel.write_enable(target):
            restored += 1
    if restored:
        log(f"Fan failsafe: {restored} channel(s) returned to board control")
    try:
        os.remove(ORIGINAL_ENABLE_FILE)
    except OSError:
        pass
    try:
        os.remove(OVERRIDE_FILE)
    except OSError:
        pass
    return restored


def _automatic_enable_value(channel: FanChannel) -> int:
    """Best automatic mode for this chip (nct6775 Smart Fan IV = 5, else 2)."""
    driver = read_text(os.path.join(channel.hwmon_path, "name"), "")
    if driver.startswith("nct6"):
        return 5
    return 2


# ── calibration ──────────────────────────────────────────────────────


def pause_engine(seconds: float) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(PAUSE_FILE, "w", encoding="utf-8") as handle:
            handle.write(str(time.time() + seconds))
    except OSError:
        pass


def resume_engine() -> None:
    try:
        os.remove(PAUSE_FILE)
    except OSError:
        pass


def calibrate(fan_ids=None, settle: float = 3.0, verbose: bool = True) -> dict:
    """Measure start/stop pwm and the RPM curve of each fan.

    Tolerant of fans that never stop (many CPU coolers and all zero-RPM-less
    designs): those are recorded with stop_allowed=false instead of failing the
    run, which is where pwmconfig traditionally gives up.
    """
    channels = [c for c in discover_channels() if not fan_ids or c.id in fan_ids]
    results = _read_json(CALIBRATION_FILE) or {}
    if not channels:
        return results
    total = len(channels) * (settle * 9 + 6)
    pause_engine(total + 30)
    try:
        for channel in channels:
            if verbose:
                print(f"Calibrating {channel.id} ...", flush=True)
            original = channel.read_enable()
            original_pwm = channel.read_pwm()
            if original == -1 or (original != 1 and not channel.write_enable(1)):
                if verbose:
                    print(f"  {channel.id}: pwm is read-only on this board, skipping")
                results[channel.id] = {"supported": False, "reason": "pwm read-only"}
                continue

            entry: dict = {"supported": True, "rpm": {}, "stop_allowed": False}
            channel.write_pwm(255)
            time.sleep(settle)
            entry["max_rpm"] = channel.read_rpm()

            stop_pwm = None
            for pct in (80, 60, 50, 40, 30, 20, 15, 10, 0):
                channel.write_pwm(pct_to_raw(pct))
                time.sleep(settle)
                rpm = channel.read_rpm()
                entry["rpm"][str(pct)] = rpm
                if rpm is not None and rpm == 0 and stop_pwm is None:
                    stop_pwm = pct
                    entry["stop_allowed"] = True
                    break

            start_pwm = None
            for pct in (10, 15, 20, 25, 30, 40, 50):
                channel.write_pwm(pct_to_raw(pct))
                time.sleep(settle)
                rpm = channel.read_rpm()
                if rpm is None:
                    break
                if rpm > 0:
                    start_pwm = pct
                    break
            if channel.read_rpm() is None:
                entry["rpm_sensor"] = False
                entry["stop_allowed"] = False
            else:
                entry["rpm_sensor"] = True
            entry["stop_pwm"] = stop_pwm
            entry["start_pwm"] = start_pwm or MIN_PWM_FLOOR
            entry["min_pwm"] = max(MIN_PWM_FLOOR, (start_pwm or MIN_PWM_FLOOR))
            entry["name"] = f"{channel.chip} fan {channel.index}"
            entry["calibrated_at"] = int(time.time())
            results[channel.id] = entry
            if verbose:
                print(
                    f"  {channel.id}: max {entry.get('max_rpm')} RPM, "
                    f"starts at {entry['start_pwm']}%, "
                    f"{'can stop' if entry['stop_allowed'] else 'never stops'}"
                )

            channel.write_pwm(original_pwm)
            channel.write_enable(original if original not in (-1,) else _automatic_enable_value(channel))
        _write_json_atomic(CALIBRATION_FILE, results)
    finally:
        resume_engine()
    return results


def apply_calibration_to_config(calibration: dict | None = None) -> tuple[bool, str | None]:
    """Regenerate 1-click presets from measured minimum speeds."""
    calibration = calibration if calibration is not None else (_read_json(CALIBRATION_FILE) or {})
    config = load_config()
    config.pop("error", None)
    for fan_id, entry in calibration.items():
        fan = config.get("fans", {}).get(fan_id)
        if not fan or not entry.get("supported", True):
            continue
        fan["min_pwm"] = max(MIN_PWM_FLOOR, int(entry.get("min_pwm", MIN_PWM_FLOOR)))
        fan["stop_allowed"] = bool(entry.get("stop_allowed", False))
        for key in PROFILE_KEYS:
            preset = fan.get("preset", {}).get(key)
            if preset in PRESET_SHAPES:
                fan["profiles"][key] = preset_curve(preset, fan["min_pwm"])
    return save_config(config)


# ── CLI ──────────────────────────────────────────────────────────────


def _cli_status() -> int:
    channels = discover_channels()
    config = load_config()
    conflict = conflicting_service()
    print(f"Fan engine: {'enabled' if config.get('enabled') else 'disabled'}")
    if config.get("error"):
        print(f"Config problem: {config['error']}")
    if conflict:
        print(f"Conflict: {conflict} is active and also controls fans")
    if not channels:
        print("No controllable fans found (no writable pwmN_enable under /sys/class/hwmon).")
        return 0
    for channel in channels:
        fan = config.get("fans", {}).get(channel.id, {})
        rpm = channel.read_rpm()
        print(
            f"{channel.id:<24} {fan.get('name', '-'):<22} "
            f"pwm={raw_to_pct(channel.read_pwm()):>3}%  "
            f"rpm={rpm if rpm is not None else '-':>6}  "
            f"mode={channel.read_enable()}"
        )
    return 0


def _cli_test(args) -> int:
    if len(args) < 2:
        print("Usage: fancontrol.py test <fan_id> <pwm%> [seconds]", file=sys.stderr)
        return 1
    fan_id, pwm = args[0], _clamp_int(args[1], 0, 100, 50)
    seconds = _clamp_int(args[2], 1, 120, 10) if len(args) > 2 else 10
    payload = {"fan": fan_id, "pwm": pwm, "until": time.time() + seconds}
    if not _write_json_atomic(OVERRIDE_FILE, payload):
        print("Could not write the fan override file.", file=sys.stderr)
        return 1
    print(f"Testing {fan_id} at {pwm}% for {seconds}s (thermal guard still applies).")
    return 0


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "status"
    args = argv[2:]
    if command == "discover":
        print(json.dumps([
            {"id": c.id, "chip": c.chip, "index": c.index, "hasRpm": bool(c.rpm_path)}
            for c in discover_channels()
        ], indent=2))
    elif command == "status":
        return _cli_status()
    elif command == "init":
        if os.path.exists(CONFIG_FILE):
            print(f"{CONFIG_FILE} already exists.")
            return 0
        ok, error = save_config(default_config())
        print(f"Created {CONFIG_FILE}" if ok else f"Failed: {error}")
        return 0 if ok else 1
    elif command == "calibrate":
        results = calibrate(args or None)
        ok, error = apply_calibration_to_config(results)
        if not ok:
            print(f"Calibration saved but presets not updated: {error}", file=sys.stderr)
        print(f"Calibration written to {CALIBRATION_FILE}")
    elif command in ("enable", "disable"):
        config = load_config()
        error = config.pop("error", None)
        if error and command == "enable":
            print(f"Refusing to enable: {error}", file=sys.stderr)
            return 1
        if command == "enable" and not discover_channels():
            print("No controllable fans found; nothing to enable.", file=sys.stderr)
            return 1
        conflict = conflicting_service() if command == "enable" else None
        if conflict:
            print(f"Refusing to enable: {conflict} is already controlling the fans.", file=sys.stderr)
            return 1
        config["enabled"] = command == "enable"
        ok, error = save_config(config)
        if not ok:
            print(f"Failed: {error}", file=sys.stderr)
            return 1
        if command == "disable":
            failsafe_all(print)
        print(f"Fan curve engine {'enabled' if config['enabled'] else 'disabled'}.")
    elif command == "failsafe":
        failsafe_all(print)
    elif command == "test":
        return _cli_test(args)
    elif command == "preset":
        if len(args) < 2 or args[1] not in PRESET_SHAPES:
            print(f"Usage: fancontrol.py preset <fan_id> <{'|'.join(PRESET_SHAPES)}> [profile]", file=sys.stderr)
            return 1
        config = load_config()
        config.pop("error", None)
        fan = config.get("fans", {}).get(args[0])
        if not fan:
            print(f"Unknown fan: {args[0]}", file=sys.stderr)
            return 1
        keys = [args[2]] if len(args) > 2 and args[2] in PROFILE_KEYS else list(PROFILE_KEYS)
        for key in keys:
            fan["preset"][key] = args[1]
            fan["profiles"][key] = preset_curve(args[1], fan.get("min_pwm", MIN_PWM_FLOOR))
        ok, error = save_config(config)
        print(f"{args[0]}: {args[1]} preset applied" if ok else f"Failed: {error}")
        return 0 if ok else 1
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
