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
<title>Boost Control Dashboard</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=Outfit:wght@400;600;700;800&display=swap');
:root{color-scheme:dark;--bg-gradient:radial-gradient(circle at 10% 20%, rgba(12,20,39,1) 0%, rgba(5,9,18,1) 90%);--panel-bg:rgba(13,24,45,0.6);--panel-border:rgba(255,255,255,0.07);--text-main:#f1f5f9;--text-muted:#94a3b8;--accent:#0ea5e9;--accent-glow:rgba(14,165,233,0.35);--color-boost:#f43f5e;--color-boost-glow:rgba(244,63,94,0.4);--color-powersave:#10b981;--color-powersave-glow:rgba(16,185,129,0.4);--color-silent:#8b5cf6;--color-silent-glow:rgba(139,92,246,0.4);--color-restore:#6b7280;--color-warn:#f59e0b;--color-danger:#ef4444;--color-ok:#10b981;--font-title:'Outfit',-apple-system,BlinkMacSystemFont,sans-serif;--font-body:'Inter',-apple-system,BlinkMacSystemFont,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg-gradient);background-attachment:fixed;color:var(--text-main);font-family:var(--font-body);line-height:1.5;-webkit-font-smoothing:antialiased}button,input{font:inherit}
main{max-width:1200px;margin:0 auto;padding:40px 24px}.top{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:24px;margin-bottom:32px}
h1{font-family:var(--font-title);font-weight:800;font-size:36px;margin:0;background:linear-gradient(to right,#38bdf8,#818cf8,#f43f5e);-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-0.02em}
.subtitle{color:var(--text-muted);margin-top:4px;font-size:15px}
.card{background:var(--panel-bg);border:1px solid var(--panel-border);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border-radius:16px;padding:24px;box-shadow:0 10px 30px rgba(0,0,0,0.25);transition:transform 0.3s ease,box-shadow 0.3s ease}
.card:hover{box-shadow:0 15px 35px rgba(0,0,0,0.3)}.status-dot{width:10px;height:10px;border-radius:50%;background:var(--color-powersave);box-shadow:0 0 12px var(--color-powersave);margin-right:8px;display:inline-block;animation:pulse 2s infinite}
.status-dot.off{background:var(--color-boost);box-shadow:0 0 12px var(--color-boost)}
@keyframes pulse{0%{transform:scale(0.95);opacity:0.8}50%{transform:scale(1.1);opacity:1}100%{transform:scale(0.95);opacity:0.8}}
.gauges-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px;margin-bottom:32px}
.gauge-card{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:20px}
.gauge-wrapper{position:relative;width:120px;height:120px;margin-bottom:12px}
.gauge-svg{transform:rotate(-90deg);width:120px;height:120px}
.gauge-bg{fill:none;stroke:rgba(255,255,255,0.05);stroke-width:8}
.gauge-fill{fill:none;stroke:var(--accent);stroke-width:8;stroke-linecap:round;transition:stroke-dashoffset 0.6s cubic-bezier(0.4,0,0.2,1);filter:drop-shadow(0 0 6px var(--accent-glow))}
.gauge-value{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-family:var(--font-title);font-weight:700;font-size:22px}
.gauge-value small{font-size:12px;font-weight:500;color:var(--text-muted)}
.gauge-label{color:var(--text-muted);font-size:12px;text-transform:uppercase;letter-spacing:0.06em;font-weight:600}
.stats-details{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:16px;width:100%;margin-top:16px;border-top:1px solid rgba(255,255,255,0.05);padding-top:16px}
.detail-item{display:flex;flex-direction:column}.detail-lbl{font-size:11px;text-transform:uppercase;color:var(--text-muted);letter-spacing:0.04em}
.detail-val{font-size:15px;font-weight:600;margin-top:4px}
.split{display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,380px);gap:24px}
@media(max-width:900px){.split{grid-template-columns:1fr}}
.section-title{font-family:var(--font-title);font-weight:700;font-size:20px;margin:0 0 16px;display:flex;align-items:center;gap:8px}
.control-group{margin-bottom:24px}.control-group:last-child{margin-bottom:0}
.control-label{font-size:12px;text-transform:uppercase;letter-spacing:0.06em;color:var(--text-muted);margin-bottom:8px;font-weight:600}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px}
.btn{border:1px solid rgba(255,255,255,0.1);background:rgba(255,255,255,0.03);color:var(--text-main);border-radius:10px;padding:10px 16px;font-weight:500;cursor:pointer;transition:all 0.2s ease;font-size:14px}
.btn:hover{background:rgba(255,255,255,0.08);border-color:rgba(255,255,255,0.2);transform:translateY(-1px)}
.btn:active{transform:translateY(1px)}
.btn.primary-boost{background:var(--color-boost);border-color:transparent;box-shadow:0 4px 12px var(--color-boost-glow)}
.btn.primary-boost:hover{background:#ff5277;box-shadow:0 6px 16px var(--color-boost-glow)}
.btn.good-save{background:var(--color-powersave);border-color:transparent;box-shadow:0 4px 12px var(--color-powersave-glow)}
.btn.good-save:hover{background:#14d496;box-shadow:0 6px 16px var(--color-powersave-glow)}
.btn.silent-mode{background:var(--color-silent);border-color:transparent;box-shadow:0 4px 12px var(--color-silent-glow)}
.btn.silent-mode:hover{background:#a78bfa;box-shadow:0 6px 16px var(--color-silent-glow)}
.btn.restore-bios{background:rgba(30,41,59,0.8);border-color:rgba(75,85,99,0.3)}
.btn.restore-bios:hover{background:rgba(51,65,85,0.8)}
.btn.active-preset{border-color:var(--accent);box-shadow:0 0 10px var(--accent-glow);background:rgba(14,165,233,0.15)}
.field-row{display:flex;gap:12px;align-items:center;flex-wrap:wrap;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05);padding:12px;border-radius:12px}
.field-row label{display:flex;flex-direction:column;gap:4px;font-size:11px;color:var(--text-muted);text-transform:uppercase}
.field-row input{width:90px;background:#090f1d;color:var(--text-main);border:1px solid rgba(255,255,255,0.1);border-radius:8px;padding:8px;text-align:center;transition:border-color 0.2s ease}
.field-row input:focus{outline:none;border-color:var(--accent)}
.message{min-height:24px;color:var(--color-ok);margin-top:14px;font-weight:500;font-size:13px;display:flex;align-items:center;gap:6px}
.message.error{color:var(--color-danger)}
.reason{border-left:4px solid var(--accent);background:rgba(14,165,233,0.04);padding:12px 16px;border-radius:0 12px 12px 0;margin:0 0 16px;font-size:14px}
.chart-container{background:rgba(13,24,45,0.4);border:1px solid rgba(255,255,255,0.05);border-radius:12px;padding:16px;margin-top:12px}
.chart-svg{width:100%;height:180px;display:block}
.chart-legend{display:flex;gap:16px;justify-content:center;margin-top:8px;font-size:12px}
.legend-item{display:flex;align-items:center;gap:6px}
.legend-dot{width:10px;height:10px;border-radius:2px}
.table-wrap{overflow-x:auto;border-radius:12px;border:1px solid rgba(255,255,255,0.05);margin-top:12px}
table{width:100%;border-collapse:collapse;background:rgba(15,23,42,0.3)}
th,td{text-align:left;padding:12px 16px;border-bottom:1px solid rgba(255,255,255,0.05);white-space:nowrap}
th{color:var(--text-muted);font-size:11px;text-transform:uppercase;letter-spacing:0.06em;background:rgba(255,255,255,0.02)}
tr:last-child td{border-bottom:0}tr:hover td{background:rgba(255,255,255,0.01)}
</style>
</head>
<body>
<main>
<div class="top">
  <div>
    <h1>Boost Control Panel</h1>
    <div class="subtitle">Linux power profile manager dashboard for Intel/AMD + NVIDIA desktops</div>
  </div>
  <div class="card" style="padding: 14px 20px;">
    <div style="display:flex; align-items:center;"><span id="serviceDot" class="status-dot"></span><strong id="serviceText">Checking...</strong></div>
    <div class="subtitle" style="font-size:12px; text-align:right;" id="updatedText">-</div>
  </div>
</div>

<section class="gauges-grid" aria-label="Live status">
  <div class="card gauge-card">
    <div class="gauge-wrapper">
      <svg class="gauge-svg" viewBox="0 0 120 120">
        <circle class="gauge-bg" cx="60" cy="60" r="50" />
        <circle id="cpuLoadCircle" class="gauge-fill" cx="60" cy="60" r="50" style="stroke: #0ea5e9; filter: drop-shadow(0 0 4px rgba(14,165,233,0.4));" />
      </svg>
      <div class="gauge-value"><span id="cpuLoadText">-</span><small>%</small></div>
    </div>
    <div class="gauge-label">CPU Load</div>
  </div>
  
  <div class="card gauge-card">
    <div class="gauge-wrapper">
      <svg class="gauge-svg" viewBox="0 0 120 120">
        <circle class="gauge-bg" cx="60" cy="60" r="50" />
        <circle id="cpuTempCircle" class="gauge-fill" cx="60" cy="60" r="50" style="stroke: #f59e0b; filter: drop-shadow(0 0 4px rgba(245,158,11,0.4));" />
      </svg>
      <div class="gauge-value"><span id="cpuTempText">-</span><small>°C</small></div>
    </div>
    <div class="gauge-label">CPU Temperature</div>
  </div>

  <div class="card" style="display:flex; flex-direction:column; justify-content:center;">
    <div class="gauge-label" style="margin-bottom:8px;">GPU Metrics</div>
    <div class="detail-val" style="font-size:22px; font-weight:700;"><span id="gpuPowerText">-</span> W <span style="font-size:14px; color:var(--text-muted); font-weight:500;">/ <span id="gpuLimitText">-</span> W limit</span></div>
    <div class="subtitle" style="font-size:14px; margin-top:2px;">GPU Temp: <strong id="gpuTempText" style="color:var(--text-main);">-</strong></div>
  </div>

  <div class="card" style="display:flex; flex-direction:column; justify-content:center;">
    <div class="gauge-label" style="margin-bottom:6px;">Current Configuration</div>
    <div class="stats-details" style="margin-top:0; border:none; padding:0; grid-template-columns:1fr 1fr;">
      <div class="detail-item">
        <div class="detail-lbl">Active Profile</div>
        <div class="detail-val" id="profile">-</div>
      </div>
      <div class="detail-item">
        <div class="detail-lbl">Auto Mode</div>
        <div class="detail-val" id="autoMode">-</div>
      </div>
      <div class="detail-item" style="margin-top:8px;">
        <div class="detail-lbl">Turbo Boost</div>
        <div class="detail-val" id="turbo">-</div>
      </div>
      <div class="detail-item" style="margin-top:8px;">
        <div class="detail-lbl">RAPL PL1/PL2</div>
        <div class="detail-val" id="limits">-</div>
      </div>
    </div>
  </div>
</section>

<div class="split">
  <div style="display:flex; flex-direction:column; gap:24px;">
    <section class="card">
      <div class="section-title">⚡ Power Profiles & Auto Modes</div>
      
      <div class="control-group">
        <div class="control-label">Manual Mode Profile Override</div>
        <div class="subtitle" style="margin-bottom:12px;">Selecting a manual profile disables Auto mode, ensuring they do not conflict.</div>
        <div class="actions">
          <button class="btn primary-boost" id="btn-boost" data-action="boost">Boost</button>
          <button class="btn good-save" id="btn-powersave" data-action="powersave">Powersave</button>
          <button class="btn silent-mode" id="btn-silent" data-action="silent">Silent (Overnight)</button>
          <button class="btn restore-bios" id="btn-restore" data-action="restore">Restore BIOS Defaults</button>
        </div>
      </div>

      <div class="control-group">
        <div class="control-label">Auto Switching Level</div>
        <div class="subtitle" style="margin-bottom:12px;">Choose Summer mode in warm rooms to lower thermal limits and reduce noise.</div>
        <div class="actions">
          <button class="btn" id="mode-calm" data-action="auto-mode" data-value="calm">Calm</button>
          <button class="btn" id="mode-summer" data-action="auto-mode" data-value="summer" style="color:var(--color-warn);">Summer</button>
          <button class="btn" id="mode-friendly" data-action="auto-mode" data-value="friendly">Friendly</button>
          <button class="btn" id="mode-active" data-action="auto-mode" data-value="active">Active</button>
          <button class="btn" id="mode-quiet" data-action="auto-mode" data-value="quiet">Quiet</button>
          <button class="btn danger" id="mode-off" data-action="auto-mode" data-value="off">Off</button>
        </div>
      </div>

      <div id="message" class="message" role="status" aria-live="polite"></div>
    </section>

    <section class="card">
      <div class="section-title">📊 Live Power Telemetry (Last 30 samples)</div>
      <div class="chart-container">
        <svg id="historyChart" class="chart-svg" viewBox="0 0 1000 180" preserveAspectRatio="none">
          <text x="50%" y="50%" text-anchor="middle" fill="#94a3b8">Collecting history data...</text>
        </svg>
        <div class="chart-legend">
          <div class="legend-item"><span class="legend-dot" style="background:#0ea5e9;"></span><span>CPU Load (%)</span></div>
          <div class="legend-item"><span class="legend-dot" style="background:#f59e0b;"></span><span>CPU Temp (°C)</span></div>
          <div class="legend-item"><span class="legend-dot" style="background:#ec4899;"></span><span>GPU Power (W / 200)</span></div>
        </div>
      </div>
    </section>
  </div>

  <aside style="display:flex; flex-direction:column; gap:24px;">
    <section class="card">
      <div class="section-title">⏳ Auto Pause & Snooze</div>
      <div class="control-group">
        <div class="control-label">Snooze suggestions for:</div>
        <div class="actions" style="margin-bottom:16px;">
          <button class="btn" data-action="snooze" data-value="30m">30m</button>
          <button class="btn" data-action="snooze" data-value="1h">1h</button>
          <button class="btn" data-action="snooze" data-value="2h">2h</button>
          <button class="btn" data-action="today-off">All Today</button>
        </div>
        <div class="actions">
          <button class="btn good-save" style="width:100%; text-align:center;" data-action="resume">Resume Auto Mode</button>
        </div>
      </div>

      <div class="stats-details" style="margin-top:16px;">
        <div class="detail-item">
          <div class="detail-lbl">Status</div>
          <div class="detail-val" id="pauseState">-</div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Reason</div>
          <div class="detail-val" style="font-size:13px; color:var(--text-muted);" id="pauseReason">-</div>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="section-title">🌙 Quiet Hours & Summer Nights</div>
      <div class="control-group">
        <div class="control-label">Quiet hours schedule</div>
        <div class="subtitle" style="margin-bottom:12px;">No prompts will be shown during quiet hours.</div>
        <div class="field-row">
          <label>Start <input id="quietStart" value="22:00" placeholder="HH:MM"></label>
          <label>End <input id="quietEnd" value="08:00" placeholder="HH:MM"></label>
          <button class="btn active-preset" id="saveQuiet" style="margin-top: 14px; width: 100%;">Save</button>
        </div>
      </div>

      <div class="control-group" style="margin-top:20px;">
        <div class="control-label">Summer Nights Switch</div>
        <div class="subtitle" style="margin-bottom:12px;">Allows Summer mode to automatically enable Silent mode overnight without prompting.</div>
        <div class="actions">
          <button class="btn good-save" id="summer-nights-on" data-action="summer-nights" data-value="on">Enable</button>
          <button class="btn" id="summer-nights-off" data-action="summer-nights" data-value="off">Disable</button>
        </div>
        <div class="subtitle" style="font-size:12px; margin-top:8px;">Current State: <strong id="summerNights" style="color:var(--text-main);">-</strong></div>
      </div>
    </section>

    <section class="card">
      <div class="section-title">📋 Reports & History</div>
      <div class="gauge-label" style="margin-bottom:8px;">Power averages</div>
      <div class="stats-details" style="margin-top:0; border:none; padding:0; grid-template-columns:1fr 1fr; gap:12px;">
        <div class="detail-item">
          <div class="detail-lbl">Average CPU</div>
          <div class="detail-val" id="avgCpu">-</div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Max CPU Temp</div>
          <div class="detail-val" id="maxTemp">-</div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Average GPU</div>
          <div class="detail-val" id="avgGpu">-</div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Governor EPP</div>
          <div class="detail-val" id="epp">-</div>
        </div>
      </div>
      <div class="actions" style="margin-top:20px; width:100%; flex-direction:column; gap:8px;">
        <button class="btn" style="width:100%;" data-action="report">Generate Full HTML Report</button>
        <a class="btn" style="width:100%; text-align:center; text-decoration:none;" href="/report" target="_blank" rel="noreferrer">Open Latest Report ↗</a>
      </div>
      <p class="subtitle" style="font-size:11px; word-break:break-all;" id="reportPath">-</p>
    </section>
  </aside>
</div>

<section class="card" style="margin-top:24px;">
  <div class="section-title">🧠 Auto Switch Decision Reason</div>
  <p class="reason" id="decisionReason">-</p>
  <div class="stats-details" style="grid-template-columns:repeat(auto-fit, minmax(130px, 1fr)); border-top:none; padding-top:0;">
    <div class="detail-item">
      <div class="detail-lbl">Warm Threshold</div>
      <div class="detail-val" id="tempHot">-</div>
    </div>
    <div class="detail-item">
      <div class="detail-lbl">Critical Temp</div>
      <div class="detail-val" id="tempCritical">-</div>
    </div>
    <div class="detail-item">
      <div class="detail-lbl">Boost Below</div>
      <div class="detail-val" id="boostLimit">-</div>
    </div>
    <div class="detail-item">
      <div class="detail-lbl">Busy Trigger</div>
      <div class="detail-val" id="busyTrigger">-</div>
    </div>
    <div class="detail-item">
      <div class="detail-lbl">Idle Trigger</div>
      <div class="detail-val" id="idleTrigger">-</div>
    </div>
    <div class="detail-item">
      <div class="detail-lbl">Cooldown</div>
      <div class="detail-val" id="cooldown">-</div>
    </div>
  </div>
</section>

<section class="section" style="margin-top:32px;">
  <h2 style="font-family:var(--font-title); font-size:24px; font-weight:700;">Preset Threshold Reference</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Mode</th><th>Warm Limit</th><th>Critical Limit</th><th>Boost Allowed Below</th><th>Busy Trigger</th><th>Idle Trigger</th><th>Prompt Cooldown</th></tr></thead>
      <tbody id="modes"></tbody>
    </table>
  </div>
</section>

<section class="section" style="margin-top:32px; margin-bottom:40px;">
  <h2 style="font-family:var(--font-title); font-size:24px; font-weight:700;">Live Sensor History</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Time</th><th>Applied Profile</th><th>CPU Load</th><th>CPU Temp</th><th>GPU Load & Power</th><th>RAPL Limits</th></tr></thead>
      <tbody id="history"></tbody>
    </table>
  </div>
</section>
</main>

<script>
const $ = (id) => document.getElementById(id)
const message = $('message')

function secondsText(seconds) {
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
  if (seconds >= 60) return `${Math.floor(seconds / 60)}m`
  return `${seconds}s`
}

function setMessage(text, isError = false) {
  message.textContent = text
  message.className = isError ? 'message error' : 'message'
  setTimeout(() => { message.textContent = '' }, 4000)
}

function setGauge(id, value, max = 100) {
  const circle = document.getElementById(`${id}Circle`);
  const text = document.getElementById(`${id}Text`);
  if (!circle || !text) return;
  
  const radius = 50;
  const circumference = 2 * Math.PI * radius;
  
  circle.style.strokeDasharray = circumference;
  const pct = Math.min(Math.max(value, 0), max) / max;
  const offset = circumference - (pct * circumference);
  circle.style.strokeDashoffset = offset;
  text.textContent = Math.round(value);
}

function drawHistoryChart(history) {
  const svg = document.getElementById('historyChart');
  if (!history || history.length === 0) {
    svg.innerHTML = '<text x="50%" y="50%" text-anchor="middle" fill="#94a3b8">No history data available yet</text>';
    return;
  }
  
  const width = 1000;
  const height = 180;
  const paddingLeft = 40;
  const paddingRight = 20;
  const paddingTop = 20;
  const paddingBottom = 25;
  
  const chartWidth = width - paddingLeft - paddingRight;
  const chartHeight = height - paddingTop - paddingBottom;
  
  const pointsCount = history.length;
  let loadPoints = [];
  let tempPoints = [];
  let gpuPoints = [];
  
  for (let i = 0; i < pointsCount; i++) {
    const x = paddingLeft + (i / (pointsCount - 1)) * chartWidth;
    const loadVal = parseFloat(history[i].cpu_load || 0);
    const tempVal = parseFloat(history[i].cpu_temp || 0);
    const gpuVal = parseFloat(history[i].gpu_power || 0);
    const gpuPct = (gpuVal / 200) * 100;
    
    const loadY = height - paddingBottom - (loadVal / 100) * chartHeight;
    const tempY = height - paddingBottom - (tempVal / 100) * chartHeight;
    const gpuY = height - paddingBottom - (Math.min(gpuPct, 100) / 100) * chartHeight;
    
    loadPoints.push(`${x},${loadY}`);
    tempPoints.push(`${x},${tempY}`);
    gpuPoints.push(`${x},${gpuY}`);
  }
  
  let gridLines = '';
  for (let pct = 0; pct <= 100; pct += 25) {
    const y = height - paddingBottom - (pct / 100) * chartHeight;
    gridLines += `<line x1="${paddingLeft}" y1="${y}" x2="${width - paddingRight}" y2="${y}" stroke="rgba(255,255,255,0.06)" stroke-width="1"/>`;
    gridLines += `<text x="${paddingLeft - 10}" y="${y + 4}" fill="#64748b" font-size="10" font-family="sans-serif" text-anchor="end">${pct}%</text>`;
  }
  
  const loadPath = `<path d="M ${loadPoints.join(' L ')}" fill="none" stroke="#0ea5e9" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />`;
  const tempPath = `<path d="M ${tempPoints.join(' L ')}" fill="none" stroke="#f59e0b" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />`;
  const gpuPath = `<path d="M ${gpuPoints.join(' L ')}" fill="none" stroke="#ec4899" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />`;
  
  svg.innerHTML = `
    <g class="grid-lines">${gridLines}</g>
    <g class="chart-paths">
      ${loadPath}
      ${tempPath}
      ${gpuPath}
    </g>
  `;
}

function render(data) {
  $('serviceDot').className = data.auto.service === 'active' ? 'status-dot' : 'status-dot off'
  $('serviceText').textContent = `Daemon Auto: ${data.auto.service} | Web Dashboard: ${data.web.service}`
  $('updatedText').textContent = `Telemetry Live Status • Refreshed: ${data.time}`
  $('profile').textContent = data.friendlyProfile
  $('autoMode').textContent = data.auto.mode
  
  // Update circular gauges
  setGauge('cpuLoad', data.cpu.load);
  setGauge('cpuTemp', data.cpu.temp);
  
  $('gpuPowerText').textContent = data.gpu.power
  $('gpuLimitText').textContent = data.gpu.limit
  $('gpuTempText').textContent = `${data.gpu.temp} C`
  
  $('limits').textContent = `${data.limits.pl1}/${data.limits.pl2} W`
  $('turbo').textContent = data.system.turbo
  $('pauseState').textContent = data.auto.pause.snoozed ? 'Snoozed' : data.auto.pause.todayOff ? 'Today off' : data.auto.pause.quietActive ? 'Quiet hours' : 'Available'
  $('pauseReason').textContent = data.auto.pause.reason
  $('quietStart').value = data.auto.quietStart
  $('quietEnd').value = data.auto.quietEnd
  $('summerNights').textContent = data.auto.summerSilentNights.toUpperCase()
  $('decisionReason').textContent = data.auto.decision
  $('tempHot').textContent = `${data.auto.thresholds.tempHot} C`
  $('tempCritical').textContent = `${data.auto.thresholds.tempCritical} C`
  $('boostLimit').textContent = `${data.auto.thresholds.boostTempLimit} C`
  $('busyTrigger').textContent = `${data.auto.thresholds.loadHigh}% for ${secondsText(data.auto.thresholds.loadHighDuration)}`
  $('idleTrigger').textContent = `${data.auto.thresholds.loadIdle}% for ${secondsText(data.auto.thresholds.loadIdleDuration)}`
  $('cooldown').textContent = secondsText(data.auto.thresholds.promptCooldown)
  $('avgCpu').textContent = `${Math.round(data.summary.avg_cpu)}%`
  $('maxTemp').textContent = `${Math.round(data.summary.max_temp)} C`
  $('avgGpu').textContent = `${Number(data.summary.avg_gpu).toFixed(1)} W`
  $('epp').textContent = `${data.system.governor} (${data.system.epp})`
  $('reportPath').textContent = data.report.latestExists ? 'Saved to: ' + data.report.path : 'No HTML report generated yet.'
  
  // Render history chart
  drawHistoryChart(data.history);
  
  // Highlight active profile buttons
  ['boost', 'powersave', 'silent'].forEach(act => {
    const btn = document.getElementById(`btn-${act}`);
    if (btn) btn.classList.remove('active-preset');
  });
  if (data.profile === 'performance') $('btn-boost').classList.add('active-preset');
  else if (data.profile === 'balanced') $('btn-powersave').classList.add('active-preset');
  else if (data.profile === 'power-saver') $('btn-silent').classList.add('active-preset');
  
  // Highlight active auto mode buttons
  ['calm', 'summer', 'friendly', 'active', 'quiet', 'off'].forEach(m => {
    const btn = document.getElementById(`mode-${m}`);
    if (btn) btn.classList.remove('active-preset');
  });
  const activeModeBtn = document.getElementById(`mode-${data.auto.mode}`);
  if (activeModeBtn) activeModeBtn.classList.add('active-preset');

  // Highlight Summer Nights
  if (data.auto.summerSilentNights === 'yes') {
    $('summer-nights-on').classList.add('active-preset');
    $('summer-nights-off').classList.remove('active-preset');
  } else {
    $('summer-nights-on').classList.remove('active-preset');
    $('summer-nights-off').classList.add('active-preset');
  }

  $('modes').innerHTML = data.auto.modes.map(mode => `
    <tr class="${data.auto.mode === mode.mode ? 'active-preset' : ''}">
      <td style="font-weight:600; text-transform:capitalize;">${mode.mode}</td>
      <td>${mode.tempHot} C</td>
      <td>${mode.tempCritical} C</td>
      <td>${mode.boostTempLimit} C</td>
      <td>${mode.loadHigh}% / ${secondsText(mode.loadHighDuration)}</td>
      <td>${mode.loadIdle}% / ${secondsText(mode.loadIdleDuration)}</td>
      <td>${secondsText(mode.promptCooldown)}</td>
    </tr>`).join('')
    
  $('history').innerHTML = data.history.slice().reverse().map(row => `
    <tr>
      <td>${row.iso ? row.iso.split('T')[1].substring(0,8) : '-'}</td>
      <td style="text-transform:capitalize;">${row.profile === 'performance' ? 'Boost' : row.profile === 'balanced' ? 'Powersave' : row.profile === 'power-saver' ? 'Silent' : row.profile}</td>
      <td><span style="display:inline-block; width:45px; font-weight:600;">${row.cpu_load || 0}%</span></td>
      <td>${row.cpu_temp || 0} C</td>
      <td>${row.gpu_temp || 0} C / ${row.gpu_power || 0} W</td>
      <td>${row.pl1 || 0}/${row.pl2 || 0} W</td>
    </tr>`).join('')
}

async function refresh() {
  try {
    render(await fetchStatus())
  } catch (error) {
    setMessage(error.message, true)
  }
}

async function sendAction(action, value = null) {
  try {
    const response = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, value })
    })
    const result = await response.json()
    setMessage(result.message || (result.ok ? 'Action Applied' : 'Execution Error'), !result.ok)
    await refresh()
  } catch (e) {
    setMessage(e.message, true)
  }
}

document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-action]')
  if (!button) return
  event.preventDefault()
  sendAction(button.dataset.action, button.dataset.value || null)
})

$('saveQuiet').addEventListener('click', () => {
  sendAction('quiet-hours', JSON.stringify({ start: $('quietStart').value, end: $('quietEnd').value }))
})

refresh()
setInterval(refresh, 2000)
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
