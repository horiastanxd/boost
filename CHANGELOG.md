# Changelog

All notable changes to Boost are documented here.

## Unreleased

### Fixed
- **Tray icon click opens GNOME Quick Settings instead of the Boost menu** on GNOME 50 / Ubuntu 26.04 — the indicator was registered under `AyatanaAppIndicator3.IndicatorCategory.SYSTEM_SERVICES`, a category some shells absorb into the system quick-settings panel instead of giving it its own dropdown. Switched to `APPLICATION_STATUS`, the category used by indicators that keep an independent click menu (e.g. Livepatch).
- **Manual profile clicks (Performance/Balanced/Eco/Default) were killing the fan engine.** `disable_auto_for_manual_profile` fully stopped `boost-auto.service` on every manual pick — but that's the only process that ticks the fan curve engine, so fans fell back to raw board control (BIOS Smart Fan) a few seconds after *any* tray click, independent of preset. Now it only flips `AUTO_MODE=off` in config, which `boost-daemon.py`'s tick loop already treats as "keep running the fan engine and sensors, skip the auto-switch suggestions" — this mode existed and was tested, it just wasn't being used for manual switches.

### Added
- **Two quieter fan presets, `whisper` and `hush`** — sit below the existing `silent` preset for people who find it still audible at idle. Both hold near the calibrated stall floor well into the 60s/70s C and only reach full speed past 85C, same as every other preset. They go through `guard_floor()` like any curve, so the daemon still forces fans up if CPU/GPU/NVMe/VRM actually gets hot — a quiet curve can't cook the machine. Selectable per-fan from the existing preset buttons and `auto fans preset <fan> whisper|hush [profile]`; no UI or profile-key changes needed since presets were already data-driven.
- **`auto fans preset-all <name> [profile]`** — applies one preset to every discovered fan's given profile (default `silent`) in one call, instead of looping `auto fans preset <fan> ...` by hand.
- **Tray → 🔇 Quiet Fans submenu** (Whisper / Hush / Silent) — one click applies the preset to every fan profile and takes effect immediately under whatever power profile is already active, backed by `auto fans quiet <whisper|hush|silent>`. Doesn't touch power-profiles-daemon or stop boost-auto.service, so it can't fight the auto daemon the way switching profiles would.
- **Eco Mode busy-machine warning** — clicking Eco Mode (tray or `silent`) while the GPU is over 30% busy now prints/notifies a heads-up that Eco Mode's power cap may make GPU-bound work feel slower, without blocking the switch. CPU-side, the existing thermal interlock (hot CPU or sustained high load) still queues Silent instead of applying it outright — this only adds the GPU signal it didn't have.

## [1.10.0] - 2026-08-16

### Added
- **Windows build (beta)** — a deliberate partial port rather than a rewrite. Profiles (Performance/Balanced/Eco/Restore) are applied with `powercfg`, turbo through `PERFBOOSTMODE`, and the NVIDIA power limit through `nvidia-smi`, clamped to the driver's own range. The same web dashboard runs at `http://127.0.0.1:8765`. Scheme *aliases* (`SCHEME_MIN`/`SCHEME_BALANCED`/`SCHEME_MAX`) are used instead of hardcoded GUIDs because OEM images ship their own, with a fallback to Windows 11 power-mode overlays when a scheme is absent. Fan control, RAPL, EPP, the auto daemon and the tray are explicitly **not** ported and say so instead of failing obscurely — fan control on Windows needs a signed kernel driver, and a userspace tool that half-owns the fans is worse than one that leaves them to the firmware.
- **Platform layer** (`lib/platform_backend.py`, `lib/platform_windows.py`) — one interface for everything that differs per platform: `apply_profile`, `get_cpu_temp`, `get_cpu_load`, `get_gpu_stats`, `set_gpu_power_limit`, `get_sensors`, `gpu_power_limit_range`, `power_profile`, plus capability flags. The Linux implementation only decides which installed command to run, so `bin/boost`, `bin/powersave`, `bin/silent --auto`, `bin/restore` and `bin/auto` remain the single source of truth for what a profile means.
- **`lib/boost_paths.py`** — config and state paths in one place. Linux resolves to exactly the historical `/etc/boost-auto.conf` and `/var/lib/power-profile/`; Windows uses `%ProgramData%\Boost`. `tests/test_platform.py` asserts the Linux values, because they are a compatibility contract.
- **`bin/boost.py`** — cross-platform entry point (`boost | powersave | silent | restore | status | web`). On Linux it is optional; on Windows PyInstaller freezes it into `boost.exe`.
- **`packaging/windows/build.ps1`** and **`.github/workflows/windows-build.yml`** — a portable zip built and smoke-tested on `windows-latest` on every push, attached to releases on a `v*` tag.
- 32 new tests covering the platform layer, including the Windows backend (exercised off-platform by faking the process runner).

### Changed
- **Repository reorganised** — the root now holds only README/LICENSE/CHANGELOG/CONTRIBUTING/SECURITY plus the two installers. Documentation moved to `docs/`, shipped defaults to `config/`, and systemd units, `.desktop` entries, the completion script and the graphical installer to `packaging/linux/`. **Installed paths are unchanged** (`/usr/local/bin`, `/usr/local/lib`, `/etc/boost-auto.conf`, `/usr/local/share/boost`), so upgrades are transparent — but a clone older than this release must `git pull` before `sudo ./install.sh`.
- **The dashboard page left the Python source.** 1,800 lines of HTML/CSS/JS that used to be one string in `lib/boost-web.py` now live in `lib/webui/{index.html,app.css,app.js}`. The server reads them once at import and inlines them, so the browser still gets a single request with zero external assets and the rendered page is byte-identical. A missing `webui/` directory fails loudly at startup instead of serving a broken page.
- **Dashboard restyled to the Legacies design system** — `#101317` ground, translucent cards with white 10% borders, indigo `#6366F1` as the primary accent and green `#67F264` only as a secondary highlight and OK status, Geist for text and IBM Plex Mono for numbers, 20px radii, diffuse black shadows and a 4px spacing scale. Every brand value is a CSS custom property; `app.js` reads the same tokens through `getComputedStyle` so the SVG charts follow them too. The animated mesh gradient behind the page and the tricolour gradient headline are gone. No webfont is fetched — the server has no guaranteed internet access, so the stacks fall back to `system-ui`.
- Buttons are now a filled indigo primary or a transparent ghost; profile buttons are flat category fills instead of gradients with coloured glows. Cards lift 4px on hover. The header is sticky and blurs on scroll. A `prefers-reduced-motion` opt-out was added.

## [1.9.0] - 2026-08-16

### Added
- **Smart fan curve control** (`lib/fancontrol.py`, `/etc/boost-fans.json`, `auto fans ...`) — per-fan curves with hysteresis, step limiting and response delay, one curve per profile (Performance/Balanced/Eco), edited by dragging points in the dashboard or applied with 1-click Silent/Balanced/Aggressive presets. Design decisions come straight from what breaks in the existing tools:
  - Fans are addressed as `chip:pwmN` (e.g. `nct6798:pwm1`), never `hwmonN` — kernel hwmon numbering is probe-order dependent and reshuffles across reboots, which is what silently repoints a saved curve at the wrong fan.
  - **Curves are requests, not orders.** Every tick the engine derives a safety floor from CPU/GPU/NVMe/VRM temperature and raises the fans above the requested curve when the hardware needs it — so an Eco curve can never cook the machine — and the dashboard says exactly which sensor forced it.
  - **Failsafe:** the daemon is now `Type=notify` with `WatchdogSec=120`, and `ExecStopPost` runs `fancontrol.py failsafe`, which returns every pwm channel to its original BIOS/Smart Fan mode on stop, crash, upgrade or uninstall. `pwmN_enable` is also re-asserted every tick, so a suspend/resume that resets it cannot leave fans stuck.
  - **One writer per channel:** an external tool moving a pwm Boost owns is detected by read-back mismatch, logged, and that channel is backed off instead of fought over. `auto doctor` refuses to start the engine when fancontrol/CoolerControl/thinkfan is running.
  - **Calibration wizard** (`auto fans calibrate`) measures start/stop pwm and the RPM curve of each fan into `/var/lib/power-profile/fans-calibration.json`, and regenerates the presets from the measured minimum speed. Fans that never stop are recorded as such instead of failing the run (where `pwmconfig` gives up).
  - Boards that expose pwm read-only (several Dell/server designs) are detected on the first write and reported, not retried every tick. GPU fans are deliberately out of scope.
- **Dumb-proof Eco interlock** — `silent` no longer applies a quiet profile while the machine is hot or under sustained load. The request is *queued* ("silent-pending") and the daemon applies it by itself once temperature and load come down, with a notification. The dashboard shows the reason and countdown on the Eco button; `silent --force` overrides.
- **Every component temperature** (`lib/sensors.py`, `auto sensors`, `/api/sensors`) — one unified layer over `/sys/class/hwmon` with stable `chip:label` ids, covering CPU package and per-core, GPU edge/junction/memory, NVMe, SATA (`drivetemp`), DDR5 DIMMs (`spd5118`), VRM, chipset, motherboard, network and battery sensors, each with per-component warn/critical thresholds (NVMe 65 °C, RAM 60 °C, VRM 90 °C). New "Component Temperatures" dashboard section with per-category cards, trend sparklines and a collapsible per-sensor breakdown. `install.sh` now loads `spd5118`/`drivetemp` and writes `/etc/modules-load.d/boost.conf`; `auto doctor` reports which sensors are missing and the exact command to fix them.
- **GPU power limit in watts** (`auto gpu-limit <W> [profile]`, dashboard slider, `GPU_PL_BOOST_W`/`GPU_PL_POWERSAVE_W`/`GPU_PL_SILENT_W`) — clamped to the range the driver itself reports (`nvidia-smi -q -d POWER`, `power1_cap_min/max`), stored per profile, re-applied on every profile switch, at boot and after resume. Raising the limit is refused while the GPU is at 85 °C or hotter.
- **Suspend/resume hook** (`/usr/lib/systemd/system-sleep/boost`) — re-applies RAPL PL1/PL2 and the GPU power limit after waking, which previously reverted to firmware defaults while the dashboard still claimed otherwise.
- **Simple / Advanced dashboard modes** — Simple shows profiles, temperatures and fan presets; Advanced adds curve editors, GPU limit, thresholds, history and the full config grid. The choice is remembered per browser.
- `stats.csv` gained `nvme_temp,ram_temp,vrm_temp,board_temp` columns (existing files are migrated in place, keeping their history), and `power-report` shows them.

### Changed
- **The dashboard no longer polls.** `/api/stream` (Server-Sent Events) pushes a payload when the daemon's snapshot changes, so an idle machine costs one cheap `stat()` per second instead of a full JSON build every 2 s in every open tab. Polling remains as the automatic fallback.
- The daemon reads all hwmon sensors once per tick and publishes them in the live snapshot; the dashboard and tray consume that instead of re-polling sysfs per request.

## [1.8.1] - 2026-08-14

### Fixed
- **Inflated CPU package temperature from a single outlier core** — coretemp reports "Package id 0" as the max of all per-core Digital Thermal Sensors. On systems where one core's DTS is miscalibrated (reads persistently 20-40C above the others even at idle), Boost's temperature reading, dashboard, and daemon thresholds all followed that single core's spurious spikes instead of the actual package temperature — visibly diverging from the BIOS/PECI reading and from `sensors`' own per-core breakdown. `find_cpu_temp_path`/`get_cpu_temp`/`cpu_temp_c` now compare the package reading against the median of per-core sensors and fall back to the median when the gap exceeds 15C.

## [1.8.0] - 2026-07-09

### Added
- **Thermal-aware Performance profile** — Boost no longer pins the CPU at maximum turbo regardless of load. New config keys in `/etc/boost-auto.conf`:
  - `BOOST_EPP` (default `balance_performance`) — full turbo under sustained load, but cores downclock at light load. Cuts idle/light-load package temps and fan noise dramatically on hot chips (e.g. Raptor Lake). Set to `performance` for the legacy always-max behavior.
  - `BOOST_PL1_PCT` / `BOOST_PL2_PCT` (defaults `100` / `80`) — scale the RAPL sustained/burst power limits applied by Boost. Capping PL2 to 80% trades a few percent of all-core burst throughput for a large peak-temperature drop.
- `set_cpu_profile` accepts an optional EPP override applied after power-profiles-daemon sets its profile (ppd `performance` otherwise forces `EPP=performance`).
- RAPL guard: scaled PL2 can never drop below PL1.

## [1.7.2] - 2026-07-06

### Fixed
- **Eco/Default mode reverting to Performance** — `silent` and `restore` never disabled the auto daemon like `boost`/`powersave` did. If auto mode (e.g. `dynamic`) was still active, the daemon's next tick would re-apply its own profile decision on top, flipping the UI back to Performance seconds after manually selecting Eco Mode or Default. Both scripts now call `disable_auto_for_manual_profile` like the other profile commands.
- **Silent profile switch to Performance when power-profiles-daemon rejects the request** — `set_cpu_profile` returned immediately after calling `powerprofilesctl set`, even when that call failed (e.g. `Device or resource busy` on hybrid CPUs), leaving the governor/EPP unchanged while the script reported success. It now falls back to the manual governor/EPP write path whenever the ppd call fails.
- **Keyboard/mouse becoming unresponsive after switching to Eco/Balanced mode** — `set_usb_autosuspend` forced `power/control=auto` on every USB device, including keyboard/mouse dongles and receivers. Kernel USB autosuspend can leave these devices asleep until a wake event, making input appear to freeze. HID devices (driver `usbhid`) are now always kept at `power/control=on`.

## [1.7.1] - 2026-07-02

### Fixed
- **Daemon crash resilience** — the poll loop now recovers from any per-cycle error instead of dying and waiting for a systemd restart.
- **No forced profile switch on daemon restart** — the first AC/battery poll after a (re)start only records the state; previously every `auto mode` change or service restart re-applied the AC profile and fired an "AC Power Connected" notification.
- **Zombie process cleanup** — profile commands and notifications spawned by the daemon are now reaped in the background; notification threads are daemonized.
- **Stats CSV: 0% battery** was recorded as an empty field.
- **Dashboard history parsing** — when the stats file had fewer rows than the requested window, the CSV header leaked in as a bogus data row, skewing averages and the profile-switch log.
- **Dashboard error handling** — GET/POST handlers survive client disconnects and internal errors instead of killing the connection thread with a traceback.
- **Turbo restore on AMD** — `save_originals` now records `ORIG_TURBO_TYPE` (matching the boot capture script), so `restore` interprets the saved turbo value correctly on AMD/cpufreq platforms.
- **Originals file is parsed, not sourced** — a malformed value can no longer abort a profile switch or restore.
- **Config writer hardening** — `set_config_value`/`set_auto_config_value` escape sed metacharacters (`\ & |`); `read_safe_config` refuses to clobber shell-critical variables (`PATH`, `IFS`, `LD_*`, …).
- **`auto snooze` input validation** — garbage durations no longer produce arithmetic errors or corrupt snooze state (falls back to 2h).
- **ac-event session detection** — prefers the *active* login session instead of the first listed one, fixing notifications when multiple sessions exist.

## [1.7.0] - 2026-06-26

### Added
- **Screen lock → silent Eco Mode** — when the GNOME screen locks, daemon silently switches to Eco Mode (no notification, no fan noise). Restores Performance automatically on unlock. Configurable via `SCREEN_LOCK_POWERSAVE=yes/no`.
- **Battery charge limit** — `BATTERY_CHARGE_LIMIT=80` writes `charge_control_end_threshold` on Apple Silicon (and compatible hardware). Protects battery longevity when permanently plugged in. Default `0` (disabled, charges to 100%).
- **Process detection O(1)** — replaced 3 separate `pgrep` subprocess calls with a single `/proc/*/comm` read, cached per poll cycle. Reduces subprocess overhead by ~60%.

### Changed
- **Meeting mode on battery** — when a video call is detected while on battery power, daemon now auto-switches to Eco Mode silently (instead of showing a suggestion). On AC power, behaviour is unchanged (suggestion with action button).

### Fixed
- **Dashboard config validation** — rejects invalid numbers, times, enum values, and unsafe threshold combinations before writing `/etc/boost-auto.conf`.
- **Dashboard CSRF hardening** — validates parsed local origins exactly instead of prefix matching and returns clean 400/413 errors for malformed POST bodies.
- **Daemon config resilience** — ignores invalid manual config values per key instead of aborting the whole config reload.
- **Dashboard telemetry escaping** — escapes CSV-derived values before inserting history tables into the page.

## [1.6.0] - 2026-06-26

### Added
- **Slow charge protection** — auto daemon now detects when AC is connected but net charging rate is too low (default < 2W, rolling 60s average). Automatically switches to Eco Mode so the charger can keep up with system load. Restores the AC profile once battery recovers to 35%. Configurable via `SLOW_CHARGE_THRESHOLD_W`, `SLOW_CHARGE_BATTERY_PCT`, `SLOW_CHARGE_RECOVERY_PCT` in `boost-auto.conf`.

## [1.5.0] - 2026-06-26

### Added
- **Boot-time profile init** — new `boost-ac-init.service` runs `ac-event` at boot, applying the correct AC or battery profile automatically. Previously, the profile was only applied on plug/unplug events, not at startup.
- **Default `AC_PROFILE=boost`** — on AC power, Boost profile is applied by default. Previously defaulted to `restore`.

### Fixed
- **Tray profile label mismatch** — "Profile: Boost" now correctly shows "Profile: Performance" when the performance profile is active. `power-saver` now shows "Eco Mode" instead of "Powersave", matching the menu labels.

## [1.3.0] - 2026-06-25

### Added
- **AMD GPU support** — power limit scaling via `amdgpu` sysfs (`power1_cap`). Fills the gap in the "Intel + AMD + NVIDIA" claim. Automatically detected; falls back gracefully when absent.
- **Process-based workload detection** — daemon now detects creator workloads (`ffmpeg`, `blender`, `cargo build`, `make`, etc.) and video call apps (`zoom`, `teams`, `discord`, etc.), offering appropriate profile suggestions.
- **Profile switch history in web dashboard** — telemetry chart now overlays colored bands when profile changes. "Recent Switches" log shows last 5 transitions.
- **Python CI** — GitHub Actions workflow adds `ruff` linting and `pytest` test suite.
- **SECURITY.md** — responsible disclosure policy and security notes.
- **PR template** — `.github/PULL_REQUEST_TEMPLATE.md` with hardware test checklist.
- **Hardware compatibility table** in README.
- **FAQ section** in README.

### Fixed
- **CSRF protection on web server** — POST requests to `/api/action` now require matching `Origin` header. Prevents cross-origin requests from other local apps.
- **RAPL path graceful handling** — `apply_hardware_limits()` and stats recording skip RAPL silently on AMD systems instead of writing to non-existent paths.
- **Atomic state file writes** — `auto-snooze-until` and `auto-skip-date` written via tmp+rename to prevent race conditions between daemon and web server.
- **Gaming preset missing from test suite** — `tests/auto-mode-presets.sh` now verifies gaming thresholds.

### Changed
- `save_originals()` now detects and saves AMD GPU power limit alongside NVIDIA.

## [1.2.0] - 2026-06-24

### Added
- **Gaming auto mode** — quick to boost, allows higher temps (80°C), reacts in 30s of sustained load. Available via `auto mode gaming`, the web dashboard, and the tray applet.
- **`uninstall.sh`** — clean removal of all Boost components with BIOS restore before teardown.
- **`auto setup` wizard now includes Gaming mode** as option 2.

### Fixed
- `bin/summer` was calling `auto mode summer/calm` — modes that do not exist. Now correctly delegates to `auto summer-nights on/off`.
- `auto mode gaming` was silently rejected by the mode validation case statement even though gaming was listed in help text and the web dashboard.
- `lib/boost-web.py` `mode_thresholds("gaming")` returned wrong defaults — gaming case was missing entirely.
- `HWMON="/sys/class/hwmon/hwmon5"` was hardcoded in `power-common.sh`. Fan curve control now discovers the correct hwmon at runtime, making it work across different hardware.
- Web dashboard had a duplicate `id="decisionReason"` — the second element (in the Decision Engine section) never updated. Both now sync correctly.
- `lib/boost-tray.py` auto mode submenu was missing Gaming.
- Version inconsistency between files (1.1.0 vs 1.2.0) — all shell scripts now report 1.2.0.

### Removed
- `fix_auto.py` and `fix_auto2.py` (development artifacts accidentally committed).

## [1.1.0] - 2025-11-01

### Added
- Python-native boost-daemon replacing the bash loop — O(1) thermal polling, no subprocess overhead.
- Game Mode auto-detect via `pgrep` — switches to Performance automatically when Steam/Wine/Proton is detected.
- Stats CSV rotation — file capped at 250KB / ~1.5 days of history.
- Config mtime caching — daemon only re-reads `/etc/boost-auto.conf` when it changes.

### Fixed
- Daemon now caches power profile to avoid repeated `powerprofilesctl` calls.
- Ambient temperature cache TTL extended to 10 minutes to reduce sensor reads.

## [1.0.0] - 2025-09-15

### Added
- Initial release: `boost`, `powersave`, `silent`, `restore` profile commands.
- Intel RAPL PL1/PL2 dynamic scaling (60% / 40% of BIOS limits).
- NVIDIA GPU power limit scaling per profile.
- GNOME power-profiles-daemon integration (syncs GNOME Power Mode indicator).
- Web dashboard at `http://localhost:8765` with live telemetry chart.
- System tray applet (GTK3 + AyatanaAppIndicator3).
- Smart auto modes: dynamic, creator, quiet.
- Quiet hours and snooze controls.
- Summer silent-nights mode (auto Eco overnight in warm rooms).
- Bash tab completion for all commands.
- Systemd service units with udev AC event trigger.
