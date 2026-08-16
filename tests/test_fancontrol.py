"""Unit tests for lib/fancontrol.py and lib/sensors.py.

Everything runs against a fake /sys/class/hwmon tree under a temp dir, so the
tests exercise real reads and writes without touching the machine's fans.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(_LIB))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _LIB / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sensors = _load("sensors", "sensors.py")
fancontrol = _load("fancontrol", "fancontrol.py")


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value))


class FakeHwmon:
    """A minimal /sys/class/hwmon with a superio chip and two NVMe drives."""

    def __init__(self, root: Path):
        self.root = root
        hwmon0 = root / "hwmon0"
        _write(hwmon0 / "name", "coretemp")
        _write(hwmon0 / "temp1_label", "Package id 0")
        _write(hwmon0 / "temp1_input", 65000)
        _write(hwmon0 / "temp2_label", "Core 0")
        _write(hwmon0 / "temp2_input", 63000)

        hwmon1 = root / "hwmon1"
        _write(hwmon1 / "name", "nct6798")
        _write(hwmon1 / "temp1_label", "SYSTIN")
        _write(hwmon1 / "temp1_input", 35000)
        for index in (1, 2):
            _write(hwmon1 / f"pwm{index}", 128)
            _write(hwmon1 / f"pwm{index}_enable", 5)
            _write(hwmon1 / f"fan{index}_input", 900)

        hwmon2 = root / "hwmon2"
        _write(hwmon2 / "name", "nvme")
        _write(hwmon2 / "temp1_label", "Composite")
        _write(hwmon2 / "temp1_input", 40000)
        _write(hwmon2 / "temp1_crit", 84000)


class HwmonTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "hwmon"
        FakeHwmon(root)
        self._orig_root = sensors.HWMON_ROOT
        sensors.HWMON_ROOT = str(root)
        sensors._DEFAULT_READER = sensors.SensorReader()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self._orig_paths = (
            fancontrol.CONFIG_FILE, fancontrol.ORIGINAL_ENABLE_FILE,
            fancontrol.OVERRIDE_FILE, fancontrol.PAUSE_FILE, fancontrol.CALIBRATION_FILE,
        )
        fancontrol.CONFIG_FILE = str(self.state / "boost-fans.json")
        fancontrol.ORIGINAL_ENABLE_FILE = str(self.state / "orig-enable.json")
        fancontrol.OVERRIDE_FILE = str(self.state / "override.json")
        fancontrol.PAUSE_FILE = str(self.state / "pause")
        fancontrol.CALIBRATION_FILE = str(self.state / "calibration.json")
        self.hwmon_root = root

    def tearDown(self):
        sensors.HWMON_ROOT = self._orig_root
        sensors._DEFAULT_READER = sensors.SensorReader()
        (fancontrol.CONFIG_FILE, fancontrol.ORIGINAL_ENABLE_FILE, fancontrol.OVERRIDE_FILE,
         fancontrol.PAUSE_FILE, fancontrol.CALIBRATION_FILE) = self._orig_paths
        self.tmp.cleanup()


class TestSensors(HwmonTestCase):
    def test_stable_ids_use_chip_names_not_hwmon_numbers(self):
        ids = set(sensors.get_all())
        self.assertIn("coretemp:Package id 0", ids)
        self.assertIn("nct6798:SYSTIN", ids)
        self.assertTrue(all("hwmon" not in sensor_id for sensor_id in ids))

    def test_categories(self):
        values = sensors.get_all()
        self.assertEqual(values["coretemp:Package id 0"]["category"], "cpu")
        self.assertEqual(values["coretemp:Core 0"]["category"], "cpu_core")
        self.assertEqual(values["nvme:Composite"]["category"], "nvme")
        self.assertEqual(values["nct6798:SYSTIN"]["category"], "board")

    def test_zero_and_implausible_readings_are_dropped(self):
        _write(self.hwmon_root / "hwmon1" / "temp2_label", "AUXTIN0")
        _write(self.hwmon_root / "hwmon1" / "temp2_input", 0)
        sensors._DEFAULT_READER = sensors.SensorReader()
        self.assertNotIn("nct6798:AUXTIN0", sensors.get_all())

    def test_chip_limits_only_tighten_defaults(self):
        # NVMe advertises crit=84C but the category warns at 65C.
        self.assertEqual(sensors.get_all()["nvme:Composite"]["warn"], 65)

    def test_hottest_by_category(self):
        self.assertEqual(sensors.hottest("cpu"), 65)
        self.assertIsNone(sensors.hottest("ram"))

    def test_group_by_category(self):
        groups = {g["category"]: g for g in sensors.group_by_category(sensors.get_all())}
        self.assertEqual(groups["cpu"]["max"], 65)
        self.assertEqual(groups["cpu"]["state"], "ok")


class TestCurveValidation(unittest.TestCase):
    def test_accepts_a_sane_curve(self):
        curve, error = fancontrol.validate_curve([[40, 20], [70, 60], [90, 100]])
        self.assertIsNone(error)
        self.assertEqual(curve[-1], [90, 100])

    def test_rejects_falling_curve(self):
        _, error = fancontrol.validate_curve([[40, 60], [70, 30], [90, 100]])
        self.assertIn("cannot go down", error)

    def test_rejects_missing_hot_tail(self):
        _, error = fancontrol.validate_curve([[40, 20], [70, 60]])
        self.assertIn("80%", error)

    def test_rejects_quiet_hot_tail(self):
        _, error = fancontrol.validate_curve([[40, 10], [90, 40]])
        self.assertIn("80%", error)

    def test_rejects_single_point(self):
        _, error = fancontrol.validate_curve([[90, 100]])
        self.assertIn("two points", error)

    def test_interpolates(self):
        points = [[40, 20], [80, 100]]
        self.assertEqual(fancontrol.curve_pwm(points, 30), 20)
        self.assertEqual(fancontrol.curve_pwm(points, 60), 60)
        self.assertEqual(fancontrol.curve_pwm(points, 95), 100)

    def test_presets_respect_min_pwm_and_stay_valid(self):
        for name in fancontrol.PRESET_SHAPES:
            curve = fancontrol.preset_curve(name, 30)
            self.assertGreaterEqual(curve[0][1], 30)
            _, error = fancontrol.validate_curve(curve)
            self.assertIsNone(error, f"{name}: {error}")


class TestGuardFloor(unittest.TestCase):
    thresholds = {"tempHot": 78, "tempCritical": 85}

    def _sensors(self, **temps):
        return {
            f"chip:{name}": {"category": name, "temp": value, "id": f"chip:{name}"}
            for name, value in temps.items()
        }

    def test_cool_machine_has_no_floor(self):
        floor, reason = fancontrol.guard_floor(self._sensors(cpu=50), self.thresholds)
        self.assertEqual(floor, 0)
        self.assertEqual(reason, "")

    def test_hot_cpu_forces_the_fans_up(self):
        floor, reason = fancontrol.guard_floor(self._sensors(cpu=82), self.thresholds)
        self.assertEqual(floor, 85)
        self.assertIn("82", reason)

    def test_critical_cpu_forces_full_speed(self):
        floor, _ = fancontrol.guard_floor(self._sensors(cpu=90), self.thresholds)
        self.assertEqual(floor, 100)

    def test_hot_nvme_raises_the_floor(self):
        floor, reason = fancontrol.guard_floor(self._sensors(cpu=40, nvme=67), self.thresholds)
        self.assertEqual(floor, 60)
        self.assertIn("NVMe", reason)

    def test_worst_component_wins(self):
        floor, _ = fancontrol.guard_floor(self._sensors(cpu=82, gpu=88, nvme=40), self.thresholds)
        self.assertEqual(floor, 100)


class TestEngine(HwmonTestCase):
    thresholds = {"tempHot": 78, "tempCritical": 85}

    def _enabled_engine(self):
        config = fancontrol.default_config()
        config["enabled"] = True
        ok, error = fancontrol.save_config(config)
        self.assertTrue(ok, error)
        engine = fancontrol.FanEngine()
        engine._check_conflict = lambda: None
        return engine

    def _set_cpu(self, celsius):
        _write(self.hwmon_root / "hwmon0" / "temp1_input", int(celsius * 1000))
        _write(self.hwmon_root / "hwmon0" / "temp2_input", int(celsius * 1000))
        sensors._DEFAULT_READER = sensors.SensorReader()

    def test_discovery_finds_both_channels(self):
        ids = [channel.id for channel in fancontrol.discover_channels()]
        self.assertEqual(ids, ["nct6798:pwm1", "nct6798:pwm2"])

    def test_disabled_engine_never_writes(self):
        engine = fancontrol.FanEngine()
        engine._check_conflict = lambda: None
        before = (self.hwmon_root / "hwmon1" / "pwm1").read_text()
        engine.tick(sensors.get_all(), "balanced", self.thresholds)
        self.assertEqual((self.hwmon_root / "hwmon1" / "pwm1").read_text(), before)

    def test_engine_takes_over_and_writes_pwm(self):
        engine = self._enabled_engine()
        self._set_cpu(50)
        status = engine.tick(sensors.get_all(), "balanced", self.thresholds)
        self.assertTrue(status["fans"][0]["controlled"])
        self.assertEqual((self.hwmon_root / "hwmon1" / "pwm1_enable").read_text(), "1")
        saved = json.loads(Path(fancontrol.ORIGINAL_ENABLE_FILE).read_text())
        self.assertEqual(saved["nct6798:pwm1"]["enable"], 5)

    def test_silent_curve_is_overridden_when_the_cpu_is_hot(self):
        engine = self._enabled_engine()
        self._set_cpu(40)
        for _ in range(12):        # let the step limiter settle
            engine.tick(sensors.get_all(), "power-saver", self.thresholds)
        quiet = engine.last_status["fans"][0]["pwm"]
        self._set_cpu(90)
        for _ in range(12):
            status = engine.tick(sensors.get_all(), "power-saver", self.thresholds)
        self.assertTrue(status["guard"]["active"])
        self.assertEqual(status["fans"][0]["pwm"], 100)
        self.assertGreater(status["fans"][0]["pwm"], quiet)
        self.assertIn("critical", status["guard"]["reason"])

    def test_step_limit_caps_how_fast_a_fan_ramps(self):
        engine = self._enabled_engine()
        self._set_cpu(40)
        engine.tick(sensors.get_all(), "balanced", self.thresholds)
        first = engine.last_status["fans"][0]["pwm"]
        self._set_cpu(95)
        engine.tick(sensors.get_all(), "balanced", self.thresholds)
        second = engine.last_status["fans"][0]["pwm"]
        self.assertLessEqual(second - first, fancontrol.DEFAULT_STEP_LIMIT)

    def test_hysteresis_ignores_small_wobbles(self):
        engine = self._enabled_engine()
        self._set_cpu(60)
        engine.tick(sensors.get_all(), "balanced", self.thresholds)
        control = engine._state["nct6798:pwm1"]["temp"]
        self._set_cpu(61)   # +1C, below hyst_up
        engine.tick(sensors.get_all(), "balanced", self.thresholds)
        self.assertEqual(engine._state["nct6798:pwm1"]["temp"], control)

    def test_external_writer_is_detected_and_backed_off(self):
        engine = self._enabled_engine()
        self._set_cpu(50)
        engine.tick(sensors.get_all(), "balanced", self.thresholds)
        state = engine._state["nct6798:pwm1"]
        state["changed"] -= 5                       # pretend the write is old
        _write(self.hwmon_root / "hwmon1" / "pwm1", 7)   # somebody else wrote
        engine.tick(sensors.get_all(), "balanced", self.thresholds)
        state["changed"] -= 5
        _write(self.hwmon_root / "hwmon1" / "pwm1", 7)
        status = engine.tick(sensors.get_all(), "balanced", self.thresholds)
        self.assertEqual(status["fans"][0]["mode"], "backoff")

    def test_failsafe_restores_the_board_mode(self):
        engine = self._enabled_engine()
        self._set_cpu(50)
        engine.tick(sensors.get_all(), "balanced", self.thresholds)
        self.assertEqual((self.hwmon_root / "hwmon1" / "pwm1_enable").read_text(), "1")
        fancontrol.failsafe_all()
        self.assertEqual((self.hwmon_root / "hwmon1" / "pwm1_enable").read_text(), "5")
        self.assertFalse(os.path.exists(fancontrol.ORIGINAL_ENABLE_FILE))

    def test_paused_engine_leaves_fans_alone(self):
        engine = self._enabled_engine()
        fancontrol.pause_engine(60)
        try:
            status = engine.tick(sensors.get_all(), "balanced", self.thresholds)
        finally:
            fancontrol.resume_engine()
        self.assertEqual(status["fans"][0]["mode"], "paused")

    def test_manual_test_override_is_still_guarded(self):
        engine = self._enabled_engine()
        self._set_cpu(90)
        import time as _time
        Path(fancontrol.OVERRIDE_FILE).write_text(
            json.dumps({"fan": "nct6798:pwm1", "pwm": 10, "until": _time.time() + 30})
        )
        for _ in range(12):
            status = engine.tick(sensors.get_all(), "balanced", self.thresholds)
        self.assertEqual(status["fans"][0]["pwm"], 100)

    def test_broken_config_disables_the_engine_instead_of_raising(self):
        Path(fancontrol.CONFIG_FILE).write_text(json.dumps({
            "version": 1, "enabled": True,
            "fans": {"nct6798:pwm1": {"profiles": {"balanced": [[40, 90], [90, 10]]}}},
        }))
        config = fancontrol.load_config()
        self.assertIn("error", config)
        self.assertFalse(config["enabled"])


class TestConfigValidation(HwmonTestCase):
    def test_unknown_fan_is_rejected(self):
        _, error = fancontrol.validate_config({"fans": {"ghost:pwm9": {}}})
        self.assertIn("Unknown fan", error)

    def test_defaults_round_trip(self):
        cleaned, error = fancontrol.validate_config(fancontrol.default_config())
        self.assertIsNone(error)
        self.assertEqual(set(cleaned["fans"]), {"nct6798:pwm1", "nct6798:pwm2"})
        for fan in cleaned["fans"].values():
            self.assertEqual(set(fan["profiles"]), set(fancontrol.PROFILE_KEYS))

    def test_out_of_range_numbers_are_clamped(self):
        config = fancontrol.default_config()
        config["fans"]["nct6798:pwm1"]["step_limit"] = 5000
        config["fans"]["nct6798:pwm1"]["hyst_up"] = -4
        cleaned, error = fancontrol.validate_config(config)
        self.assertIsNone(error)
        self.assertEqual(cleaned["fans"]["nct6798:pwm1"]["step_limit"], 100)
        self.assertEqual(cleaned["fans"]["nct6798:pwm1"]["hyst_up"], 0)

    def test_calibration_updates_min_pwm_and_presets(self):
        fancontrol.save_config(fancontrol.default_config())
        ok, error = fancontrol.apply_calibration_to_config(
            {"nct6798:pwm1": {"supported": True, "min_pwm": 34, "stop_allowed": True}}
        )
        self.assertTrue(ok, error)
        config = fancontrol.load_config()
        fan = config["fans"]["nct6798:pwm1"]
        self.assertEqual(fan["min_pwm"], 34)
        self.assertTrue(fan["stop_allowed"])
        self.assertGreaterEqual(fan["profiles"]["silent"][0][1], 34)


if __name__ == "__main__":
    unittest.main()
