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

## Repository Layout

Source layout is grouped by *what a file is*, not by what it does at runtime.
The installed paths (`/usr/local/bin`, `/usr/local/lib`) are unchanged.

```
.
├── bin/                     Executables copied to /usr/local/bin
├── lib/                     Runtime libraries copied to /usr/local/lib
│   └── webui/               Dashboard sources (index.html, app.css, app.js)
├── config/                  Shipped defaults
│   ├── boost-auto.conf      → /etc/boost-auto.conf
│   └── presets.json         → /usr/local/share/boost/presets.json
├── packaging/
│   ├── linux/
│   │   ├── systemd/         Units, udev rule, sleep hook
│   │   ├── desktop/         .desktop launchers
│   │   ├── boost-completion.bash
│   │   └── install-gui.sh   Double-clickable graphical installer
│   └── windows/             PowerShell build + Windows notes
├── docs/                    ARCHITECTURE, DEPENDENCIES, TROUBLESHOOTING
├── tests/                   pytest + bash test suites
├── install.sh / uninstall.sh
└── README.md, CHANGELOG.md, CONTRIBUTING.md, SECURITY.md, LICENSE
```

Anything moved here must stay in sync with `install.sh`, `uninstall.sh`, the
systemd units, and the test suites — those are the only places that hardcode
repository-relative paths.

## Platform Layer

Boost talks to sysfs, RAPL and systemd directly. None of that exists on
Windows, so everything that must be done differently per platform goes through
one small interface in `lib/platform_backend.py`:

```
                    ┌──────────────────────────────┐
   bin/boost.py ───►│      PlatformBackend         │
   boost-web.py ───►│  apply_profile               │
                    │  get_cpu_temp / get_cpu_load │
                    │  get_gpu_stats / get_sensors │
                    │  set_gpu_power_limit         │
                    │  gpu_power_limit_range       │
                    │  power_profile               │
                    └───────┬──────────────┬───────┘
                            │              │
              ┌─────────────▼───┐   ┌──────▼─────────────────┐
              │  LinuxBackend   │   │   WindowsBackend       │
              │  bin/* CLI      │   │   powercfg             │
              │  sensors.py     │   │   nvidia-smi           │
              │  /proc, sysfs   │   │   GetSystemTimes / WMI │
              └─────────────────┘   └────────────────────────┘
```

`get_backend()` picks the implementation for the running platform, once.

**The Linux path is unchanged.** `LinuxBackend` only decides *which* installed
command to run — `bin/boost`, `bin/powersave`, `bin/silent --auto`,
`bin/restore` and `bin/auto` remain the single source of truth for what a
profile means. And because `reads_sysfs` is true on Linux, `boost-web.py` keeps
using its own hand-tuned sysfs readers; the backend's telemetry methods are
what `bin/boost.py` and non-Linux platforms use.

**Capability flags** (`supports_fan_control`, `supports_auto_daemon`,
`supports_rapl`, `supports_epp`, `supports_tray`) let callers refuse an action
with a clear message rather than failing obscurely. The dashboard checks them
before dispatching fan and auto-daemon actions.

`lib/boost_paths.py` is the matching change for the filesystem: every module
imports its config and state paths from there. On Linux they resolve to exactly
the historical values (`/etc/boost-auto.conf`, `/var/lib/power-profile/`) —
`tests/test_platform.py` asserts this, because those paths are a compatibility
contract. On Windows both live under `%ProgramData%\Boost`.

### Windows scope

Windows v1 is monitor + profiles + dashboard. It ships **no** fan control (that
needs a signed kernel driver to reach the embedded controller), no RAPL, no
EPP, no auto daemon and no tray. `WindowsBackend` drives `powercfg` through
*aliases* (`SCHEME_MIN` / `SCHEME_BALANCED` / `SCHEME_MAX`), never hardcoded
GUIDs, because OEM images ship their own scheme GUIDs — and falls back to
Windows 11 power-mode overlays when a scheme is absent. See
`packaging/windows/` for the PyInstaller build.

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
**Purpose:** Local web UI at `http://127.0.0.1:8765` for real-time monitoring and
profile switching.

**Architecture:**
- `ThreadingHTTPServer` — one thread per request.
- No framework (stdlib `http.server`).
- JSON API at `/api/status` (GET) and `/api/action` (POST).
- The page itself lives in `lib/webui/` as three ordinary files:

  | File | Contents |
  |------|----------|
  | `webui/index.html` | Markup, with `{{APP_CSS}}` / `{{APP_JS}}` placeholders |
  | `webui/app.css` | Design tokens and all component styles |
  | `webui/app.js` | Polling, SSE stream, charts, dashboard interactions |

  `load_index_html()` reads the three files **once at import time** and inlines
  them into a single document. The browser still gets one request and zero
  external assets, but the sources stay editable and lintable on their own. A
  missing `webui/` directory aborts startup with an explicit message rather than
  serving a broken page.

**Design system:**

The dashboard follows the Legacies design system. Every brand decision is a CSS
custom property in the token block at the top of `webui/app.css` — no rule below
that block hardcodes a colour, and `app.js` resolves the same tokens at load
(`getComputedStyle`) so SVG fills, which cannot use `var()`, stay in sync.

| Token group | Values |
|-------------|--------|
| Surfaces | `--bg-deep:#101317`, `--bg-surface:rgba(24,29,35,.70)`, borders white at 10% |
| Text | `--text-main:#FAFAFA` plus 72% / 48% / 30% steps |
| Accents | `--accent:#6366F1` (indigo, primary) · `--accent-green:#67F264` (secondary highlights and OK status only) |
| Status | boost / powersave / silent / warn / danger — colour carries meaning, never decoration |
| Type | Geist (500 headings, 300–400 body), IBM Plex Mono for every number. No webfont is fetched: the stacks fall back to `system-ui`, so an offline machine still renders correctly. |
| Radii | 20px for cards, buttons and surfaces · 10px for small elements and media |
| Shadows | `0 1px 3px` / `0 4px 16px` / `0 8px 32px` black — diffuse and never coloured |
| Spacing | 4px scale exposed as `--sp-1 … --sp-11`; container padding 48px desktop, 20px mobile |
| Motion | 200–300ms interactions, a discreet fade-up reveal, and a `prefers-reduced-motion` opt-out |

Buttons come in two shapes — filled indigo primary and transparent ghost with a
thin border. Profile buttons are flat fills in their category colour. Cards are
translucent dark with a fine blur and a `translateY(-4px)` hover. The header is
sticky and minimal, picking up a blurred ground once the page scrolls.

The Python module is organised in banner-delimited sections — helpers, config,
presets, ambient/quiet hours, pause state, system state cache, live snapshot,
sensors/fans/GPU, hardware telemetry, history/battery, `/api/status` payload,
dashboard actions, page assembly, HTTP server.

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

### 6. Sensor Layer (`lib/sensors.py`)

**Language:** Python 3 (stdlib only)
**Purpose:** One unified view of every `/sys/class/hwmon` temperature.

- Enumerates all chips once, caches the topology, re-reads only `tempN_input`.
- **Stable ids:** `chip:label` (`nct6798:SYSTIN`, `nvme-nvme0:Composite`), never
  `hwmonN` — kernel hwmon numbering is probe-order dependent and changes across
  reboots. Duplicate chip names are disambiguated by the device they hang off.
- Classifies each sensor into a component category (cpu, cpu_core, gpu, nvme,
  sata, ram, vrm, chipset, board, network, battery) with per-category warn and
  critical thresholds; chip-reported `tempN_max`/`tempN_crit` only ever *tighten*
  those defaults.
- Drops readings of 0 or >=200 °C (unconnected superio channels).
- `missing_drivers()` reports hardware present without a driver (DDR5 SPD hub
  without `spd5118`, SATA disks without `drivetemp`) and the exact fix command;
  used by `auto doctor`.
- Called by the daemon once per tick; the web layer only falls back to reading it
  directly when the daemon snapshot is stale.

### 7. Fan Curve Engine (`lib/fancontrol.py`)

**Language:** Python 3 (stdlib only)
**Purpose:** Per-fan curve control with a safety floor that cannot be overridden.

- Discovery: every `pwmN` with a `pwmN_enable` under `/sys/class/hwmon`, keyed
  `chip:pwmN`. GPU chips are skipped by design.
- Config: `/etc/boost-fans.json` — per fan a source (sensor category or explicit
  ids, `max`/`avg` mix), `min_pwm`, `stop_allowed`, `hyst_up`/`hyst_down`,
  `response_delay_s`, `step_limit`, and one curve per profile
  (`boost`/`balanced`/`silent`).
- Validation refuses curves that fall as temperature rises or that do not reach
  80% by 85 °C, so a saved curve is always able to cool the machine.
- `guard_floor()` derives a minimum pwm from live CPU/GPU/NVMe/VRM temperature
  every tick and raises the fan above the requested curve when needed; the reason
  string is published for the UI.
- Ownership: `pwmN_enable` is set to 1 (manual) only after saving the original
  value to `fans-original-enable.json`, and is re-asserted every tick (suspend
  resets it). `failsafe_all()` restores the board's own mode and is wired to the
  unit's `ExecStopPost`.
- Conflict handling: read-back mismatch marks another writer and backs that
  channel off for 120 s; an active fancontrol/CoolerControl/thinkfan unit blocks
  the engine entirely.
- CLI: `fancontrol.py discover|status|init|enable|disable|calibrate|preset|test|failsafe`,
  driven by `auto fans ...`.

### 8. Utility Scripts

| Script | Purpose |
|--------|---------|
| `bin/ac-event` | udev-triggered: switches profile on AC plug/unplug |
| `bin/power-save-originals` | systemd oneshot: captures boot-time state |
| `bin/power-report` | generates text/HTML reports from stats CSV |
| `bin/summer` | shortcut for `auto summer-nights on/off` |
| `bin/boost-web` | thin wrapper: `exec python3 /usr/local/lib/boost-web.py` |

### 9. Systemd Services

| Service | Type | Purpose |
|---------|------|---------|
| `power-save-originals.service` | oneshot (boot) | Captures boot state before any profile |
| `boost-auto.service` | notify | Auto daemon + fan engine. `WatchdogSec=120`; `ExecStopPost` runs `fancontrol.py failsafe` so fans always return to board control |
| `boost-web.service` | simple | Web dashboard (restart on failure, 5s delay) |
| `/usr/lib/systemd/system-sleep/boost` | sleep hook | Re-applies RAPL + GPU power limits after resume |

### 10. udev Rules

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

read_all_sensors() → lib/sensors.py, every hwmon temperature (once per tick)
run_fan_engine() → lib/fancontrol.py tick: curve + guard floor + pwm writes
handle_pending_silent() → apply a queued Silent request once the machine cools

if stats_interval elapsed → record_stats() → append CSV
write_live_snapshot() → live.json (cpu, gpu, sensors, fans, interlock, limits)

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

### Web dashboard updates (Server-Sent Events)

```
Browser opens GET /api/stream
  → the handler stats live.json once a second and pushes a full status payload
    whenever the daemon's snapshot changes (or every 10s as a heartbeat)
  → EventSource unavailable or stream dropped → falls back to polling /api/status

GET /api/status
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
| `/var/lib/power-profile/fan-curve-backup.env` | `KEY=VALUE` | Original Smart Fan IV curve |
| `/etc/boost-fans.json` | JSON | Fan curve configuration (per fan, per profile) |
| `/var/lib/power-profile/fans-calibration.json` | JSON | Measured start/stop pwm and RPM curve |
| `/var/lib/power-profile/fans-original-enable.json` | JSON | Original `pwmN_enable` per channel (failsafe) |
| `/var/lib/power-profile/fan-override.json` | JSON | Temporary "test this fan" override |
| `/var/lib/power-profile/fan-engine-pause` | Unix timestamp | Engine paused until (calibration) |
| `/var/lib/power-profile/silent-pending` | `epoch reason` | Queued Silent request held by the interlock |
| `/var/lib/power-profile/live.json` | JSON | Per-tick snapshot shared with web/tray |
| `/var/lib/power-profile/reports/latest.html` | HTML | Latest generated report |

## Key Design Decisions

1. **No IPC bus** — Components communicate through filesystem (config + state files).
   Simplifies debugging but means no real-time coordination.

2. **No shell in the daemon** — profile commands are spawned with
   `subprocess.Popen(shlex.split(cmd))`; there is no `shell=True` anywhere.

3. **Config is parsed, never sourced** — `/etc/boost-auto.conf` is `KEY=VALUE`
   read by `read_safe_config()` (shell) and an explicit parser (Python). Keys that
   could hijack a shell (`PATH`, `LD_*`, `BASH*`, ...) are ignored. Structured data
   (fan curves) lives in JSON instead: `/etc/boost-fans.json`.

4. **Stdlib-only web server** — Zero pip dependencies. Works out of the box on any
   Python 3 install. Trade-off: no async, no routing framework.

5. **Dual GPU support** — NVIDIA via `nvidia-smi`, AMD via `amdgpu` sysfs.
   Detection is automatic; no GPU means graceful skip.

6. **Notification actions** — Uses `notify-send --action` for interactive
   notifications. Falls back to plain notifications if actions are unsupported.

7. **Fan curves are requests** — the engine treats a user curve as a target and
   clamps it against a live safety floor, so no configuration reachable from the
   UI or a hand-edited JSON file can hold the fans down on hot hardware.

8. **One writer per pwm** — Boost takes ownership of a channel explicitly, checks
   read-back every tick, and gives the channel back to the board on stop, crash,
   watchdog timeout or uninstall.
