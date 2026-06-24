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
        if name not in {"coretemp", "k10temp"}:
            continue
        for label_file in hwmon.glob("temp*_label"):
            label = read_text(label_file, "")
            if label in {"Package id 0", "Tctl", "Tdie"}:
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
<title>Boost Control</title>
<style>
:root{color-scheme:dark;--bg:#08111f;--panel:#0f1b2d;--panel2:#12223a;--text:#e7eef8;--muted:#8fa3bd;--line:#223852;--accent:#38bdf8;--ok:#2dd4bf;--warn:#fbbf24;--danger:#fb7185}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}button,input{font:inherit}
main{max-width:1180px;margin:0 auto;padding:24px}.top{display:flex;gap:16px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;margin-bottom:18px}
h1{margin:0;font-size:28px}.muted{color:var(--muted)}.status-dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--ok);margin-right:8px}.status-dot.off{background:var(--danger)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:16px 0}.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px;min-width:0}.label{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}.value{font-size:24px;font-weight:750;margin-top:5px}.value small{font-size:13px;color:var(--muted);font-weight:500}
.section{margin-top:18px}.actions{display:flex;gap:10px;flex-wrap:wrap}.btn{border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:8px;padding:10px 12px;cursor:pointer}.btn:hover,.btn:focus{outline:2px solid var(--accent);outline-offset:1px}.btn.primary{background:#0e7490;border-color:#0891b2}.btn.good{background:#0f766e;border-color:#14b8a6}.btn.warn{background:#854d0e;border-color:#f59e0b}.btn.danger{background:#9f1239;border-color:#fb7185}
.split{display:grid;grid-template-columns:minmax(0,1fr) minmax(280px,360px);gap:12px}@media(max-width:800px){.split{grid-template-columns:1fr}main{padding:16px}.value{font-size:21px}}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-size:12px;text-transform:uppercase}tr:last-child td{border-bottom:0}.table-wrap{overflow:auto;border-radius:8px}
.message{min-height:22px;color:var(--ok);margin-top:10px}.message.error{color:var(--danger)}.field-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.field-row input{width:92px;background:#071120;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:9px}.reason{border-left:3px solid var(--accent);padding-left:10px}
</style>
</head>
<body>
<main>
<div class="top">
  <div>
    <h1>Boost Control</h1>
    <div class="muted">Local controls for profiles, Auto mode, live statistics, and reports. Updates automatically.</div>
  </div>
  <div class="card">
    <div><span id="serviceDot" class="status-dot"></span><strong id="serviceText">Checking...</strong></div>
    <div class="muted" id="updatedText">-</div>
  </div>
</div>

<section class="grid" aria-label="Live status">
  <div class="card"><div class="label">Current profile</div><div class="value" id="profile">-</div></div>
  <div class="card"><div class="label">Auto mode</div><div class="value" id="autoMode">-</div></div>
  <div class="card"><div class="label">CPU</div><div class="value"><span id="cpuLoad">-</span>% <small id="cpuTemp">- C</small></div></div>
  <div class="card"><div class="label">GPU</div><div class="value"><span id="gpuPower">-</span> W <small id="gpuTemp">- C</small></div></div>
  <div class="card"><div class="label">CPU limits</div><div class="value"><span id="limits">-</span> W</div></div>
  <div class="card"><div class="label">Turbo</div><div class="value" id="turbo">-</div></div>
  <div class="card"><div class="label">Ambient</div><div class="value" id="ambient">-</div><div class="muted" id="ambientSource">-</div></div>
  <div class="card"><div class="label">Pause state</div><div class="value" id="pauseState">-</div><div class="muted" id="pauseReason">-</div></div>
</section>

<div class="split">
  <section class="section card">
    <div class="label">Manual profiles</div>
    <p class="muted">Manual choices disable Auto mode so the system does not fight your decision.</p>
    <div class="actions">
      <button class="btn primary" data-action="boost">Boost - performance</button>
      <button class="btn good" data-action="powersave">Powersave - cool and efficient</button>
    </div>
    <div class="section">
      <div class="label">Auto mode</div>
      <p class="muted">Use Summer when the room is hot and the PC needs more thermal headroom.</p>
      <div class="actions">
        <button class="btn good" data-action="auto-mode" data-value="calm">Calm</button>
        <button class="btn warn" data-action="auto-mode" data-value="summer">Summer</button>
        <button class="btn" data-action="auto-mode" data-value="friendly">Friendly</button>
        <button class="btn" data-action="auto-mode" data-value="active">Active</button>
        <button class="btn" data-action="auto-mode" data-value="quiet">Quiet</button>
        <button class="btn danger" data-action="auto-mode" data-value="off">Off</button>
      </div>
    </div>
    <div class="section">
      <div class="label">Pause</div>
      <div class="actions">
        <button class="btn" data-action="snooze" data-value="30m">30 min</button>
        <button class="btn" data-action="snooze" data-value="1h">1 hour</button>
        <button class="btn" data-action="snooze" data-value="2h">2 hours</button>
        <button class="btn" data-action="today-off">Not today</button>
        <button class="btn good" data-action="resume">Resume</button>
      </div>
    </div>
    <div class="section">
      <div class="label">Quiet hours</div>
      <div class="field-row">
        <label>Start <input id="quietStart" value="22:00" inputmode="numeric"></label>
        <label>End <input id="quietEnd" value="08:00" inputmode="numeric"></label>
        <button class="btn" id="saveQuiet">Save</button>
      </div>
    </div>
    <div class="section">
      <div class="label">Summer nights</div>
      <p class="muted">Optional link between Summer and Silent: during quiet hours, Auto can apply Silent mode without an interactive prompt.</p>
      <div class="actions">
        <button class="btn good" data-action="summer-nights" data-value="on">Enable</button>
        <button class="btn" data-action="summer-nights" data-value="off">Disable</button>
      </div>
      <p class="muted" id="summerNights">-</p>
    </div>
    <div id="message" class="message" role="status" aria-live="polite"></div>
  </section>

  <aside class="section card">
    <div class="label">Summary</div>
    <div class="grid" style="grid-template-columns:1fr 1fr">
      <div><div class="muted">Average CPU</div><strong id="avgCpu">-</strong></div>
      <div><div class="muted">Max temp</div><strong id="maxTemp">-</strong></div>
      <div><div class="muted">Average GPU</div><strong id="avgGpu">-</strong></div>
      <div><div class="muted">EPP</div><strong id="epp">-</strong></div>
    </div>
    <div class="actions">
      <button class="btn" data-action="report">Generate report</button>
      <a class="btn" href="/report" target="_blank" rel="noreferrer">Open latest</a>
    </div>
    <p class="muted" id="reportPath">-</p>
  </aside>
</div>

<section class="section card">
  <div class="label">Auto decision</div>
  <p class="reason" id="decisionReason">-</p>
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr))">
    <div><div class="muted">Warm CPU</div><strong id="tempHot">-</strong></div>
    <div><div class="muted">Critical CPU</div><strong id="tempCritical">-</strong></div>
    <div><div class="muted">Boost allowed below</div><strong id="boostLimit">-</strong></div>
    <div><div class="muted">Busy trigger</div><strong id="busyTrigger">-</strong></div>
    <div><div class="muted">Idle trigger</div><strong id="idleTrigger">-</strong></div>
    <div><div class="muted">Prompt cooldown</div><strong id="cooldown">-</strong></div>
  </div>
</section>

<section class="section">
  <h2>Auto mode presets</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Mode</th><th>Warm</th><th>Critical</th><th>Boost below</th><th>Busy</th><th>Idle</th><th>Cooldown</th></tr></thead>
      <tbody id="modes"></tbody>
    </table>
  </div>
</section>

<section class="section">
  <h2>Recent history</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Time</th><th>Profile</th><th>CPU</th><th>CPU temp</th><th>GPU</th><th>Limits</th></tr></thead>
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

async function fetchStatus() {
  const response = await fetch('/api/status', { cache: 'no-store' })
  if (!response.ok) throw new Error('Cannot read status')
  return response.json()
}

function setMessage(text, isError = false) {
  message.textContent = text
  message.className = isError ? 'message error' : 'message'
}

function render(data) {
  $('serviceDot').className = data.auto.service === 'active' ? 'status-dot' : 'status-dot off'
  $('serviceText').textContent = `Auto: ${data.auto.service} | Web: ${data.web.service}`
  $('updatedText').textContent = `Updated: ${data.time}`
  $('profile').textContent = data.friendlyProfile
  $('autoMode').textContent = data.auto.mode
  $('cpuLoad').textContent = data.cpu.load
  $('cpuTemp').textContent = `${data.cpu.temp} C`
  $('gpuPower').textContent = data.gpu.power
  $('gpuTemp').textContent = `${data.gpu.temp} C`
  $('limits').textContent = `${data.limits.pl1}/${data.limits.pl2}`
  $('turbo').textContent = data.system.turbo
  $('ambient').textContent = data.auto.ambient.detected ? `${data.auto.ambient.temp} C` : 'Not detected'
  $('ambientSource').textContent = data.auto.ambient.source
  $('pauseState').textContent = data.auto.pause.snoozed ? 'Snoozed' : data.auto.pause.todayOff ? 'Today off' : data.auto.pause.quietActive ? 'Quiet hours' : 'Available'
  $('pauseReason').textContent = data.auto.pause.reason
  $('quietStart').value = data.auto.quietStart
  $('quietEnd').value = data.auto.quietEnd
  $('summerNights').textContent = `Current: ${data.auto.summerSilentNights}`
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
  $('epp').textContent = data.system.epp
  $('reportPath').textContent = data.report.latestExists ? data.report.path : 'No report yet'
  $('modes').innerHTML = data.auto.modes.map(mode => `
    <tr>
      <td>${mode.mode}</td><td>${mode.tempHot} C</td><td>${mode.tempCritical} C</td>
      <td>${mode.boostTempLimit} C</td><td>${mode.loadHigh}% / ${secondsText(mode.loadHighDuration)}</td>
      <td>${mode.loadIdle}% / ${secondsText(mode.loadIdleDuration)}</td><td>${secondsText(mode.promptCooldown)}</td>
    </tr>`).join('')
  $('history').innerHTML = data.history.slice().reverse().map(row => `
    <tr>
      <td>${row.iso || '-'}</td><td>${row.profile || '-'}</td><td>${row.cpu_load || 0}%</td>
      <td>${row.cpu_temp || 0} C</td><td>${row.gpu_temp || 0} C / ${row.gpu_power || 0} W</td>
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
  setMessage('Working...')
  const response = await fetch('/api/action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, value })
  })
  const result = await response.json()
  setMessage(result.message || (result.ok ? 'Done' : 'Error'), !result.ok)
  await refresh()
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
