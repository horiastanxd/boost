# Boost - Linux Power Profile Manager

Simple, safe power profile switcher for **Intel + NVIDIA** desktops on Linux.  
Four commands. No daemons. No config files. Fully reversible.

```
boost       # Maximum performance
powersave   # Efficient daily use (~54°C vs 87°C, barely slower)
silent      # Overnight mode: quiet fans, minimum power, priority process
restore     # Revert everything to boot-time state
```

---

## Why

Most Linux desktops run at full BIOS power limits all the time.  
For an i7-14700KF that means **253W burst** and **89°C at idle** with stock settings.

These scripts tune the right knobs — CPU governor, EPP hints, RAPL power limits, GPU wattage, I/O scheduler, transparent hugepages — without disabling turbo or sacrificing responsiveness.

**Real results on i7-14700KF + RTX 5060 Ti:**

| Profile   | Package Temp | PL1   | PL2   | GPU    | Turbo |
|-----------|-------------|-------|-------|--------|-------|
| `boost`     | ~63°C       | 125 W | 253 W | 180 W  | ON    |
| `powersave` | ~54°C       | 125 W | 150 W | 150 W  | ON    |
| `silent`    | ~50°C       | 65 W  | 75 W  | 150 W  | OFF   |
| BIOS default| ~89°C       | 135 W | 253 W | 180 W  | ON    |

---

## Requirements

- Linux with `intel_pstate` driver (Intel 6th gen+)
- NVIDIA GPU with `nvidia-smi` installed
- `lm-sensors` for temperature display
- `nct6798` or compatible SuperIO for fan curve control (optional)
- Root / sudo access

Check compatibility:

```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_driver   # should say: intel_pstate
nvidia-smi --version                                       # should work
ls /sys/class/powercap/intel-rapl/                         # should exist
```

---

## Install

```bash
git clone https://github.com/horiastanxd/boost
cd boost
sudo ./install.sh
```

This copies scripts to `/usr/local/bin/`, the shared lib to `/usr/local/lib/`,  
and enables a systemd service that captures your boot-time state before any profile is applied.

---

## Commands

### `boost` — Maximum performance

```bash
boost
boost --status   # show current state without changing anything
```

- CPU governor: `performance`
- EPP: `performance`
- Turbo: ON
- RAPL PL1: 125 W, PL2: 253 W
- I/O scheduler: `none` for NVMe/SSD, `mq-deadline` for HDD
- Transparent hugepages: `always`
- GPU: max power limit

Use for: gaming, video rendering, ML inference, compilation.

---

### `powersave` — Efficient daily use

```bash
powersave
powersave --status
```

- CPU governor: `powersave` + EPP: `balance_performance`
- Turbo: ON (CPU still boosts under real load)
- RAPL PL1: 125 W, PL2: 150 W (burst capped = less heat spikes)
- Transparent hugepages: `madvise`
- GPU: hardware minimum power limit

Performance impact: ~5% slower on sustained multi-thread workloads.  
Thermal impact: ~15-20°C cooler than BIOS defaults.  
Turbo stays active — the CPU still reaches max clocks when the workload demands it.

---

### `silent` — Overnight / background mode

```bash
silent
```

Designed for leaving the PC on overnight (download, processing, etc.) without fan noise.

On launch it:
1. Applies minimum power settings (turbo off, PL1=65W, PL2=75W)
2. Lowers the Smart Fan IV PWM curve: **12% → 31% → 59% → 86% → 100%** (temp thresholds unchanged — full blast at 75°C+, always safe)
3. Asks which process to keep at high priority
4. Renices all other user processes to nice +15

```
Top running processes for user 'yourname':
-----------------------------------------------
  PID=12345   CPU=15.2  wget
  PID=67890   CPU=3.1   aria2c
  ...
-----------------------------------------------
Process name/PID to keep at HIGH priority (Enter to skip): aria2c
```

The priority process gets: **nice -5** + **ionice best-effort/high**  
Everything else: **nice +15** (yields CPU to the priority process)

Fan curve backup is saved to `/var/lib/power-profile/fan-curve-backup.env` before any modification.  
Running `boost` or `powersave` in the morning restores it automatically.

---

### `restore` — Full revert

```bash
restore
restore --status
```

Restores:
- CPU governor, EPP, turbo, RAPL limits
- Fan curve (from backup if `silent` was used)
- GPU power limit
- Process nice values → 0 for all user processes
- Transparent hugepages

Falls back to known Intel/NVIDIA spec values if no backup exists.

---

## How it works

### CPU

Uses `intel_pstate` in active HWP mode. Key levers:

- **Governor** (`powersave` vs `performance`): in pstate active mode this doesn't cap frequency — it sets the HWP hint aggressiveness. `powersave` + good EPP = CPU idles deep, boosts when needed.
- **EPP (Energy Performance Preference)**: the actual knob. `performance` = always boost. `balance_performance` = boost on demand. `power` = minimize energy (used by `silent`).
- **RAPL PL1/PL2**: hardware power caps. PL2 is the short-burst limit — capping it from 253W to 150W eliminates heat spikes without affecting sustained performance (governed by PL1). All writes are bounds-checked against the hardware maximum reported by the RAPL interface.

### Fan

The `nct6798` SuperIO chip exposes a Smart Fan IV curve via `/sys/class/hwmon/hwmon5/pwm1_auto_point*`.  
`silent` lowers the PWM values while keeping the temperature thresholds and hardware feedback loop intact — the motherboard still controls the fan based on temperature, just with a quieter curve.  
Original values are backed up and restored automatically.

### GPU

`nvidia-smi --power-limit` caps GPU TDP. The RTX 5060 Ti hardware minimum is 150 W — `powersave` and `silent` set it there. `boost` restores the card's maximum (180 W on this model).

### I/O

- NVMe and SSD: scheduler `none` (bypass kernel queue, lower latency)
- HDD: `mq-deadline` (fair deadline scheduling, prevents starvation)

Detection is automatic via `/sys/block/*/queue/rotational`.

### Boot persistence

A systemd one-shot service (`power-save-originals.service`) captures your BIOS/kernel defaults into `/var/lib/power-profile/originals.env` on first boot, before any profile is applied. This is what `restore` uses as its ground truth.

---

## Files

```
/usr/local/bin/boost
/usr/local/bin/powersave
/usr/local/bin/silent
/usr/local/bin/restore
/usr/local/bin/power-save-originals
/usr/local/lib/power-common.sh          # shared helpers
/var/lib/power-profile/originals.env    # auto-saved boot state
/var/lib/power-profile/fan-curve-backup.env   # auto-saved fan curve (created by silent)
/etc/systemd/system/power-save-originals.service
```

---

## Safety

- **RAPL writes are bounds-checked**: the script reads `constraint_*_max_power_uw` and clamps any requested value to the hardware maximum before writing.
- **Fan curve always has thermal feedback**: `silent` never sets manual PWM mode. Smart Fan IV stays active — the fan goes to 100% at 75°C+ regardless of the quiet curve.
- **All changes are ephemeral by default**: nothing survives a reboot unless you run `install.sh` (which only installs the scripts, not a profile). Reboot = BIOS defaults restored.
- **`safe_write` on every /sys write**: warns and continues on failure, never exits mid-profile.
- **Process renice uses nice +15, not SCHED_IDLE**: processes remain schedulable and responsive; they just yield to anything with default priority.

---

## Uninstall

```bash
sudo rm /usr/local/bin/{boost,powersave,silent,restore,power-save-originals}
sudo rm /usr/local/lib/power-common.sh
sudo systemctl disable --now power-save-originals.service
sudo rm /etc/systemd/system/power-save-originals.service
sudo rm -rf /var/lib/power-profile
```

---

## License

MIT
