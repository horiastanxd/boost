"""Windows implementation of the Boost platform backend.

Scope is deliberately narrower than Linux. Windows exposes no sysfs, no RAPL
and no EPP, and fan control there needs a signed kernel driver — which Boost
will not ship, because a userspace tool that half-owns the fans is worse than
one that leaves them to the firmware. What Windows *does* offer through
supported, documented interfaces is:

  * power schemes and power-mode overlays  -> `powercfg`
  * processor performance boost (turbo)    -> `powercfg` PERFBOOSTMODE
  * NVIDIA telemetry and watt limits       -> `nvidia-smi`, same as on Linux
  * CPU load                               -> psutil, else GetSystemTimes
  * CPU temperature                        -> ACPI thermal zone, best effort

Everything here is best effort by design: a machine that cannot report its
temperature reports 0 and the dashboard shows "n/a" rather than a guess.

Scheme aliases (SCHEME_MIN / SCHEME_BALANCED / SCHEME_MAX) are used instead of
raw GUIDs because OEM images frequently ship their own scheme GUIDs. Windows
11 often exposes only Balanced plus power-mode *overlays*, so each profile has
a scheme and an overlay, and whichever the machine accepts is used.
"""

from __future__ import annotations

import ctypes
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import boost_paths
from platform_backend import (
    PlatformBackend,
    _nvidia_smi_limit_range,
    _nvidia_smi_stats,
    run,
)

#: Saved on the first profile switch so `restore` can put the machine back.
ORIGINAL_SCHEME_FILE = boost_paths.STATE_DIR / "original-scheme"

#: PERFBOOSTMODE values, from the documented processor power settings.
BOOST_DISABLED = 0
BOOST_ENABLED = 1
BOOST_AGGRESSIVE = 2

#: profile -> (scheme alias, power-mode overlay alias, PERFBOOSTMODE)
PROFILE_PLANS = {
    "boost": ("SCHEME_MIN", "OVERLAY_SCHEME_MAX", BOOST_AGGRESSIVE),
    "powersave": ("SCHEME_BALANCED", "SCHEME_CURRENT", BOOST_ENABLED),
    "silent": ("SCHEME_MAX", "OVERLAY_SCHEME_MIN", BOOST_DISABLED),
}

_GUID_RE = re.compile(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})")


def _powercfg(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    return run(["powercfg", *args], timeout=timeout)


def active_scheme_guid() -> str:
    """GUID of the scheme currently in force, or "" when powercfg is unusable."""
    result = _powercfg("/getactivescheme", timeout=8)
    if result.returncode != 0:
        return ""
    match = _GUID_RE.search(result.stdout)
    return match.group(1) if match else ""


def _remember_original_scheme() -> None:
    """Capture the boot-time scheme once, the way power-save-originals does."""
    if ORIGINAL_SCHEME_FILE.exists():
        return
    guid = active_scheme_guid()
    if not guid:
        return
    boost_paths.ensure_state_dir()
    try:
        ORIGINAL_SCHEME_FILE.write_text(guid, encoding="utf-8")
    except OSError:
        pass  # unprivileged run: restore falls back to Balanced


def _set_boost_mode(value: int) -> bool:
    """Set PERFBOOSTMODE on the active scheme, on AC and on battery."""
    ok = True
    for verb in ("/setacvalueindex", "/setdcvalueindex"):
        result = _powercfg(verb, "SCHEME_CURRENT", "SUB_PROCESSOR", "PERFBOOSTMODE", str(value))
        ok = ok and result.returncode == 0
    # The index writes only take effect once the scheme is re-activated.
    _powercfg("/setactive", "SCHEME_CURRENT")
    return ok


def _activate(scheme: str, overlay: str) -> tuple[bool, str]:
    """Activate a scheme, falling back to the power-mode overlay.

    Returns (ok, which interface actually applied).
    """
    if _powercfg("/setactive", scheme).returncode == 0:
        return True, "power scheme"
    if _powercfg("/overlaysetactive", overlay).returncode == 0:
        return True, "power mode"
    return False, ""


class WindowsBackend(PlatformBackend):
    name = "Windows"
    reads_sysfs = False

    # Windows v1 intentionally ships none of these. See docs/ARCHITECTURE.md.
    supports_fan_control = False
    supports_auto_daemon = False
    supports_rapl = False
    supports_epp = False
    supports_tray = False

    # ── profiles ──────────────────────────────────────────────────────────
    def apply_profile(self, profile: str) -> dict[str, Any]:
        if profile == "restore":
            return self._restore()
        plan = PROFILE_PLANS.get(profile)
        if plan is None:
            return {"ok": False, "message": "Unknown profile."}
        scheme, overlay, boost_mode = plan
        _remember_original_scheme()
        ok, via = _activate(scheme, overlay)
        if not ok:
            return {
                "ok": False,
                "message": "powercfg refused to change the power plan. "
                           "Run Boost from an elevated prompt.",
            }
        turbo = _set_boost_mode(boost_mode)
        note = "" if turbo else " (turbo setting needs administrator rights)"
        return {"ok": True, "message": f"{profile.capitalize()} applied via {via}.{note}"}

    def _restore(self) -> dict[str, Any]:
        guid = ""
        try:
            guid = ORIGINAL_SCHEME_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        target = guid or "SCHEME_BALANCED"
        if _powercfg("/setactive", target).returncode != 0:
            _powercfg("/overlaysetactive", "SCHEME_CURRENT")
        # Hand the boost decision back to Windows' own default.
        _set_boost_mode(BOOST_ENABLED)
        where = "the plan active before Boost" if guid else "Balanced"
        return {"ok": True, "message": f"Restored {where}."}

    def power_profile(self) -> str:
        result = _powercfg("/getactivescheme", timeout=8)
        if result.returncode != 0:
            return "unknown"
        # powercfg prints: Power Scheme GUID: <guid>  (Balanced)
        match = re.search(r"\(([^)]+)\)\s*$", result.stdout.strip())
        return match.group(1) if match else "unknown"

    # ── CPU ───────────────────────────────────────────────────────────────
    _load_lock = threading.Lock()
    _last_idle = 0
    _last_busy = 0

    def get_cpu_load(self) -> int:
        try:
            import psutil  # optional; a nicer reading when it happens to be there
        except ImportError:
            return self._load_from_system_times()
        try:
            return int(psutil.cpu_percent(interval=None))
        except Exception:  # noqa: BLE001 - fall back rather than break the page
            return self._load_from_system_times()

    def _load_from_system_times(self) -> int:
        """CPU busy percentage from kernel32!GetSystemTimes, no dependencies."""

        class _FILETIME(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

            @property
            def value(self) -> int:
                return (self.high << 32) | self.low

        idle_t, kernel_t, user_t = _FILETIME(), _FILETIME(), _FILETIME()
        try:
            ok = ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
                ctypes.byref(idle_t), ctypes.byref(kernel_t), ctypes.byref(user_t)
            )
        except (AttributeError, OSError):
            return 0
        if not ok:
            return 0
        idle = idle_t.value
        # Kernel time includes idle time, so busy = kernel + user - idle.
        busy = kernel_t.value + user_t.value - idle
        with self._load_lock:
            prev_idle, prev_busy = self._last_idle, self._last_busy
            WindowsBackend._last_idle, WindowsBackend._last_busy = idle, busy
        if prev_busy == 0 and prev_idle == 0:
            return 0
        delta_busy, delta_idle = busy - prev_busy, idle - prev_idle
        total = delta_busy + delta_idle
        if total <= 0:
            return 0
        return max(0, min(100, int(100 * delta_busy / total)))

    def get_cpu_temp(self) -> int:
        """ACPI thermal zone temperature in °C, or 0 when the firmware hides it.

        Most desktop boards expose no usable thermal zone, so 0 is the common
        answer and callers must render it as "n/a" rather than as a cold CPU.
        """
        result = run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-CimInstance -Namespace root/wmi "
             "-ClassName MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue "
             "| Measure-Object -Property CurrentTemperature -Maximum).Maximum"],
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return 0
        try:
            deci_kelvin = float(result.stdout.strip().splitlines()[0])
        except (ValueError, IndexError):
            return 0
        celsius = deci_kelvin / 10.0 - 273.15
        # Reject the obviously bogus values some firmwares report.
        return int(celsius) if 0 < celsius < 150 else 0

    def get_sensors(self) -> list[dict[str, Any]]:
        """Component temperatures, in the same shape sensors.py produces.

        Windows has no hwmon tree: only the CPU thermal zone and the GPU are
        readable without a kernel driver, so those are the only two cards.
        """
        groups: list[dict[str, Any]] = []
        cpu = self.get_cpu_temp()
        if cpu:
            groups.append(_group("cpu", "CPU", cpu, warn=85, crit=95))
        gpu = self.get_gpu_stats().get("temp", "")
        try:
            gpu_temp = int(float(gpu))
        except (TypeError, ValueError):
            gpu_temp = 0
        if gpu_temp:
            groups.append(_group("gpu", "GPU", gpu_temp, warn=83, crit=90))
        return groups

    # ── GPU ───────────────────────────────────────────────────────────────
    def get_gpu_stats(self) -> dict[str, str]:
        return _nvidia_smi_stats()

    def gpu_power_limit_range(self) -> tuple[int, int]:
        return _nvidia_smi_limit_range()

    def set_gpu_power_limit(self, watts: int, profile: str = "boost") -> dict[str, Any]:
        low, high = self.gpu_power_limit_range()
        if not high:
            return {"ok": False, "message": "No NVIDIA GPU with an adjustable power limit was found."}
        clamped = max(low, min(high, int(watts)))
        result = run(["nvidia-smi", "-pl", str(clamped)], timeout=20)
        if result.returncode == 0:
            note = "" if clamped == watts else f" (clamped to the driver range {low}-{high} W)"
            return {"ok": True, "message": f"GPU limited to {clamped} W{note}."}
        message = (result.stderr or result.stdout).strip()
        if "permission" in message.lower() or "privile" in message.lower():
            message = "Setting the GPU power limit needs an elevated prompt (Run as administrator)."
        return {"ok": False, "message": message.splitlines()[-1] if message else "nvidia-smi failed."}


def _group(category: str, label: str, temp: int, warn: int, crit: int) -> dict[str, Any]:
    state = "critical" if temp >= crit else "warn" if temp >= warn else "ok"
    reading = {
        "key": category, "label": label, "category": category,
        "temp": temp, "warn": warn, "crit": crit, "state": state,
    }
    return {
        "category": category, "label": label, "bulk": False,
        "max": temp, "warn": warn, "crit": crit, "state": state,
        "sensors": [reading],
    }
