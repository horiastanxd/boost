# Changelog

All notable changes to Boost are documented here.

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
