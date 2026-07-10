# Boost Architecture

## Overview

Boost is a Linux power management tool composed of four layers that communicate
through the filesystem and systemd. There is no IPC bus between components —
everything is orchestrated via config files, state files, and systemd service
activation.

```
┌─────────────────────────────────────────────────────────────────┐
│                        User Interaction                         │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────────┐  │
│  │  boost   │  │powersave │  │  silent  │  │     auto       │  │
│  │ (CLI)   │  │  (CLI)   │  │  (CLI)   │  │  (CLI + daemon)│  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └───────┬────────┘  │
│       │              │             │                │           │
│       └──────────────┴─────────────┴────────────────┘           │
│                              │                                  │
│                     ┌────────▼────────┐                         │
│                     │  power-common.sh │                        │
│                     │  (shared lib)    │                        │
│                     └────────┬────────┘                         │
│                              │                                  │
│                     ┌────────▼────────┐                         │
│                     │   sysfs writes   │                         │
│                     │   (kernel hw)    │                        │
│                     └─────────────────┘                         │
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐   │
│  │ boost-web.py  │    │boost-daemon │    │  boost-tray.py   │   │
│  │ (web server)  │◄──►│  .py        │    │  (GTK tray)      │   │
│  └──────┬───────┘    │  (auto)      │    └────────┬─────────┘   │
│         │            └──────┬───────┘             │             │
│         │                   │                     │             │
│         └───────────────────┴─────────────────────┘             │
│                             │                                   │
│                    ┌────────▼────────┐                          │
│                    │  /etc/boost-    │                          │
│                    │  auto.conf      │                          │
│                    │  /var/lib/      │                          │
│                    │  power-profile/ │                          │
│                    └─────────────────┘                          │
└─────────────────────────────────────────────────────────────────┘
```

## Component Details

### 1. CLI Profile Commands (`bin/boost`, `bin/powersave`, `bin/silent`, `bin/restore`)

**Language:** Bash  
**Lines:** ~35–92 each  
**Purpose:** Apply a fixed power profile immediately.

Each script:
1. Sources `power-common.sh` for shared helpers.
2. Calls `check_root` to auto-elevate via `sudo`.
3. Calls `disable_auto_for_manual_profile` to stop the auto daemon.
4. Calls `save_originals` (once) to capture boot-time state.
5. Applies CPU governor/EPP via `set_cpu_profile` (uses `powerprofilesctl` if available, else direct sysfs).
6. Sets turbo on/off via `set_turbo`.
7. Calls `apply_hardware_limits` to scale RAPL (Intel) and GPU power limits (NVIDIA/AMD).
8. Sets I/O scheduler via `set_io_schedulers`.
9. Configures transparent hugepages.
10. Restores or applies fan curve.
11. Resets process priorities.
12. Calls `show_status` to print a summary.

**Key filesystem interactions:**
- `/sys/devices/system/cpu/cpu*/cpufreq/` — governor, EPP
- `/sys/class/powercap/intel-rapl/` — RAPL limits
- `/sys/class/hwmon/` — temperature, fan control
- `/sys/class/drm/` — AMD GPU power limits
- `nvidia-smi` — NVIDIA GPU power limits
- `/sys/kernel/mm/transparent_hugepage/enabled`
- `/sys/block/*/queue/scheduler`

### 2. Auto Daemon (`lib/boost-daemon.py`)

**Language:** Python 3  
**Lines:** 545  
**Purpose:** Background daemon that monitors temperature, CPU load, and running
processes, then suggests or automatically applies profile changes.

**Lifecycle:**
- Started by `boost-auto.service` (systemd).
- Runs as root.
- Polls every 5 seconds (configurable via `POLL_INTERVAL`).
- Records stats every 60 seconds to CSV.

**Detection logic (in order of priority):**
1. **Game detection** — `pgrep` for known game processes → auto-switches to Boost.
2. **Creator workload** — `pgrep` for ffmpeg/blender/cargo etc. → suggests Boost.
3. **Meeting detection** — `pgrep` for zoom/teams/discord → suggests Quiet.
4. **Critical heat** — if temp ≥ `TEMP_CRITICAL` and profile is Performance → emergency Powersave.
5. **Hot warning** — if temp ≥ `TEMP_HOT` and profile is Performance → suggests cooldown.
6. **High load** — if load ≥ `LOAD_HIGH` for `LOAD_HIGH_DURATION` → suggests Boost.
7. **Idle** — if load ≤ `LOAD_IDLE` for `LOAD_IDLE_DURATION` → suggests Powersave.
8. **Summer nights** — if enabled and in quiet hours → auto Silent.

**Notifications:**
- Uses `notify-send` with action buttons (Enable Boost / Cool down / Snooze / Not today).
- Runs notification handling in a background thread.
- Resolves user session via `loginctl` to get correct DBUS and display.

**State files:**
- `/etc/boost-auto.conf` — configuration (re-read on mtime change)
- `/var/lib/power-profile/stats.csv` — telemetry
- `/var/lib/power-profile/auto-snooze-until` — snooze timestamp
- `/var/lib/power-profile/auto-skip-date` — "skip today" date

### 3. Web Dashboard (`lib/boost-web.py`)

**Language:** Python 3 (stdlib only)  
**Lines:** 1,571  
**Purpose:** Local web UI at `http://127.0.0.1:8765` for real-time monitoring and
profile switching.

**Architecture:**
- `ThreadingHTTPServer` — one thread per request.
- No framework (stdlib `http.server`).
- Serves a single HTML page with embedded CSS/JS (~30KB inline string).
- JSON API at `/api/status` (GET) and `/api/action` (POST).

**API endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard HTML |
| `/api/status` | GET | Full system state as JSON |
| `/api/action` | POST | Execute a profile/auto command |
| `/report` | GET | Latest HTML report |

**API actions:**
- `boost`, `powersave`, `silent`, `restore` — apply profile
- `auto-mode` (value: dynamic/gaming/creator/quiet/off) — set auto mode
- `snooze` (value: duration like `2h`) — snooze suggestions
- `today-off` — pause for today
- `resume` — resume suggestions
- `summer-nights` (value: on/off) — toggle summer mode
- `quiet-hours` (value: JSON `{"start":"22:00","end":"08:00"}`) — set quiet hours

**CSRF protection:**
- POST requests require `Origin` or `Referer` header matching `http://127.0.0.1:8765`,
  `http://localhost:8765`, or `http://<bind_address>:<port>`.

**Caching:**
- Config file cached with mtime check (30s TTL for ambient temp, 10s for RAPL, 5s for GPU).
- Stats CSV cached with mtime check.
- CPU load uses a global delta accumulator (thread-safe via lock).

### 4. System Tray Applet (`lib/boost-tray.py`)

**Language:** Python 3 (GTK3 + AyatanaAppIndicator3)  
**Lines:** 387  
**Purpose:** System tray icon with quick access to profiles, auto mode switching,
snooze controls, and live CPU telemetry.

**Dependencies:**
- `python3-gi` (PyGObject)
- `gir1.2-gtk-3.0`
- `gir1.2-ayatanaappindicator3-0.1`
- `gir1.2-notify-0.7`

**Features:**
- Shows CPU temp + load in the menu.
- Profile switching with desktop notifications.
- Auto mode submenu with checkmark on active mode.
- Snooze submenu (30m / 1h / 2h / 4h / All Today / Resume).
- "Open Web Dashboard" button.
- Updates every 3 seconds via background thread + `GLib.idle_add`.

**Caching:**
- Power profile cached for 15 seconds (5 cycles × 3s).
- Auto mode / snooze / today-off cached for 30 seconds.

### 5. Shared Shell Library (`lib/power-common.sh`)

**Language:** Bash  
**Lines:** 542  
**Purpose:** Shared functions sourced by all CLI profile scripts and `bin/auto`.

**Key functions:**
- `check_root` — auto-elevate via `sudo`
- `set_cpu_profile` — set governor + EPP via ppd or direct sysfs
- `apply_hardware_limits` — scale RAPL + GPU power limits per mode
- `get_cpu_temp_c` — discover and read CPU temperature sensor
- `get_cpu_load_percent` — calculate CPU load from `/proc/stat`
- `get_gpu_csv` — read NVIDIA (via nvidia-smi) or AMD (via sysfs) GPU stats
- `save_originals` / `restore_fan_curve` — backup/restore boot state
- `show_status` — print formatted terminal status
- `set_io_schedulers` — set none for NVMe/SSD, mq-deadline for HDD
- `set_turbo` — enable/disable turbo boost
- `set_rapl` — write RAPL power limits with max cap check

### 6. Utility Scripts

| Script | Purpose |
|--------|---------|
| `bin/ac-event` | udev-triggered: switches profile on AC plug/unplug |
| `bin/power-save-originals` | systemd oneshot: captures boot-time state |
| `bin/power-report` | generates text/HTML reports from stats CSV |
| `bin/summer` | shortcut for `auto summer-nights on/off` |
| `bin/boost-web` | thin wrapper: `exec python3 /usr/local/lib/boost-web.py` |

### 7. Systemd Services

| Service | Type | Purpose |
|---------|------|---------|
| `power-save-originals.service` | oneshot (boot) | Captures boot state before any profile |
| `boost-auto.service` | simple | Auto daemon (restart on failure, 10s delay) |
| `boost-web.service` | simple | Web dashboard (restart on failure, 5s delay) |

### 8. udev Rules

`99-boost-power.rules` triggers `ac-event` when AC power supply status changes.

## Data Flow

### Profile switch (e.g. `boost`)

```
User runs: boost
  → bin/boost sources power-common.sh
  → check_root → exec sudo
  → disable_auto_for_manual_profile → writes AUTO_MODE=off to config
  → save_originals (first time only)
  → set_cpu_profile performance performance performance
  → set_turbo on
  → apply_hardware_limits boost
     → RAPL: PL1=100%, PL2=100%
     → NVIDIA: power limit = max
     → AMD: power cap = max
  → set_io_schedulers
  → safe_write always → THP
  → restore_fan_curve
  → reset_process_priorities
  → show_status (reads sensors, prints formatted table)
```

### Auto daemon loop (every 5s)

```
read_config() → check mtime, re-parse if changed
apply_preset() → set thresholds based on mode
read_cpu_temp() → read hwmon
read_cpu_load() → /proc/stat delta
is_game_running() → pgrep
is_creator_running() → pgrep
is_meeting_running() → pgrep

if stats_interval elapsed → record_stats() → append CSV

Decision tree (checked in order):
  1. mode == "off" → skip
  2. summer_nights + quiet_hours → auto silent
  3. game detected → auto boost
  4. creator detected → suggest boost
  5. meeting detected → suggest quiet
  6. temp >= critical → emergency powersave
  7. temp >= hot + performance → suggest cooldown
  8. load >= high for duration → suggest boost
  9. load <= idle for duration → suggest powersave
```

### Web dashboard poll (every 2s)

```
Browser polls GET /api/status
  → status_payload()
     → read_config()
     → history() → read stats CSV, parse last 80 rows
     → gpu_stats() → nvidia-smi or AMD sysfs
     → cpu_temp_c() → hwmon discovery
     → cpu_load_percent() → /proc/stat delta
     → ambient_temp() → config or hwmon
     → mode_thresholds() → preset values
     → pause_payload() → check snooze/skip files
     → decision_reason() → text explanation
  → returns JSON

User clicks "Boost" button
  → POST /api/action {action: "boost"}
  → _csrf_ok() → check Origin/Referer
  → run_action("boost") → subprocess.run(["/usr/local/bin/boost"])
  → returns {ok: true, message: "Boost applied"}
```

## State Files

| File | Format | Purpose |
|------|--------|---------|
| `/etc/boost-auto.conf` | `KEY=VALUE` | Configuration (sourced by shell scripts) |
| `/var/lib/power-profile/originals.env` | `KEY=VALUE` | Boot-time hardware state |
| `/var/lib/power-profile/stats.csv` | CSV | Telemetry (rotated at 250KB) |
| `/var/lib/power-profile/auto-snooze-until` | Unix timestamp | Snooze expiration |
| `/var/lib/power-profile/auto-skip-date` | `YYYY-MM-DD` | "Skip today" marker |
| `/var/lib/power-profile/fan-curve-backup.env` | `KEY=VALUE` | Original fan curve |
| `/var/lib/power-profile/reports/latest.html` | HTML | Latest generated report |

## Key Design Decisions

1. **No IPC bus** — Components communicate through filesystem (config + state files).
   Simplifies debugging but means no real-time coordination.

2. **`shell=True` in daemon** — Historical; used because daemon calls CLI scripts
   by name. Should be refactored to direct sysfs writes.

3. **Config as shell source** — `/etc/boost-auto.conf` is valid Bash. Convenient
   for CLI scripts but a security risk (command injection possible).

4. **Stdlib-only web server** — Zero pip dependencies. Works out of the box on any
   Python 3 install. Trade-off: no async, no routing framework.

5. **Dual GPU support** — NVIDIA via `nvidia-smi`, AMD via `amdgpu` sysfs.
   Detection is automatic; no GPU means graceful skip.

6. **Notification actions** — Uses `notify-send --action` for interactive
   notifications. Falls back to plain notifications if actions are unsupported.
