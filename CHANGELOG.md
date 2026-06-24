# Changelog

All notable changes to Boost are documented here.

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
