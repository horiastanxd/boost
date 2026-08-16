"""Platform abstraction for the parts of Boost that are not Linux-specific.

Boost grew up talking straight to sysfs, RAPL and systemd. None of that exists
on Windows, so everything that has to be done differently per platform is
funnelled through one small interface here:

    apply_profile(name)          switch power profile
    get_cpu_temp()               package temperature in °C, 0 when unknown
    get_cpu_load()               busy percentage since the previous call
    get_gpu_stats()              GPU watts / limit / temperature / utilisation
    set_gpu_power_limit(w, p)    clamp the GPU to a watt budget
    get_sensors()                per-component temperature groups
    gpu_power_limit_range()      (min, max) watts the driver will accept

`get_backend()` returns the implementation for the running platform. The Linux
implementation delegates to the existing CLI and to sensors.py — it changes no
behaviour. The Windows implementation lives in platform_windows.py.

Capability flags say what a platform genuinely supports, so callers can refuse
an action with a clear message instead of failing obscurely. Windows v1 has no
fan control (that needs a kernel driver), no RAPL, no EPP and no auto daemon.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import boost_paths

PROFILES = ("boost", "powersave", "silent", "restore")


def run(cmd: list[str], timeout: float = 4.0) -> subprocess.CompletedProcess[str]:
    """Run a command without raising; callers inspect returncode themselves."""
    kwargs: dict[str, Any] = {}
    if boost_paths.IS_WINDOWS:
        # Keep console windows from flashing up when the dashboard shells out.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        cmd, text=True, capture_output=True, timeout=timeout, check=False, **kwargs
    )


class PlatformBackend:
    """Contract every platform implementation fulfils.

    Methods return plain data, never raise for "this machine cannot tell me":
    an unknown temperature is 0, unknown stats are empty strings. Actions
    return {"ok": bool, "message": str} so the dashboard can show the result
    verbatim.
    """

    name = "unknown"

    #: True when the process can read Linux sysfs directly. Callers that
    #: already have hand-tuned sysfs readers keep using them when this is set.
    reads_sysfs = False

    supports_fan_control = False
    supports_auto_daemon = False
    supports_rapl = False
    supports_epp = False
    supports_tray = False

    def apply_profile(self, profile: str) -> dict[str, Any]:
        raise NotImplementedError

    def get_cpu_temp(self) -> int:
        raise NotImplementedError

    def get_cpu_load(self) -> int:
        raise NotImplementedError

    def get_gpu_stats(self) -> dict[str, str]:
        raise NotImplementedError

    def set_gpu_power_limit(self, watts: int, profile: str = "boost") -> dict[str, Any]:
        raise NotImplementedError

    def get_sensors(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def gpu_power_limit_range(self) -> tuple[int, int]:
        raise NotImplementedError

    def power_profile(self) -> str:
        """Human-readable name of the profile currently in force."""
        raise NotImplementedError

    def unsupported(self, what: str) -> dict[str, Any]:
        return {"ok": False, "message": f"{what} is not supported on {self.name}."}


# ──────────────────────────────────────────────────────────────────────────
# Linux
# ──────────────────────────────────────────────────────────────────────────
class LinuxBackend(PlatformBackend):
    """Delegates to the shell CLI and to sensors.py — no behaviour of its own.

    The installed commands stay the single source of truth for what a profile
    means; this class only decides which one to run.
    """

    name = "Linux"
    reads_sysfs = True
    supports_fan_control = True
    supports_auto_daemon = True
    supports_rapl = True
    supports_epp = True
    supports_tray = True

    BIN = "/usr/local/bin"

    #: silent runs with --auto so the thermal interlock can queue the request
    #: instead of forcing a Silent curve onto a hot machine.
    PROFILE_ARGV = {
        "boost": ["boost"],
        "powersave": ["powersave"],
        "silent": ["silent", "--auto"],
        "restore": ["restore"],
    }

    def _cmd(self, argv: list[str]) -> list[str]:
        return [f"{self.BIN}/{argv[0]}", *argv[1:]]

    def apply_profile(self, profile: str) -> dict[str, Any]:
        argv = self.PROFILE_ARGV.get(profile)
        if argv is None:
            return {"ok": False, "message": "Unknown profile."}
        result = run(self._cmd(argv), timeout=30)
        if result.returncode == 0:
            if profile == "silent" and "queued" in result.stdout:
                # The thermal interlock held the request back; show its own
                # words instead of a misleading "Silent applied successfully".
                queued = [
                    line for line in result.stdout.splitlines()
                    if "queued" in line or "Not applying" in line
                ]
                return {
                    "ok": True,
                    "queued": True,
                    "message": " ".join(queued[:2]).replace("[SILENT] ", ""),
                }
            return {"ok": True, "message": f"{profile.capitalize()} applied successfully."}
        full_err = (result.stderr or result.stdout).strip()
        return {"ok": False, "message": full_err.split("\n")[-1] if full_err else "Unknown error"}

    def auto(self, args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
        """Run the `auto` CLI. Only meaningful where the daemon exists."""
        return run(self._cmd(["auto", *args]), timeout=timeout)

    def set_gpu_power_limit(self, watts: int, profile: str = "boost") -> dict[str, Any]:
        result = self.auto(["gpu-limit", str(watts), profile], timeout=15)
        note = (result.stdout or result.stderr).strip().splitlines()
        return {
            "ok": result.returncode == 0,
            "message": note[-1] if note else f"GPU limit for {profile}: {watts} W.",
        }

    def get_sensors(self) -> list[dict[str, Any]]:
        try:
            import sensors as sensor_layer
        except ImportError:
            return []
        try:
            return sensor_layer.group_by_category(sensor_layer.get_all())
        except Exception:  # noqa: BLE001 - a bad sensor must not break a caller
            return []

    def get_cpu_temp(self) -> int:
        try:
            import sensors as sensor_layer
        except ImportError:
            return 0
        try:
            readings = sensor_layer.get_all()
        except Exception:  # noqa: BLE001
            return 0
        for category in ("cpu_package", "cpu", "cpu_core"):
            hottest = sensor_layer.hottest(category, readings)
            if hottest:
                return int(hottest)
        return 0

    _cpu_lock = threading.Lock()
    _last_total = 0
    _last_idle = 0

    def get_cpu_load(self) -> int:
        try:
            fields = Path("/proc/stat").read_text(encoding="utf-8").split("\n")[0].split()[1:]
            values = [int(v) for v in fields]
        except (OSError, ValueError, IndexError):
            return 0
        total, idle = sum(values), values[3] + (values[4] if len(values) > 4 else 0)
        with self._cpu_lock:
            prev_total, prev_idle = self._last_total, self._last_idle
            LinuxBackend._last_total, LinuxBackend._last_idle = total, idle
        delta_total = total - prev_total
        if prev_total == 0 or delta_total <= 0:
            return 0
        return max(0, min(100, int(100 * (delta_total - (idle - prev_idle)) / delta_total)))

    def get_gpu_stats(self) -> dict[str, str]:
        return _nvidia_smi_stats()

    def gpu_power_limit_range(self) -> tuple[int, int]:
        return _nvidia_smi_limit_range()

    def power_profile(self) -> str:
        result = run(["powerprofilesctl", "get"], timeout=3)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        governor = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
        try:
            return governor.read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            return "unknown"


# ──────────────────────────────────────────────────────────────────────────
# nvidia-smi — the one hardware interface that is identical on both platforms
# ──────────────────────────────────────────────────────────────────────────
_NVIDIA_QUERY = "power.draw,power.limit,temperature.gpu,utilization.gpu"


def _nvidia_smi_stats() -> dict[str, str]:
    """GPU telemetry via nvidia-smi. Empty strings when there is no NVIDIA GPU."""
    blank = {"power": "", "limit": "", "temp": "", "util": "", "vendor": "none"}
    try:
        result = run(
            ["nvidia-smi", f"--query-gpu={_NVIDIA_QUERY}",
             "--format=csv,noheader,nounits", "-i", "0"],
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return blank
    if result.returncode != 0 or not result.stdout.strip():
        return blank
    parts = [p.strip() for p in result.stdout.strip().splitlines()[0].split(",")]
    if len(parts) != 4:
        return blank
    stats = dict(zip(("power", "limit", "temp", "util"), parts))
    stats["vendor"] = "nvidia"
    return stats


def _nvidia_smi_limit_range() -> tuple[int, int]:
    """(min, max) watts the driver accepts, or (0, 0) when unknown."""
    try:
        result = run(
            ["nvidia-smi", "--query-gpu=power.min_limit,power.max_limit",
             "--format=csv,noheader,nounits", "-i", "0"],
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return (0, 0)
    if result.returncode != 0:
        return (0, 0)
    parts = [p.strip() for p in result.stdout.strip().splitlines()[0].split(",")] if result.stdout.strip() else []
    if len(parts) != 2:
        return (0, 0)
    try:
        return (int(float(parts[0])), int(float(parts[1])))
    except ValueError:
        return (0, 0)


# ──────────────────────────────────────────────────────────────────────────
# Selection
# ──────────────────────────────────────────────────────────────────────────
_BACKEND: PlatformBackend | None = None
_BACKEND_LOCK = threading.Lock()


def get_backend() -> PlatformBackend:
    """The backend for the running platform, created once."""
    global _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is None:
            if boost_paths.IS_WINDOWS:
                from platform_windows import WindowsBackend

                _BACKEND = WindowsBackend()
            else:
                _BACKEND = LinuxBackend()
        return _BACKEND
