"""Tests for the platform layer.

The Windows backend is exercised from Linux by faking `run`, because the whole
point of the layer is that the decision-making (which powercfg alias, which
fallback, which clamp) is separable from the platform it runs on.
"""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(_LIB))

import boost_paths  # noqa: E402
import platform_backend  # noqa: E402
import platform_windows  # noqa: E402


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class PathsTest(unittest.TestCase):
    """The Linux paths are a compatibility contract: they must not move."""

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux paths")
    def test_linux_paths_are_the_historical_ones(self):
        self.assertEqual(str(boost_paths.CONF_FILE), "/etc/boost-auto.conf")
        self.assertEqual(str(boost_paths.STATE_DIR), "/var/lib/power-profile")
        self.assertEqual(str(boost_paths.STATS_FILE), "/var/lib/power-profile/stats.csv")
        self.assertEqual(str(boost_paths.LIVE_FILE), "/var/lib/power-profile/live.json")
        self.assertEqual(str(boost_paths.SNOOZE_FILE), "/var/lib/power-profile/auto-snooze-until")
        self.assertEqual(str(boost_paths.SKIP_TODAY_FILE), "/var/lib/power-profile/auto-skip-date")
        self.assertEqual(str(boost_paths.SILENT_PENDING_FILE), "/var/lib/power-profile/silent-pending")
        self.assertEqual(
            str(boost_paths.LATEST_REPORT), "/var/lib/power-profile/reports/latest.html"
        )

    def test_web_dashboard_uses_the_shared_paths(self):
        spec = importlib.util.spec_from_file_location("boost_web_paths", _LIB / "boost-web.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.CONF_FILE, boost_paths.CONF_FILE)
        self.assertEqual(module.STATS_FILE, boost_paths.STATS_FILE)
        self.assertEqual(module.LIVE_FILE, boost_paths.LIVE_FILE)
        self.assertEqual(module.SILENT_PENDING_FILE, boost_paths.SILENT_PENDING_FILE)


class LinuxBackendTest(unittest.TestCase):
    def setUp(self):
        self.backend = platform_backend.LinuxBackend()

    def test_capabilities(self):
        self.assertTrue(self.backend.reads_sysfs)
        self.assertTrue(self.backend.supports_fan_control)
        self.assertTrue(self.backend.supports_auto_daemon)

    def test_silent_runs_with_auto_so_the_interlock_can_queue(self):
        with patch.object(platform_backend, "run", return_value=_completed()) as fake:
            self.backend.apply_profile("silent")
        self.assertEqual(fake.call_args[0][0], ["/usr/local/bin/silent", "--auto"])

    def test_each_profile_maps_to_its_own_command(self):
        for profile, expected in (
            ("boost", ["/usr/local/bin/boost"]),
            ("powersave", ["/usr/local/bin/powersave"]),
            ("restore", ["/usr/local/bin/restore"]),
        ):
            with patch.object(platform_backend, "run", return_value=_completed()) as fake:
                result = self.backend.apply_profile(profile)
            self.assertEqual(fake.call_args[0][0], expected)
            self.assertTrue(result["ok"])

    def test_queued_silent_reports_the_interlock_message(self):
        stdout = "[SILENT] Not applying now: CPU at 84C\n[SILENT] Silent queued until it cools\n"
        with patch.object(platform_backend, "run", return_value=_completed(stdout=stdout)):
            result = self.backend.apply_profile("silent")
        self.assertTrue(result["ok"])
        self.assertTrue(result["queued"])
        self.assertIn("queued", result["message"])
        self.assertNotIn("[SILENT]", result["message"])

    def test_failure_surfaces_the_last_error_line(self):
        with patch.object(
            platform_backend, "run",
            return_value=_completed(returncode=1, stderr="something\nreal reason here"),
        ):
            result = self.backend.apply_profile("boost")
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "real reason here")

    def test_unknown_profile_is_refused(self):
        self.assertFalse(self.backend.apply_profile("turbo")["ok"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux backend selection")
    def test_get_backend_picks_linux(self):
        self.assertIsInstance(platform_backend.get_backend(), platform_backend.LinuxBackend)


class NvidiaSmiTest(unittest.TestCase):
    def test_stats_parse(self):
        out = "115.20, 180.00, 61, 42\n"
        with patch.object(platform_backend, "run", return_value=_completed(stdout=out)):
            stats = platform_backend._nvidia_smi_stats()
        self.assertEqual(stats["power"], "115.20")
        self.assertEqual(stats["limit"], "180.00")
        self.assertEqual(stats["temp"], "61")
        self.assertEqual(stats["vendor"], "nvidia")

    def test_missing_gpu_is_blank_not_an_error(self):
        with patch.object(platform_backend, "run", side_effect=OSError("no nvidia-smi")):
            stats = platform_backend._nvidia_smi_stats()
        self.assertEqual(stats["vendor"], "none")
        self.assertEqual(stats["power"], "")

    def test_limit_range_parse(self):
        with patch.object(platform_backend, "run", return_value=_completed(stdout="100.00, 320.00\n")):
            self.assertEqual(platform_backend._nvidia_smi_limit_range(), (100, 320))

    def test_limit_range_unknown_is_zero(self):
        with patch.object(platform_backend, "run", return_value=_completed(returncode=9)):
            self.assertEqual(platform_backend._nvidia_smi_limit_range(), (0, 0))


class WindowsBackendTest(unittest.TestCase):
    def setUp(self):
        self.backend = platform_windows.WindowsBackend()

    def test_windows_declares_what_it_cannot_do(self):
        self.assertFalse(self.backend.reads_sysfs)
        self.assertFalse(self.backend.supports_fan_control)
        self.assertFalse(self.backend.supports_auto_daemon)
        self.assertFalse(self.backend.supports_rapl)
        self.assertIn("not supported on Windows", self.backend.unsupported("Fan control")["message"])

    def test_every_profile_has_a_plan_and_uses_aliases_not_guids(self):
        self.assertEqual(set(platform_windows.PROFILE_PLANS), {"boost", "powersave", "silent"})
        for scheme, overlay, _ in platform_windows.PROFILE_PLANS.values():
            # OEM images ship their own scheme GUIDs, so only aliases are safe.
            self.assertFalse(platform_windows._GUID_RE.search(scheme))
            self.assertFalse(platform_windows._GUID_RE.search(overlay))

    def test_boost_activates_the_high_performance_scheme(self):
        calls = []

        def fake_run(cmd, timeout=4.0):
            calls.append(cmd)
            return _completed()

        with patch.object(platform_windows, "run", fake_run), \
             patch.object(platform_windows, "_remember_original_scheme"):
            result = self.backend.apply_profile("boost")
        self.assertTrue(result["ok"])
        self.assertIn(["powercfg", "/setactive", "SCHEME_MIN"], calls)
        self.assertTrue(
            any("PERFBOOSTMODE" in c and str(platform_windows.BOOST_AGGRESSIVE) in c for c in calls)
        )

    def test_falls_back_to_the_power_mode_overlay_when_the_scheme_is_missing(self):
        """Windows 11 often ships Balanced only, exposing modes as overlays."""
        calls = []

        def fake_run(cmd, timeout=4.0):
            calls.append(cmd)
            if cmd[1] == "/setactive" and cmd[2].startswith("SCHEME_M"):
                return _completed(returncode=1, stderr="does not exist")
            return _completed()

        with patch.object(platform_windows, "run", fake_run), \
             patch.object(platform_windows, "_remember_original_scheme"):
            result = self.backend.apply_profile("silent")
        self.assertTrue(result["ok"])
        self.assertIn("power mode", result["message"])
        self.assertIn(["powercfg", "/overlaysetactive", "OVERLAY_SCHEME_MIN"], calls)

    def test_a_refused_powercfg_asks_for_elevation(self):
        with patch.object(platform_windows, "run", return_value=_completed(returncode=1)), \
             patch.object(platform_windows, "_remember_original_scheme"):
            result = self.backend.apply_profile("boost")
        self.assertFalse(result["ok"])
        self.assertIn("elevated", result["message"])

    def test_unknown_profile_is_refused(self):
        self.assertFalse(self.backend.apply_profile("ludicrous")["ok"])

    def test_restore_puts_back_the_remembered_scheme(self):
        guid = "381b4222-f694-41f0-9685-ff5bb260df2e"
        calls = []

        def fake_run(cmd, timeout=4.0):
            calls.append(cmd)
            return _completed()

        with tempfile.TemporaryDirectory() as tmp:
            remembered = Path(tmp) / "original-scheme"
            remembered.write_text(guid + "\n", encoding="utf-8")
            with patch.object(platform_windows, "run", fake_run), \
                 patch.object(platform_windows, "ORIGINAL_SCHEME_FILE", remembered):
                result = self.backend.apply_profile("restore")
        self.assertTrue(result["ok"])
        self.assertIn(["powercfg", "/setactive", guid], calls)

    def test_restore_without_a_remembered_scheme_falls_back_to_balanced(self):
        calls = []

        def fake_run(cmd, timeout=4.0):
            calls.append(cmd)
            return _completed()

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "never-written"
            with patch.object(platform_windows, "run", fake_run), \
                 patch.object(platform_windows, "ORIGINAL_SCHEME_FILE", missing):
                result = self.backend.apply_profile("restore")
        self.assertTrue(result["ok"])
        self.assertIn(["powercfg", "/setactive", "SCHEME_BALANCED"], calls)
        self.assertIn("Balanced", result["message"])

    def test_gpu_limit_is_clamped_to_the_driver_range(self):
        calls = []

        def fake_run(cmd, timeout=4.0):
            calls.append(cmd)
            return _completed()

        with patch.object(self.backend, "gpu_power_limit_range", return_value=(100, 320)), \
             patch.object(platform_windows, "run", fake_run):
            result = self.backend.set_gpu_power_limit(900)
        self.assertTrue(result["ok"])
        self.assertEqual(calls[-1], ["nvidia-smi", "-pl", "320"])
        self.assertIn("clamped", result["message"])

    def test_gpu_limit_without_a_gpu_is_refused(self):
        with patch.object(self.backend, "gpu_power_limit_range", return_value=(0, 0)):
            result = self.backend.set_gpu_power_limit(250)
        self.assertFalse(result["ok"])
        self.assertIn("No NVIDIA GPU", result["message"])

    def test_gpu_limit_permission_error_explains_elevation(self):
        with patch.object(self.backend, "gpu_power_limit_range", return_value=(100, 320)), \
             patch.object(platform_windows, "run",
                          return_value=_completed(returncode=1, stderr="Insufficient Permissions")):
            result = self.backend.set_gpu_power_limit(200)
        self.assertFalse(result["ok"])
        self.assertIn("administrator", result["message"])

    def test_thermal_zone_deci_kelvin_becomes_celsius(self):
        with patch.object(platform_windows, "run", return_value=_completed(stdout="3232\n")):
            self.assertEqual(self.backend.get_cpu_temp(), 50)  # 323.2 K

    def test_bogus_thermal_zone_readings_report_unknown(self):
        for value in ("0", "9999", "2731"):  # 0 K, 726 C, exactly 0 C
            with patch.object(platform_windows, "run", return_value=_completed(stdout=value)):
                self.assertEqual(self.backend.get_cpu_temp(), 0, value)

    def test_missing_thermal_zone_reports_unknown(self):
        with patch.object(platform_windows, "run", return_value=_completed(returncode=1)):
            self.assertEqual(self.backend.get_cpu_temp(), 0)

    def test_sensor_groups_match_the_shape_sensors_py_produces(self):
        with patch.object(self.backend, "get_cpu_temp", return_value=91), \
             patch.object(self.backend, "get_gpu_stats", return_value={"temp": "60"}):
            groups = self.backend.get_sensors()
        self.assertEqual([g["category"] for g in groups], ["cpu", "gpu"])
        cpu = groups[0]
        self.assertEqual(set(cpu), {"category", "label", "bulk", "max", "warn", "crit", "state", "sensors"})
        self.assertEqual(cpu["state"], "warn")  # 91 is over warn (85), under crit (95)
        self.assertEqual(groups[1]["state"], "ok")

    def test_unreadable_temperatures_produce_no_cards(self):
        with patch.object(self.backend, "get_cpu_temp", return_value=0), \
             patch.object(self.backend, "get_gpu_stats", return_value={"temp": ""}):
            self.assertEqual(self.backend.get_sensors(), [])


class WebActionGatingTest(unittest.TestCase):
    """Actions a platform cannot perform must be refused with a clear reason."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("boost_web_gating", _LIB / "boost-web.py")
        cls.web = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.web)

    def test_fan_actions_are_refused_without_fan_control(self):
        with patch.object(self.web.BACKEND, "supports_fan_control", False):
            result = self.web.run_action("fan-enable", "on")
        self.assertFalse(result["ok"])
        self.assertIn("Fan control", result["message"])

    def test_auto_actions_are_refused_without_the_daemon(self):
        with patch.object(self.web.BACKEND, "supports_auto_daemon", False):
            result = self.web.run_action("auto-mode", "gaming")
        self.assertFalse(result["ok"])
        self.assertIn("auto daemon", result["message"])

    def test_profile_actions_go_through_the_backend(self):
        with patch.object(self.web.BACKEND, "apply_profile",
                          return_value={"ok": True, "message": "Boost applied successfully."}) as fake:
            result = self.web.run_action("boost")
        fake.assert_called_once_with("boost")
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
