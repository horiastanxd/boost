"""Unit tests for lib/boost-daemon.py (BoostDaemon)."""
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

# Provide a stub syslog before importing the module (syslog is Linux-only).
syslog_stub = MagicMock()
syslog_stub.LOG_PID = 1
syslog_stub.LOG_USER = 8
syslog_stub.LOG_INFO = 6
sys.modules.setdefault("syslog", syslog_stub)

import importlib.util

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")

# boost-daemon.py has a hyphen in its name so we load it via importlib.
with patch("syslog.openlog"), patch("syslog.syslog"):
    _spec = importlib.util.spec_from_file_location(
        "boost_daemon", os.path.join(_LIB, "boost-daemon.py")
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["boost_daemon"] = _mod
    _spec.loader.exec_module(_mod)

import boost_daemon  # noqa: E402
from boost_daemon import BoostDaemon  # noqa: E402


def _make_daemon(**overrides):
    """Return a BoostDaemon with filesystem probing stubbed out."""
    with patch("syslog.openlog"), patch("syslog.syslog"), patch.object(
        BoostDaemon, "find_cpu_temp_path", return_value=None
    ):
        d = BoostDaemon()
    for k, v in overrides.items():
        setattr(d, k, v)
    return d


class TestInQuietHours(unittest.TestCase):
    """in_quiet_hours() - overnight span (22:00-08:00) and same-day span (09:00-17:00)."""

    def _daemon(self, start, end):
        d = _make_daemon()
        d.quiet_start = start
        d.quiet_end = end
        return d

    def _patch_now(self, hour, minute):
        dt = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        return patch("boost_daemon.datetime", wraps=datetime, now=lambda: dt), dt

    # Helper that patches datetime.now inside the module
    def _at(self, hour, minute):
        dt = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        m = MagicMock(wraps=datetime)
        m.now.return_value = dt
        return patch("boost_daemon.datetime", m)

    # --- overnight span (22:00 - 08:00) ---

    def test_overnight_inside_before_midnight(self):
        d = self._daemon("22:00", "08:00")
        with self._at(23, 0):
            self.assertTrue(d.in_quiet_hours())

    def test_overnight_inside_after_midnight(self):
        d = self._daemon("22:00", "08:00")
        with self._at(2, 30):
            self.assertTrue(d.in_quiet_hours())

    def test_overnight_at_start(self):
        d = self._daemon("22:00", "08:00")
        with self._at(22, 0):
            self.assertTrue(d.in_quiet_hours())

    def test_overnight_at_end_is_outside(self):
        # End boundary is exclusive
        d = self._daemon("22:00", "08:00")
        with self._at(8, 0):
            self.assertFalse(d.in_quiet_hours())

    def test_overnight_outside_midday(self):
        d = self._daemon("22:00", "08:00")
        with self._at(14, 0):
            self.assertFalse(d.in_quiet_hours())

    def test_overnight_just_before_end(self):
        d = self._daemon("22:00", "08:00")
        with self._at(7, 59):
            self.assertTrue(d.in_quiet_hours())

    # --- same-day span (09:00 - 17:00) ---

    def test_sameday_inside(self):
        d = self._daemon("09:00", "17:00")
        with self._at(13, 0):
            self.assertTrue(d.in_quiet_hours())

    def test_sameday_at_start(self):
        d = self._daemon("09:00", "17:00")
        with self._at(9, 0):
            self.assertTrue(d.in_quiet_hours())

    def test_sameday_at_end_is_outside(self):
        d = self._daemon("09:00", "17:00")
        with self._at(17, 0):
            self.assertFalse(d.in_quiet_hours())

    def test_sameday_outside_before(self):
        d = self._daemon("09:00", "17:00")
        with self._at(8, 59):
            self.assertFalse(d.in_quiet_hours())

    def test_sameday_outside_after(self):
        d = self._daemon("09:00", "17:00")
        with self._at(17, 1):
            self.assertFalse(d.in_quiet_hours())

    # --- equal start/end means never quiet ---

    def test_equal_start_end_never_quiet(self):
        d = self._daemon("08:00", "08:00")
        with self._at(8, 0):
            self.assertFalse(d.in_quiet_hours())


class TestSuggestionsPaused(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _paths(self, d):
        """Redirect daemon state files into tmp dir."""
        import boost_daemon as bd
        d._snooze_cache = (0, 0, False)
        self._orig_snooze = bd.SNOOZE_FILE
        self._orig_skip = bd.SKIP_TODAY_FILE
        bd.SNOOZE_FILE = os.path.join(self.tmp, "snooze")
        bd.SKIP_TODAY_FILE = os.path.join(self.tmp, "skip-date")
        return bd.SNOOZE_FILE, bd.SKIP_TODAY_FILE

    def tearDown(self):
        import boost_daemon as bd
        if hasattr(self, "_orig_snooze"):
            bd.SNOOZE_FILE = self._orig_snooze
            bd.SKIP_TODAY_FILE = self._orig_skip

    def test_mode_off_pauses(self):
        d = _make_daemon(mode="off")
        self._paths(d)
        self.assertTrue(d.suggestions_paused())

    def test_mode_quiet_pauses(self):
        d = _make_daemon(mode="quiet")
        self._paths(d)
        self.assertTrue(d.suggestions_paused())

    def test_snooze_file_future_pauses(self):
        d = _make_daemon(mode="dynamic")
        snooze_path, _ = self._paths(d)
        future = int(time.time()) + 7200
        with open(snooze_path, "w") as f:
            f.write(str(future))
        with patch.object(d, "in_quiet_hours", return_value=False):
            self.assertTrue(d.suggestions_paused())

    def test_skip_today_file_pauses(self):
        d = _make_daemon(mode="dynamic")
        _, skip_path = self._paths(d)
        with open(skip_path, "w") as f:
            f.write(datetime.now().strftime("%Y-%m-%d"))
        with patch.object(d, "in_quiet_hours", return_value=False):
            self.assertTrue(d.suggestions_paused())

    def test_active_state_not_paused(self):
        d = _make_daemon(mode="dynamic")
        self._paths(d)
        with patch.object(d, "in_quiet_hours", return_value=False):
            self.assertFalse(d.suggestions_paused())


class TestApplyPreset(unittest.TestCase):
    def test_dynamic(self):
        d = _make_daemon(mode="dynamic")
        d.apply_preset()
        self.assertEqual(d.temp_hot, 78)
        self.assertEqual(d.boost_temp_limit, 78)
        self.assertEqual(d.load_high, 75)
        self.assertEqual(d.load_high_duration, 120)
        self.assertEqual(d.load_idle, 8)
        self.assertEqual(d.load_idle_duration, 600)
        self.assertEqual(d.prompt_cooldown, 900)

    def test_gaming(self):
        d = _make_daemon(mode="gaming")
        d.apply_preset()
        self.assertEqual(d.temp_hot, 80)
        self.assertEqual(d.boost_temp_limit, 80)
        self.assertEqual(d.load_high, 50)
        self.assertEqual(d.load_high_duration, 30)
        self.assertEqual(d.load_idle, 10)
        self.assertEqual(d.load_idle_duration, 600)
        self.assertEqual(d.prompt_cooldown, 900)

    def test_creator(self):
        d = _make_daemon(mode="creator")
        d.apply_preset()
        self.assertEqual(d.temp_hot, 82)
        self.assertEqual(d.boost_temp_limit, 82)
        self.assertEqual(d.load_high, 85)
        self.assertEqual(d.load_high_duration, 30)
        self.assertEqual(d.load_idle, 15)
        self.assertEqual(d.load_idle_duration, 1200)
        self.assertEqual(d.prompt_cooldown, 300)

    def test_quiet(self):
        d = _make_daemon(mode="quiet")
        d.apply_preset()
        self.assertEqual(d.temp_hot, 70)
        self.assertEqual(d.boost_temp_limit, 70)
        self.assertEqual(d.load_high, 90)
        self.assertEqual(d.load_high_duration, 600)
        self.assertEqual(d.load_idle, 5)
        self.assertEqual(d.load_idle_duration, 120)
        self.assertEqual(d.prompt_cooldown, 3600)


class TestReadConfig(unittest.TestCase):
    def test_parses_known_keys(self):
        content = "\n".join([
            "# comment",
            "AUTO_MODE=gaming",
            "TEMP_HOT=80",
            "TEMP_CRITICAL=90",
            "BOOST_TEMP_LIMIT=80",
            "LOAD_HIGH=60",
            "LOAD_IDLE=5",
            "QUIET_HOURS_START=23:00",
            "QUIET_HOURS_END=07:00",
            "SUMMER_SILENT_NIGHTS=yes",
            "ALLOW_CRITICAL_AUTO=no",
        ])
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(content)
            path = f.name

        import boost_daemon as bd
        orig = bd.CONF_FILE
        bd.CONF_FILE = path
        try:
            d = _make_daemon()
            d.read_config()
            self.assertEqual(d.mode, "gaming")
            self.assertEqual(d.temp_hot, 80)
            self.assertEqual(d.temp_critical, 90)
            self.assertEqual(d.boost_temp_limit, 80)
            self.assertEqual(d.load_high, 60)
            self.assertEqual(d.load_idle, 5)
            self.assertEqual(d.quiet_start, "23:00")
            self.assertEqual(d.quiet_end, "07:00")
            self.assertEqual(d.summer_nights, "yes")
            self.assertEqual(d.allow_critical, "no")
        finally:
            bd.CONF_FILE = orig
            os.unlink(path)

    def test_missing_file_is_noop(self):
        import boost_daemon as bd
        orig = bd.CONF_FILE
        bd.CONF_FILE = "/nonexistent/path/boost-auto.conf"
        try:
            d = _make_daemon()
            d.mode = "dynamic"
            d.read_config()
            self.assertEqual(d.mode, "dynamic")
        finally:
            bd.CONF_FILE = orig

    def test_ignores_blank_and_comment_lines(self):
        content = "\n# full comment line\n\nAUTO_MODE=quiet\n"
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(content)
            path = f.name

        import boost_daemon as bd
        orig = bd.CONF_FILE
        bd.CONF_FILE = path
        try:
            d = _make_daemon()
            d.read_config()
            self.assertEqual(d.mode, "quiet")
        finally:
            bd.CONF_FILE = orig
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
