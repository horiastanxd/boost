#!/usr/bin/env python3
"""Unified hwmon sensor layer for Boost.

Enumerates every /sys/class/hwmon chip once and exposes one flat dict of
temperature readings keyed by a *stable* id (``chip:label``) instead of the
kernel's hwmonN numbering, which is assigned in probe order and therefore
reshuffles across reboots (the single most reported CoolerControl/fancontrol
config-breaking bug).

The daemon is the single poll authority: it calls get_all() once per tick and
publishes the result in the live snapshot. lib/boost-web.py only falls back to
calling this module directly when that snapshot is stale.
"""
from __future__ import annotations

import os
import time

HWMON_ROOT = "/sys/class/hwmon"

# Chip name -> component category. Prefix match, longest first.
CHIP_CATEGORIES = (
    ("coretemp", "cpu"),
    ("k10temp", "cpu"),
    ("zenpower", "cpu"),
    ("amd_energy", "cpu"),
    ("macsmc_hwmon", "cpu"),
    ("nvme", "nvme"),
    ("drivetemp", "sata"),
    ("spd5118", "ram"),
    ("jc42", "ram"),
    ("amdgpu", "gpu"),
    ("nouveau", "gpu"),
    ("i915", "gpu"),
    ("xe", "gpu"),
    ("acpitz", "board"),
    ("iwlwifi", "network"),
    ("mt7921", "network"),
    ("r8169", "network"),
    ("BAT", "battery"),
)

# Superio / embedded-controller chips whose category is decided by the label.
SUPERIO_CHIPS = ("nct6", "it87", "it86", "f71", "w836", "w837", "asus", "dell_smm", "nzxt")

# Label keyword -> category, checked in order (lowercased "in" match).
LABEL_CATEGORIES = (
    ("package id", "cpu"),
    ("tctl", "cpu"),
    ("tdie", "cpu"),
    ("tccd", "cpu"),
    ("core ", "cpu_core"),
    ("cputin", "cpu"),
    ("peci", "cpu"),
    ("vrm", "vrm"),
    ("vcore", "vrm"),
    ("mos", "vrm"),
    ("pch", "chipset"),
    ("chipset", "chipset"),
    ("systin", "board"),
    ("motherboard", "board"),
    ("junction", "gpu"),
    ("edge", "gpu"),
    ("mem", "gpu"),
    ("composite", "nvme"),
    ("dimm", "ram"),
    ("sodimm", "ram"),
)

# Per-category warn/critical fallbacks used when the chip exposes no
# tempN_max / tempN_crit of its own. Values follow the vendor guidance the
# plan calls out (NVMe warn 65C, RAM 60C, VRM 90C).
CATEGORY_LIMITS = {
    "cpu": (80, 95),
    "cpu_core": (85, 100),
    "gpu": (80, 90),
    "nvme": (65, 80),
    "sata": (55, 70),
    "ram": (60, 85),
    "vrm": (90, 105),
    "chipset": (80, 100),
    "board": (60, 80),
    "network": (80, 100),
    "battery": (50, 60),
    "aux": (80, 100),
    "other": (80, 100),
}

CATEGORY_LABELS = {
    "cpu": "CPU",
    "cpu_core": "CPU cores",
    "gpu": "GPU",
    "nvme": "NVMe",
    "sata": "SATA drive",
    "ram": "Memory",
    "vrm": "VRM",
    "chipset": "Chipset",
    "board": "Motherboard",
    "network": "Network",
    "battery": "Battery",
    "aux": "Aux",
    "other": "Other",
}

# Categories whose per-sensor cards are shown collapsed by default: dozens of
# per-core readings would otherwise drown the dashboard.
BULK_CATEGORIES = {"cpu_core", "aux"}

_RESCAN_INTERVAL = 60.0


def read_text(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return default


def read_int(path: str, default: int = 0) -> int:
    raw = read_text(path, "")
    try:
        return int(raw)
    except ValueError:
        return default


def _category_for(chip: str, label: str) -> str:
    lowered_chip = chip.lower()
    lowered_label = label.lower()
    for prefix, category in CHIP_CATEGORIES:
        if lowered_chip.startswith(prefix.lower()):
            if category == "cpu":
                for keyword, label_category in LABEL_CATEGORIES:
                    if keyword in lowered_label and label_category in ("cpu", "cpu_core"):
                        return label_category
                return "cpu"
            return category
    if any(lowered_chip.startswith(prefix) for prefix in SUPERIO_CHIPS):
        for keyword, category in LABEL_CATEGORIES:
            if keyword in lowered_label:
                return category
        if lowered_label.startswith("auxtin") or lowered_label.startswith("temp"):
            return "aux"
        return "board"
    for keyword, category in LABEL_CATEGORIES:
        if keyword in lowered_label:
            return category
    return "other"


def _chip_key(name: str, hwmon_path: str, duplicate: bool) -> str:
    """Stable chip key: the chip name, disambiguated by the *device* it hangs
    off when the same driver is bound more than once (two NVMe drives), never
    by the hwmonN index."""
    if not duplicate:
        return name
    device = os.path.join(hwmon_path, "device")
    try:
        resolved = os.path.basename(os.path.realpath(device))
    except OSError:
        resolved = ""
    return f"{name}-{resolved}" if resolved else name


def chip_map() -> dict[str, str]:
    """Return {hwmon_path: stable_chip_key} for every hwmon on the system.

    Shared with lib/fancontrol.py so a fan id and a sensor id always name the
    same chip the same way.
    """
    try:
        paths = [os.path.join(HWMON_ROOT, entry) for entry in sorted(os.listdir(HWMON_ROOT))]
    except OSError:
        return {}
    names = {path: read_text(os.path.join(path, "name"), "") for path in paths}
    counts: dict[str, int] = {}
    for name in names.values():
        counts[name] = counts.get(name, 0) + 1
    return {
        path: _chip_key(name, path, counts.get(name, 0) > 1)
        for path, name in names.items()
        if name
    }


class SensorReader:
    """Caches the hwmon topology and re-reads only the temperature inputs."""

    def __init__(self, rescan_interval: float = _RESCAN_INTERVAL) -> None:
        self.rescan_interval = rescan_interval
        self._entries: list[dict] = []
        self._scanned_at = 0.0
        self._chip_count = -1

    # ── discovery ────────────────────────────────────────────────────

    def scan(self, force: bool = False) -> list[dict]:
        now = time.monotonic()
        if not force and self._entries and now - self._scanned_at < self.rescan_interval:
            return self._entries
        try:
            hwmons = sorted(os.listdir(HWMON_ROOT))
        except OSError:
            self._entries, self._scanned_at = [], now
            return self._entries

        paths = [os.path.join(HWMON_ROOT, entry) for entry in hwmons]
        chips = chip_map()

        entries: list[dict] = []
        for path in paths:
            name = read_text(os.path.join(path, "name"), "")
            chip = chips.get(path)
            if not name or not chip:
                continue
            try:
                files = os.listdir(path)
            except OSError:
                continue
            for filename in sorted(files):
                if not (filename.startswith("temp") and filename.endswith("_input")):
                    continue
                prefix = filename[: -len("_input")]
                label = read_text(os.path.join(path, f"{prefix}_label"), "") or prefix
                category = _category_for(name, label)
                warn, crit = CATEGORY_LIMITS.get(category, CATEGORY_LIMITS["other"])
                # Chip-reported limits only ever tighten the category defaults:
                # an NVMe advertising crit=89C must still warn at the 65C the
                # drive starts thermal-throttling at, not at 84C.
                chip_max = read_int(os.path.join(path, f"{prefix}_max"), 0) // 1000
                chip_crit = read_int(os.path.join(path, f"{prefix}_crit"), 0) // 1000
                if 0 < chip_crit < 200:
                    crit = min(crit, chip_crit)
                if 0 < chip_max < 200:
                    warn = min(warn, chip_max)
                warn = min(warn, max(crit - 5, 30))
                entries.append(
                    {
                        "id": f"{chip}:{label}",
                        "chip": chip,
                        "driver": name,
                        "label": label,
                        "category": category,
                        "path": os.path.join(path, filename),
                        "warn": warn,
                        "crit": crit,
                    }
                )
        self._entries, self._scanned_at, self._chip_count = entries, now, len(paths)
        return entries

    # ── reading ──────────────────────────────────────────────────────

    def get_all(self) -> dict[str, dict]:
        """Return {sensor_id: {label, chip, category, temp, warn, crit, state}}.

        Sensors reading 0 or an implausible value are dropped: several superio
        channels are wired to nothing and would otherwise show up as 0C cards.
        """
        result: dict[str, dict] = {}
        for entry in self.scan():
            raw = read_int(entry["path"], 0)
            temp = raw / 1000.0
            if raw <= 0 or temp >= 200:
                continue
            temp_i = int(round(temp))
            if temp_i >= entry["crit"]:
                state = "critical"
            elif temp_i >= entry["warn"]:
                state = "warn"
            else:
                state = "ok"
            result[entry["id"]] = {
                "id": entry["id"],
                "chip": entry["chip"],
                "driver": entry["driver"],
                "label": entry["label"],
                "category": entry["category"],
                "temp": temp_i,
                "warn": entry["warn"],
                "crit": entry["crit"],
                "state": state,
            }
        return result

    def hottest(self, category: str, sensors: dict[str, dict] | None = None) -> int | None:
        """Hottest reading in a category, or None when nothing reports it."""
        pool = sensors if sensors is not None else self.get_all()
        temps = [item["temp"] for item in pool.values() if item["category"] == category]
        return max(temps) if temps else None


_DEFAULT_READER = SensorReader()


def get_all() -> dict[str, dict]:
    return _DEFAULT_READER.get_all()


def hottest(category: str, sensors: dict[str, dict] | None = None) -> int | None:
    return _DEFAULT_READER.hottest(category, sensors)


def group_by_category(sensors: dict[str, dict]) -> list[dict]:
    """Collapse readings into one card per component category."""
    groups: dict[str, dict] = {}
    for item in sensors.values():
        group = groups.setdefault(
            item["category"],
            {
                "category": item["category"],
                "label": CATEGORY_LABELS.get(item["category"], item["category"]),
                "bulk": item["category"] in BULK_CATEGORIES,
                "max": item["temp"],
                "warn": item["warn"],
                "crit": item["crit"],
                "state": item["state"],
                "sensors": [],
            },
        )
        group["sensors"].append(item)
        if item["temp"] > group["max"]:
            group["max"] = item["temp"]
            group["warn"] = item["warn"]
            group["crit"] = item["crit"]
        order = {"ok": 0, "warn": 1, "critical": 2}
        if order[item["state"]] > order[group["state"]]:
            group["state"] = item["state"]
    for group in groups.values():
        group["sensors"].sort(key=lambda item: (-item["temp"], item["label"]))
    priority = ["cpu", "gpu", "nvme", "sata", "ram", "vrm", "chipset", "board", "cpu_core"]
    return sorted(
        groups.values(),
        key=lambda group: (
            priority.index(group["category"]) if group["category"] in priority else len(priority),
            group["label"],
        ),
    )


def missing_drivers(sensors: dict[str, dict] | None = None) -> list[dict]:
    """Report hardware present without a loaded hwmon driver, plus the fix.

    Only reports what this machine actually has: DDR5 SPD hubs on an SMBus,
    SATA disks without drivetemp. Everything else stays quiet so `auto doctor`
    never nags about sensors the box cannot have.
    """
    pool = sensors if sensors is not None else get_all()
    categories = {item["category"] for item in pool.values()}
    missing: list[dict] = []

    if "ram" not in categories and os.path.isdir("/sys/bus/i2c/devices"):
        try:
            has_spd = any(
                read_text(os.path.join("/sys/bus/i2c/devices", entry, "name"), "").startswith("spd")
                or entry.endswith("-0050")
                for entry in os.listdir("/sys/bus/i2c/devices")
            )
        except OSError:
            has_spd = False
        if has_spd:
            missing.append(
                {
                    "component": "RAM (DDR5 DIMM)",
                    "module": "spd5118",
                    "fix": "sudo modprobe spd5118",
                }
            )

    if "sata" not in categories:
        try:
            sata_disks = [name for name in os.listdir("/sys/block") if name.startswith("sd")]
        except OSError:
            sata_disks = []
        if sata_disks:
            missing.append(
                {
                    "component": f"SATA drive temps ({', '.join(sorted(sata_disks))})",
                    "module": "drivetemp",
                    "fix": "sudo modprobe drivetemp",
                }
            )

    if "board" not in categories and "vrm" not in categories:
        missing.append(
            {
                "component": "Motherboard / VRM sensors",
                "module": "nct6775 (or it87)",
                "fix": "sudo sensors-detect --auto && sudo modprobe nct6775",
            }
        )
    return missing


def _main() -> int:
    import json
    import sys

    command = sys.argv[1] if len(sys.argv) > 1 else "list"
    sensors = get_all()
    if command == "json":
        print(json.dumps({"sensors": sensors, "groups": group_by_category(sensors)}, indent=2))
    elif command == "missing":
        for item in missing_drivers(sensors):
            print(f"{item['component']}: missing {item['module']} -> {item['fix']}")
    else:
        for group in group_by_category(sensors):
            print(f"{group['label']:<12} {group['max']:>3}C  ({group['state']})")
            for item in group["sensors"]:
                print(f"    {item['id']:<40} {item['temp']:>3}C  warn {item['warn']} crit {item['crit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
