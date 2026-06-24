<div align="center">

# ⚡ boost

**Linux power profile manager for Intel + NVIDIA desktops**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![ShellCheck](https://github.com/horiastanxd/boost/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/horiastanxd/boost/actions/workflows/shellcheck.yml)
[![Shell: Bash](https://img.shields.io/badge/Shell-Bash-4EAA25?logo=gnubash&logoColor=white)](bin/boost)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux-FCC624?logo=linux&logoColor=black)](https://kernel.org)
[![CPU: Intel](https://img.shields.io/badge/CPU-Intel%20pstate-0071C5?logo=intel&logoColor=white)](https://www.kernel.org/doc/html/latest/admin-guide/pm/intel_pstate.html)
[![GPU: NVIDIA](https://img.shields.io/badge/GPU-NVIDIA-76B900?logo=nvidia&logoColor=white)](https://developer.nvidia.com/nvidia-system-management-interface)
[![GNOME: power-profiles-daemon](https://img.shields.io/badge/GNOME-power--profiles--daemon-4A86CF?logo=gnome&logoColor=white)](https://gitlab.freedesktop.org/hadess/power-profiles-daemon)

Manual profiles, Auto mode, and a local web dashboard. Fully reversible.
**GNOME Power Mode indicator stays in sync automatically.**

</div>

```
boost       # Maximum performance — gaming, compiling, ML inference
powersave   # Efficient daily use — barely slower, 15–25°C cooler
silent      # Overnight — quiet fans, priority process, minimum power
restore     # Revert everything to your boot-time BIOS state
auto        # Intelligent daemon — monitors load & temp, prompts when switching makes sense
summer      # Shortcut for Auto Summer mode
auto summer # Hot-room mode — cooler behavior for warm summer rooms
auto stats  # Current power statistics in terminal
auto report # Local HTML report with recent samples
auto web    # Realtime local web dashboard with controls
auto setup  # Guided setup for non-technical users
auto doctor # Health check with plain-language hints
```

---

## The problem

Most Linux desktops run at full BIOS power limits all the time.

On a stock i7-14700KF that means **253 W burst** and **89°C at idle** — the CPU spikes to maximum power on every context switch, fans react, temperatures climb.

`boost` gives you per-use-case control over the knobs that actually matter: CPU governor, energy performance hints (EPP), RAPL power limits, GPU wattage, I/O scheduler, and fan curve — with a single command and full safety guarantees.

---

## Results

Tested on **i7-14700KF + RTX 5060 Ti**, Ubuntu 24.04, one case fan:

| Profile | Package Temp | Fan | PL1 | PL2 | GPU | Turbo |
|---------|-------------|-----|-----|-----|-----|-------|
| BIOS default | **89°C** | loud | 135 W | 253 W | 180 W | ON |
| `boost` | 63°C | moderate | 125 W | 253 W | 180 W | ON |
| `powersave` | 54°C | quiet | 125 W | 150 W | 150 W | ON |
| `silent` | ~50°C | near-silent | 65 W | 75 W | 150 W | OFF |

**35°C drop from software alone.** No undervolting, no hardware changes.

---

## Quick start

```bash
git clone https://github.com/horiastanxd/boost
cd boost
sudo ./install.sh
```

Then:

```bash
powersave        # start here — good for 95% of daily use
boost            # switch to when you need full power
silent           # tonight, before you sleep
restore          # back to BIOS defaults anytime
auto mode calm   # optional: enable gentle automatic suggestions
summer           # shortcut: hot-room Auto mode when ambient temperature is high
auto report      # generate and open a local web report
auto web         # open realtime web controls
auto doctor      # check whether sensors, GPU stats, reports, and notifications work
```

All commands auto-elevate via `sudo` — no need to prefix them.

---

## Requirements

| Component | Requirement |
|-----------|-------------|
| CPU driver | `intel_pstate` (Intel 6th gen+) |
| GPU | NVIDIA with `nvidia-smi` |
| GNOME sync | `power-profiles-daemon` + `powerprofilesctl` *(auto-detected, optional)* |
| Fan control | `nct6798` or compatible SuperIO *(optional — silent mode)* |
| Privileges | sudo |

Check in one line:

```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver  # intel_pstate
nvidia-smi -L                                             # GPU found
ls /sys/class/powercap/intel-rapl/                        # RAPL available
```

> **AMD users:** RAPL and fan control work identically. Replace governor/EPP logic with `amd_pstate` equivalents. PRs welcome.

---

## Commands

### `boost` — Full performance

```bash
boost [--status] [--version]
```

| Setting | Value |
|---------|-------|
| GNOME Power Mode | **Performance** |
| CPU governor | `performance` |
| Energy performance hint (EPP) | `performance` |
| Turbo boost | ON |
| RAPL PL1 (sustained) | 125 W |
| RAPL PL2 (burst) | 253 W |
| I/O scheduler | `none` (NVMe/SSD) · `mq-deadline` (HDD) |
| Transparent hugepages | `always` |
| GPU power limit | max (180 W) |

Use for: gaming, video rendering, ML training/inference, large compilations.

---

### `powersave` — Efficient daily use

```bash
powersave [--status] [--version]
```

| Setting | Value |
|---------|-------|
| GNOME Power Mode | **Balanced** |
| CPU governor | `powersave` |
| EPP | `balance_performance` |
| Turbo boost | ON (boosts on real load, idles deep) |
| RAPL PL1 | 125 W |
| RAPL PL2 | 150 W *(capped from 253 W — cuts heat spikes)* |
| Transparent hugepages | `madvise` |
| GPU power limit | 150 W (hardware minimum) |

With `intel_pstate` active + `balance_performance` EPP, the CPU still reaches maximum turbo frequencies under load — HWP (Hardware P-states) handles scaling. The key difference: burst power is capped at 150 W instead of 253 W, which eliminates the brief thermal spikes that drive fan noise without affecting sustained throughput.

**Typical impact:** ~5% slower on sustained multi-thread, identical on single-thread tasks.

---

### `silent` — Overnight mode

```bash
silent
```

Designed for: downloads, background processing, anything you run before sleeping.

**What it does:**

1. Applies minimum power settings — turbo off, PL1=65 W, PL2=75 W
2. Lowers the Smart Fan IV PWM curve:

   ```
   Before:  60% at 20°C → 69% at 45°C → 84% at 60°C → 100% at 70°C
   After:   12% at 20°C → 31% at 45°C → 59% at 60°C → 100% at 75°C
   ```

   Temperature thresholds and hardware feedback loop unchanged — full blast at 75°C+ regardless.

3. Asks which process to prioritize:

   ```
   Top running processes for user 'you':
   -----------------------------------------------
     PID=1234    CPU=12.3   aria2c
     PID=5678    CPU=3.1    chrome
     ...
   -----------------------------------------------
   Process name/PID to keep at HIGH priority (Enter to skip): aria2c
   ```

4. Sets priority process to **nice -5** + **ionice best-effort/high**
5. Renices everything else to **nice +15** (yields CPU without blocking)

Fan curve is backed up before modification. Running `boost` or `powersave` in the morning restores it automatically.

---

### `restore` — Full revert

```bash
restore [--status]
```

Restores all settings to the state captured at boot:
- CPU governor, EPP, turbo, RAPL limits
- Fan curve (from backup if `silent` was used)
- GPU power limit
- Process nice values → 0 for all user processes
- Transparent hugepages

---

### `auto` — Gentle helper + reports

```bash
auto setup           # guided menu, easiest option
auto doctor          # friendly health check
auto modes           # show all Auto mode thresholds
summer              # shortcut for auto mode summer
auto mode calm       # rare suggestions, best default
auto mode summer     # hot-room mode, cooler and slower to suggest Boost
auto mode friendly   # balanced suggestions
auto mode active     # faster suggestions for heavy work
auto mode quiet      # no suggestions, only critical heat protection
auto mode off        # disable auto mode completely
auto stats           # print a current power snapshot
auto report          # generate and open a local HTML report
auto web             # realtime web dashboard with controls
auto dashboard       # same as auto web
auto summer-nights on  # optional: Summer can apply Silent during quiet hours
```

Manual profile commands stay in control: running `boost` or `powersave`
turns auto mode off, so the daemon will not fight your choice. Run
`auto start` or `auto mode calm|summer|friendly|active|quiet` to opt back in.

`auto mode summer` is for high ambient temperatures. It suggests cooler
profiles sooner, requires longer sustained load before suggesting Boost,
leaves Boost more quickly when the system is quiet, and will not suggest
Boost if the CPU is already above the configured Boost temperature limit.

`auto modes` prints the active thresholds for every built-in mode, so you
can compare `calm`, `summer`, `friendly`, `active`, `quiet`, and `off`
without reading the config file.

Summer can use local ambient temperature if available. Set
`AMBIENT_TEMP_C=29`, point `AMBIENT_TEMP_FILE` at a local sensor file, or
expose an hwmon sensor with an ambient/system/room label. No internet
weather API is used. In Summer mode, ambient readings at 28°C+ lower the
thermal thresholds slightly.

`auto summer-nights on` links Summer and Silent only when you opt in.
When Auto mode is `summer` and quiet hours are active, the daemon may
apply `silent --auto` without asking for an interactive process priority.

Reports are generated under:

```text
/var/lib/power-profile/reports/latest.html
```

The daemon records a lightweight CSV sample about once per minute while
it is running:

```text
/var/lib/power-profile/stats.csv
```

The report includes current profile, CPU load, CPU temperature, GPU
temperature/power, RAPL limits, governor, EPP, turbo state, and recent
history.

`auto web` opens a local dashboard at `http://127.0.0.1:8765`.
It updates live and lets non-CLI users switch Boost/Powersave, change
Auto mode, snooze suggestions, edit quiet hours, generate reports, and
open the latest report from the browser. It also shows the current
snooze/today-off state, local ambient source, active thresholds, all mode
presets, and the current Auto decision reason such as "Not suggesting
Boost because CPU is 79 C and the summer Boost limit is 70 C."

`auto doctor` checks the pieces that commonly confuse users:
GNOME power profile sync, NVIDIA access, CPU power-limit support,
temperature sensor access, notification buttons, stats history, and
whether a web report exists.

---

## How it works

### RAPL power limits

Intel CPUs expose two power limits via the RAPL (Running Average Power Limit) interface:

- **PL1** (`long_term`) — sustained limit, ~56-second window. This is the thermal design point.
- **PL2** (`short_term`) — burst limit, ~2.4ms window. BIOS defaults are often 2× PL1 or higher.

Capping PL2 from 253 W to 150 W means the CPU can't spike above 150 W for burst workloads. This is the single most effective lever for reducing fan noise: the fan reacts to temperature spikes, and spikes come from PL2 bursts. Sustained single-thread and sustained multi-thread performance (governed by PL1) are unaffected.

All RAPL writes are bounds-checked against the hardware-reported maximum before writing.

### EPP (Energy Performance Preference)

With `intel_pstate` in active HWP mode, the CPU's internal P-state selection is driven by EPP hints rather than the OS governor alone. The governor (`performance` vs `powersave`) sets the ceiling; EPP sets the behavior within that ceiling:

| EPP | Behavior |
|-----|----------|
| `performance` | Always select the highest P-state |
| `balance_performance` | Prefer high P-states, allow scaling |
| `power` | Aggressive frequency reduction, minimum energy |

`powersave` mode uses `balance_performance` — the CPU still hits maximum turbo when the workload demands it, but drops to low frequencies immediately when idle.

### Fan control

The `nct6798` SuperIO chip exposes Smart Fan IV curve points via sysfs. `silent` modifies only the **PWM values** at each temperature threshold — the temperature points and hardware feedback loop remain intact. The motherboard retains full thermal authority; the quiet curve just shifts the fan response lower at temperatures the CPU won't reach in low-power mode.

### I/O scheduler

Detected automatically via `/sys/block/*/queue/rotational`:
- `rotational=0` → scheduler `none` (NVMe/SSD: bypass kernel queue, minimum latency)
- `rotational=1` → scheduler `mq-deadline` (HDD: deadline scheduling, prevents starvation)

Loop devices (snap/flatpak mounts) are excluded.

---

## File layout

```
/usr/local/bin/
  boost                   # performance profile
  powersave               # efficient profile
  silent                  # overnight profile
  summer                  # shortcut for Auto Summer mode
  restore                 # revert to boot state
  power-save-originals    # run by systemd at boot
  auto                    # gentle automatic helper
  power-report            # text/HTML power statistics
  boost-web               # local realtime dashboard

/usr/local/lib/
  power-common.sh         # shared: safe_write, set_rapl, set_io_schedulers, show_status
  boost-web.py            # local dashboard server

/var/lib/power-profile/
  originals.env           # boot-time state (captured once by systemd service)
  fan-curve-backup.env    # fan curve backup (created when silent runs)
  stats.csv               # lightweight history for reports
  reports/latest.html     # latest generated web report

/etc/systemd/system/
  power-save-originals.service   # one-shot, runs before basic.target
  boost-auto.service             # Auto mode daemon
  boost-web.service              # local dashboard daemon

tests/
  auto-mode-presets.sh           # preset threshold regression test
```

---

## Safety

| Concern | How it's handled |
|---------|-----------------|
| RAPL writes above hardware max | `set_rapl()` reads `constraint_*_max_power_uw` and clamps before writing |
| Fan stuck at manual speed | `silent` keeps Smart Fan IV mode active — hardware controls the fan |
| Overheating in silent mode | Fan goes to 100% at 75°C+ regardless of quiet curve |
| /sys write failure | `safe_write()` warns and continues — profile applies partially rather than failing |
| Wrong originals | Systemd service captures state at first boot before any profile touches it |
| Process starvation | `renice +15` not `SCHED_IDLE` — processes remain schedulable |
| No reboot persistence | Profile changes are ephemeral by default; nothing survives reboot unless you re-run the command |

---

## Uninstall

```bash
sudo rm /usr/local/bin/{boost,powersave,silent,restore,power-save-originals}
sudo rm /usr/local/bin/{summer,auto,power-report,boost-web}
sudo rm /usr/local/lib/power-common.sh
sudo rm /usr/local/lib/boost-web.py
sudo systemctl disable --now power-save-originals.service boost-auto.service boost-web.service
sudo rm /etc/systemd/system/{power-save-originals,boost-auto,boost-web}.service
sudo rm -rf /var/lib/power-profile
```

---

---

## `auto` — Intelligent daemon

```bash
auto start      # enable + start background daemon
auto stop       # stop daemon
auto status     # current metrics + thresholds
auto logs       # tail journalctl output
auto config     # show active config
auto modes      # show every built-in mode threshold
auto summer     # hot-room mode
auto summer-nights on|off
```

Monitors CPU temperature and load every 5 seconds. Reacts based on configurable thresholds:

| Event | Action |
|-------|--------|
| Temp ≥ 85°C (CRITICAL) | **Auto-switch to powersave** + desktop notification — no prompt, immediate safety action |
| Temp ≥ 78°C + profile=boost | Prompt: *"CPU at 82°C — switch to Powersave?"* (30s timeout, defaults to no) |
| Load ≥ 75% for 120s + profile=powersave | Prompt: *"CPU at 80% load — switch to Boost?"* |
| Load ≤ 8% for 10 min + profile=boost | Prompt: *"CPU idle — switch to Powersave?"* |
| Summer mode + CPU above Boost limit | Do not suggest Boost until the system is cooler |
| Summer mode + quiet hours + `SUMMER_SILENT_NIGHTS=yes` | Apply `silent --auto` at most once per hour |

Prompts appear as desktop notifications with action buttons when supported.
No answer keeps the current profile. Prompts are throttled by mode.

**Config** (`/etc/boost-auto.conf`):

```bash
TEMP_CRITICAL=85       # °C: auto-switch threshold
TEMP_HOT=78            # °C: prompt threshold
BOOST_TEMP_LIMIT=78    # °C: do not suggest Boost above this temperature
SUMMER_SILENT_NIGHTS=no # optional: apply silent --auto during summer quiet hours
AMBIENT_TEMP_C=         # optional local room temperature override
AMBIENT_TEMP_FILE=      # optional local file containing room temperature
LOAD_HIGH=75           # %: high load threshold
LOAD_HIGH_DURATION=120 # seconds of sustained load before prompting
LOAD_IDLE=8            # %: idle threshold
LOAD_IDLE_DURATION=600 # seconds of idle before prompting
PROMPT_COOLDOWN=900    # seconds between prompts
POLL_INTERVAL=5        # measurement frequency
```

Runs as a systemd service. Detects Wayland and X11 sessions automatically.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Hardware compatibility reports especially welcome.

---

<div align="center">

MIT License · [Horia Stan](https://github.com/horiastanxd)

*If this saved your CPU from thermal hell, consider leaving a ⭐*

</div>
