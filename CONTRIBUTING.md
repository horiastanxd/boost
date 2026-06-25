# Contributing

Contributions welcome. Keep it focused: this project does one thing (power profiles) and does it well.

## What fits

- Support for more hardware (AMD CPUs via `amd_pstate`, other SuperIO chips)
- Additional safe tunables (e.g. `vm.dirty_ratio`, CPU readahead, IRQ affinity)
- Bug fixes and safety improvements
- Better fan curve detection / hwmon discovery
- Tests

## What doesn't fit

- GUI / TUI wrappers (keep it a shell tool)
- Distro-specific packaging scripts
- Features that require persistent daemons

## How to contribute

1. Fork the repo
2. Create a branch: `git checkout -b feature/amd-pstate`
3. Make changes — test on real hardware before submitting
4. Open a PR with: what hardware you tested on, before/after temperatures

## Testing checklist

Before opening a PR, verify:

```bash
# All four commands run without errors
sudo boost
sudo powersave
echo "" | sudo silent
sudo restore

# --status works without sudo (auto-elevate)
boost --status
powersave --status
restore --status

# RAPL bounds check triggers correctly (clamps silently to max)
sudo bash -c 'source /usr/local/lib/power-common.sh; set_rapl 0 999000000'

# Fan curve backup exists after silent
ls /var/lib/power-profile/fan-curve-backup.env

# Fan curve restored after boost
sudo boost
# should print: [FAN] Smart Fan IV curve restored
```

## Hardware tested

| CPU | GPU | Distro | Status |
|-----|-----|--------|--------|
| i7-14700KF | RTX 5060 Ti | Ubuntu 24.04 | Working |

Add your hardware to the table in your PR.
