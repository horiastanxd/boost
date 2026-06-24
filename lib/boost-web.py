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
import subprocess
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


def read_config() -> dict[str, str]:
    config: dict[str, str] = {}
    if not CONF_FILE.exists():
        return config
    for line in CONF_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip()
    return config


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
    if mode == "calm":
        thresholds.update(
            {
                "tempHot": 80,
                "boostTempLimit": 80,
                "loadHigh": 85,
                "loadHighDuration": 300,
                "loadIdle": 5,
                "loadIdleDuration": 1200,
                "promptCooldown": 3600,
            }
        )
    elif mode == "summer":
        thresholds.update(
            {
                "tempCritical": 82,
                "tempHot": 74,
                "boostTempLimit": 70,
                "loadHigh": 90,
                "loadHighDuration": 360,
                "loadIdle": 15,
                "loadIdleDuration": 180,
                "promptCooldown": 1800,
            }
        )
    elif mode == "active":
        thresholds.update(
            {
                "tempHot": 76,
                "boostTempLimit": 76,
                "loadHigh": 65,
                "loadHighDuration": 45,
                "loadIdle": 12,
                "loadIdleDuration": 240,
                "promptCooldown": 300,
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


def ambient_temp(config: dict[str, str]) -> dict[str, Any]:
    value = config.get("AMBIENT_TEMP_C", "").strip()
    if value:
        try:
            return {"detected": True, "temp": int(float(value)), "source": "AMBIENT_TEMP_C"}
        except ValueError:
            pass

    temp_file = config.get("AMBIENT_TEMP_FILE", "").strip()
    if temp_file and Path(temp_file).is_file():
        raw = read_text(temp_file, "")
        try:
            parsed = int(float(raw))
            return {"detected": True, "temp": parsed // 1000 if parsed > 200 else parsed, "source": temp_file}
        except ValueError:
            pass

    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        for label_file in hwmon.glob("temp*_label"):
            label = read_text(label_file, "").lower()
            if not any(part in label for part in ("ambient", "room", "system", "motherboard", "systin")):
                continue
            raw = int(read_text(str(label_file).replace("_label", "_input"), "0") or "0")
            if raw > 0:
                return {"detected": True, "temp": raw // 1000, "source": f"{hwmon.name}:{label}"}

    return {"detected": False, "temp": None, "source": "not detected"}


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


def pause_payload(config: dict[str, str]) -> dict[str, Any]:
    now = int(time.time())
    mode = config.get("AUTO_MODE", "friendly")
    quiet = quiet_active(config.get("QUIET_HOURS_START", "22:00"), config.get("QUIET_HOURS_END", "08:00"))
    today_off = SKIP_TODAY_FILE.exists() and read_text(SKIP_TODAY_FILE, "") == time.strftime("%Y-%m-%d")
    snooze_until = int(read_text(SNOOZE_FILE, "0") or "0") if SNOOZE_FILE.exists() else 0
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


def set_config_value(key: str, value: str) -> None:
    lines = []
    found = False
    if CONF_FILE.exists():
        lines = CONF_FILE.read_text(encoding="utf-8").splitlines()
    next_lines: list[str] = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            next_lines.append(f"{key}={value}")
            found = True
        else:
            next_lines.append(line)
    if not found:
        next_lines.append(f"{key}={value}")
    CONF_FILE.write_text("\n".join(next_lines) + "\n", encoding="utf-8")


def active_service(name: str) -> str:
    result = run(["systemctl", "is-active", name], timeout=2)
    return result.stdout.strip() or "inactive"


def power_profile() -> str:
    result = run(["powerprofilesctl", "get"], timeout=2)
    return result.stdout.strip() or "unknown"


def cpu_temp_c() -> int:
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


def cpu_totals() -> tuple[int, int]:
    parts = read_text("/proc/stat", "").splitlines()[0].split()
    values = [int(value) for value in parts[1:]]
    idle = values[3] + values[4]
    return sum(values), idle


def cpu_load_percent() -> int:
    total_a, idle_a = cpu_totals()
    time.sleep(0.15)
    total_b, idle_b = cpu_totals()
    delta_total = total_b - total_a
    delta_idle = idle_b - idle_a
    if delta_total <= 0:
        return 0
    return int((delta_total - delta_idle) * 100 / delta_total)


def gpu_stats() -> dict[str, str]:
    result = run(
        [
            "nvidia-smi",
            "--query-gpu=temperature.gpu,power.draw,power.limit",
            "--format=csv,noheader,nounits",
        ],
        timeout=3,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {"temp": "0", "power": "0", "limit": "0"}
    temp, power, limit = [part.strip() for part in result.stdout.splitlines()[0].split(",")]
    return {"temp": temp, "power": power, "limit": limit}


def rapl_w(constraint: int) -> int:
    path = f"/sys/class/powercap/intel-rapl/intel-rapl:0/constraint_{constraint}_power_limit_uw"
    return int(read_text(path, "0") or "0") // 1_000_000


def history(limit: int = 80) -> list[dict[str, str]]:
    if not STATS_FILE.exists():
        return []
    rows = list(csv.DictReader(STATS_FILE.open(encoding="utf-8")))
    return rows[-limit:]


def summary(rows: list[dict[str, str]]) -> dict[str, float]:
    if not rows:
        return {"avg_cpu": 0, "avg_temp": 0, "avg_gpu": 0, "max_temp": 0, "max_cpu": 0}

    def number(row: dict[str, str], key: str) -> float:
        try:
            return float(row.get(key, "0") or "0")
        except ValueError:
            return 0

    return {
        "avg_cpu": sum(number(row, "cpu_load") for row in rows) / len(rows),
        "avg_temp": sum(number(row, "cpu_temp") for row in rows) / len(rows),
        "avg_gpu": sum(number(row, "gpu_power") for row in rows) / len(rows),
        "max_temp": max(number(row, "cpu_temp") for row in rows),
        "max_cpu": max(number(row, "cpu_load") for row in rows),
    }


def status_payload() -> dict[str, Any]:
    config = read_config()
    rows = history()
    gpu = gpu_stats()
    profile = power_profile()
    mode = config.get("AUTO_MODE", "friendly")
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
            "modes": [mode_thresholds(item, config) for item in ("calm", "summer", "friendly", "active", "quiet", "off")],
            "pause": pause,
            "ambient": ambient,
            "decision": decision_reason(mode, profile, cpu_temp, cpu_load, thresholds, pause),
        },
        "web": {"service": active_service("boost-web.service"), "url": f"http://{HOST}:{PORT}"},
        "profile": profile,
        "friendlyProfile": {"performance": "Boost", "balanced": "Balanced", "power-saver": "Maximum savings"}.get(profile, profile),
        "cpu": {"load": cpu_load, "temp": cpu_temp},
        "gpu": gpu,
        "limits": {"pl1": rapl_w(0), "pl2": rapl_w(1)},
        "system": {
            "governor": read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
            "epp": read_text("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference"),
            "turbo": "ON" if read_text("/sys/devices/system/cpu/intel_pstate/no_turbo", "1") == "0" else "OFF",
        },
        "report": {"latestExists": LATEST_REPORT.exists(), "path": str(LATEST_REPORT)},
        "summary": summary(rows),
        "history": rows[-30:],
    }


def run_action(action: str, value: str | None = None) -> dict[str, Any]:
    allowed_modes = {"calm", "summer", "friendly", "active", "quiet", "off"}
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
    elif action == "today-off":
        result = run(["/usr/local/bin/auto", "today-off"], timeout=10)
    elif action == "resume":
        result = run(["/usr/local/bin/auto", "resume"], timeout=10)
    elif action == "quiet-hours":
        payload = json.loads(value or "{}")
        start = str(payload.get("start", "22:00"))
        end = str(payload.get("end", "08:00"))
        if not valid_hhmm(start) or not valid_hhmm(end):
            return {"ok": False, "message": "Quiet hours must use HH:MM."}
        result = run(["/usr/local/bin/auto", "quiet-hours", start, end], timeout=10)
    elif action == "summer-nights" and value in {"on", "off"}:
        result = run(["/usr/local/bin/auto", "summer-nights", value], timeout=10)
    elif action == "report":
        result = run(["/usr/local/bin/power-report"], timeout=10)
    else:
        return {"ok": False, "message": "Unknown action."}

    message = (result.stdout or result.stderr).strip()
    return {"ok": result.returncode == 0, "message": message or "Done."}


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
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;600;700;800&display=swap');
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

  <div class="card gauge-card" style="animation-delay:0.2s">
    <div class="gauge-label" style="margin-bottom:12px;font-size:12px">System Config</div>
    <div class="stats-details" style="margin-top:0;border:none;padding:0;grid-template-columns:1fr 1fr;gap:12px">
      <div class="detail-item"><div class="detail-lbl">Profile</div><div class="detail-val" id="profile">—</div></div>
      <div class="detail-item"><div class="detail-lbl">Auto Mode</div><div class="detail-val" id="autoMode">—</div></div>
      <div class="detail-item"><div class="detail-lbl">Turbo</div><div class="detail-val" id="turbo">—</div></div>
      <div class="detail-item"><div class="detail-lbl">RAPL PL1/PL2</div><div class="detail-val" id="limits">—</div></div>
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
          <button class="btn primary-boost" id="btn-boost" data-action="boost">🚀 Boost <span class="kbd">1</span></button>
          <button class="btn good-save" id="btn-powersave" data-action="powersave">🍃 Powersave <span class="kbd">2</span></button>
          <button class="btn silent-mode" id="btn-silent" data-action="silent">🌙 Silent <span class="kbd">3</span></button>
          <button class="btn restore-bios" id="btn-restore" data-action="restore">♻️ Restore <span class="kbd">4</span></button>
        </div>
      </div>

      <div class="control-group">
        <div class="control-label">Auto Switching Level</div>
        <div style="color:var(--text-muted);font-size:12px;margin-bottom:12px">Summer mode lowers thermal limits for warm environments</div>
        <div class="actions">
          <button class="btn" id="mode-calm" data-action="auto-mode" data-value="calm">Calm</button>
          <button class="btn" id="mode-summer" data-action="auto-mode" data-value="summer" style="color:var(--color-warn)">☀️ Summer</button>
          <button class="btn" id="mode-friendly" data-action="auto-mode" data-value="friendly">Friendly</button>
          <button class="btn" id="mode-active" data-action="auto-mode" data-value="active">Active</button>
          <button class="btn" id="mode-quiet" data-action="auto-mode" data-value="quiet">Quiet</button>
          <button class="btn" id="mode-off" data-action="auto-mode" data-value="off" style="color:var(--color-danger)">Off</button>
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
  <div class="table-wrap"><table>
    <thead><tr><th>Time</th><th>Profile</th><th>CPU Load</th><th>CPU Temp</th><th>GPU</th><th>RAPL</th></tr></thead>
    <tbody id="history"></tbody>
  </table></div>
</section>

<footer class="footer">
  Boost Power Manager v1.2.0 — Keyboard: <kbd>1</kbd> Boost <kbd>2</kbd> Powersave <kbd>3</kbd> Silent <kbd>4</kbd> Restore <kbd>R</kbd> Refresh
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
    let pts = [], areaPts = [];
    for (let i = 0; i < n; i++) {
      const x = pL + (n > 1 ? (i/(n-1)) * cW : cW/2);
      const v = Math.min(parseFloat(data[i][key] || 0), max);
      const y = H - pB - (v/max) * cH;
      pts.push(`${x},${y}`);
      areaPts.push(`${x},${y}`);
    }
    const lineD = `M ${pts.join(' L ')}`;
    const areaD = `${lineD} L ${pL + cW},${H - pB} L ${pL},${H - pB} Z`;
    return `<path d="${areaD}" fill="url(#grad-${color})" opacity="0.15"/>
            <path d="${lineD}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
  }

  let grid = '';
  for (let p = 0; p <= 100; p += 25) {
    const y = H - pB - (p/100) * cH;
    grid += `<line x1="${pL}" y1="${y}" x2="${W-pR}" y2="${y}" stroke="rgba(255,255,255,0.04)" stroke-width="1"/>`;
    grid += `<text x="${pL-8}" y="${y+4}" fill="#475569" font-size="9" font-family="Inter,sans-serif" text-anchor="end">${p}</text>`;
  }

  // GPU power mapped to 0-200W scale shown as percentage
  let gpuData = history.map(r => ({...r, gpu_pct: String((parseFloat(r.gpu_power||0)/200)*100)}));

  svg.innerHTML = `
    <defs>
      <linearGradient id="grad-#0ea5e9" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0ea5e9"/><stop offset="1" stop-color="#0ea5e9" stop-opacity="0"/></linearGradient>
      <linearGradient id="grad-#f59e0b" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f59e0b"/><stop offset="1" stop-color="#f59e0b" stop-opacity="0"/></linearGradient>
      <linearGradient id="grad-#ec4899" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ec4899"/><stop offset="1" stop-color="#ec4899" stop-opacity="0"/></linearGradient>
    </defs>
    ${grid}
    ${makePath(history, 'cpu_load', 100, '#0ea5e9')}
    ${makePath(history, 'cpu_temp', 100, '#f59e0b')}
    ${makePath(gpuData, 'gpu_pct', 100, '#ec4899')}
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

  // System config
  $('profile').textContent = data.friendlyProfile;
  $('autoMode').textContent = data.auto.mode;
  $('limits').textContent = `${data.limits.pl1}/${data.limits.pl2} W`;
  $('turbo').textContent = data.system.turbo;

  // Pause
  const p = data.auto.pause;
  $('pauseState').textContent = p.snoozed ? '⏸ Snoozed' : p.todayOff ? '⏸ Today off' : p.quietActive ? '🌙 Quiet hours' : '✅ Available';
  $('pauseReason').textContent = p.reason;

  // Quiet hours
  $('quietStart').value = data.auto.quietStart;
  $('quietEnd').value = data.auto.quietEnd;
  $('summerNights').textContent = data.auto.summerSilentNights.toUpperCase();

  // Decision
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

  // Chart
  drawChart(data.history);

  // Active profile highlight
  ['boost','powersave','silent'].forEach(a => { const b = $(`btn-${a}`); if(b) b.classList.remove('active-preset'); });
  if (data.profile === 'performance') $('btn-boost')?.classList.add('active-preset');
  else if (data.profile === 'balanced') $('btn-powersave')?.classList.add('active-preset');
  else if (data.profile === 'power-saver') $('btn-silent')?.classList.add('active-preset');

  // Active auto mode highlight
  ['calm','summer','friendly','active','quiet','off'].forEach(m => { const b = $(`mode-${m}`); if(b) b.classList.remove('active-preset'); });
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
  $('modes').innerHTML = data.auto.modes.map(m => `
    <tr class="${data.auto.mode === m.mode ? 'active-preset' : ''}">
      <td style="font-weight:600;text-transform:capitalize">${m.mode}</td>
      <td>${m.tempHot}°C</td><td>${m.tempCritical}°C</td><td>${m.boostTempLimit}°C</td>
      <td>${m.loadHigh}% / ${secondsText(m.loadHighDuration)}</td>
      <td>${m.loadIdle}% / ${secondsText(m.loadIdleDuration)}</td>
      <td>${secondsText(m.promptCooldown)}</td>
    </tr>`).join('');

  // History table
  $('history').innerHTML = data.history.slice().reverse().map(r => `
    <tr>
      <td>${r.iso ? r.iso.split('T')[1].substring(0,8) : '—'}</td>
      <td style="text-transform:capitalize">${{performance:'Boost',balanced:'Balanced','power-saver':'Silent'}[r.profile]||r.profile}</td>
      <td><strong>${r.cpu_load||0}%</strong></td>
      <td style="color:${tempColor(parseInt(r.cpu_temp||0))}">${r.cpu_temp||0}°C</td>
      <td>${r.gpu_temp||0}°C / ${r.gpu_power||0}W</td>
      <td>${r.pl1||0}/${r.pl2||0}W</td>
    </tr>`).join('');

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
setInterval(refresh, 2000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "BoostWeb/1.0"

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
            self.send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif parsed.path == "/favicon.ico":
            self.send_bytes(b"", "image/x-icon")
        elif parsed.path == "/api/status":
            self.send_json(status_payload())
        elif parsed.path == "/report":
            if LATEST_REPORT.exists():
                self.send_bytes(LATEST_REPORT.read_bytes(), "text/html; charset=utf-8")
            else:
                self.send_bytes(b"No report yet. Click Generate report first.", "text/plain; charset=utf-8", 404)
        else:
            self.send_bytes(b"Not found", "text/plain; charset=utf-8", 404)

    def do_POST(self) -> None:
        if urllib.parse.urlparse(self.path).path != "/api/action":
            self.send_json({"ok": False, "message": "Not found"}, 404)
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
