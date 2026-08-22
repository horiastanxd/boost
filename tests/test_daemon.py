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

from boost_daemon import BoostDaemon  # noqa: E402


def _make_daemon(**overrides):
    """Return a BoostDaemon with filesystem probing stubbed out."""
    with patch("syslog.openlog"), patch("syslog.syslog"), patch.object(
        BoostDaemon, "find_cpu_temp_path", return_value=(None, None)
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

    def test_invalid_values_do_not_abort_config_read(self):
        content = "\n".join([
            "AUTO_MODE=banana",
            "TEMP_HOT=not-a-number",
            "TEMP_CRITICAL=90",
            "QUIET_HOURS_START=25:00",
            "QUIET_HOURS_END=07:30",
            "AC_PROFILE=/bin/sh",
            "BATTERY_PROFILE=silent",
            "SLOW_CHARGE_THRESHOLD_W=bad",
        ])
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(content)
            path = f.name

        import boost_daemon as bd
        orig = bd.CONF_FILE
        bd.CONF_FILE = path
        try:
            d = _make_daemon(mode="dynamic")
            d.temp_hot = 78
            d.slow_charge_threshold_uw = 2_000_000
            d.read_config()
            self.assertEqual(d.mode, "dynamic")
            self.assertEqual(d.temp_hot, 78)
            self.assertEqual(d.temp_critical, 90)
            self.assertEqual(d.quiet_start, "22:00")
            self.assertEqual(d.quiet_end, "07:30")
            self.assertEqual(d.ac_profile, "restore")
            self.assertEqual(d.battery_profile, "silent")
            self.assertEqual(d.slow_charge_threshold_uw, 2_000_000)
        finally:
            bd.CONF_FILE = orig
            os.unlink(path)


class TestSilentInterlock(unittest.TestCase):
    """A Silent request is held back while the machine still needs cooling."""

    def _daemon(self):
        d = _make_daemon()
        d.boost_temp_limit = 78
        d.load_high = 75
        d.high_since = 0
        return d

    def test_cool_and_idle_is_allowed(self):
        blocked, reason = self._daemon().silent_interlock(45, 10, 1000)
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_hot_cpu_blocks(self):
        blocked, reason = self._daemon().silent_interlock(84, 10, 1000)
        self.assertTrue(blocked)
        self.assertIn("84", reason)

    def test_brief_load_spike_does_not_block(self):
        d = self._daemon()
        d.high_since = 995      # only 5s of high load
        blocked, _ = d.silent_interlock(50, 90, 1000)
        self.assertFalse(blocked)

    def test_sustained_load_blocks(self):
        d = self._daemon()
        d.high_since = 900      # 100s of high load
        blocked, reason = d.silent_interlock(50, 90, 1000)
        self.assertTrue(blocked)
        self.assertIn("90%", reason)


class TestPendingSilent(unittest.TestCase):
    """The queued Silent request applies itself once the machine cools down."""

    def setUp(self):
        import boost_daemon as bd
        self.bd = bd
        self.tmpdir = tempfile.mkdtemp()
        self.orig = bd.PENDING_SILENT_FILE
        bd.PENDING_SILENT_FILE = os.path.join(self.tmpdir, "silent-pending")

    def tearDown(self):
        self.bd.PENDING_SILENT_FILE = self.orig

    def _daemon(self):
        d = _make_daemon()
        d.boost_temp_limit = 78
        d.load_high = 75
        d.high_since = 0
        d.run_command = MagicMock()
        d.send_notification = MagicMock()
        return d

    def _queue(self, when=None):
        with open(self.bd.PENDING_SILENT_FILE, "w") as f:
            f.write(f"{when if when is not None else int(time.time())} the CPU is 84 C\n")

    def test_nothing_queued_does_nothing(self):
        d = self._daemon()
        d.handle_pending_silent(45, 5, int(time.time()))
        d.run_command.assert_not_called()

    def test_still_hot_keeps_the_request_queued(self):
        d = self._daemon()
        self._queue()
        d.handle_pending_silent(84, 5, int(time.time()))
        d.run_command.assert_not_called()
        self.assertTrue(os.path.exists(self.bd.PENDING_SILENT_FILE))
        d.send_notification.assert_called_once()

    def test_cooled_down_applies_silent_and_clears_the_queue(self):
        d = self._daemon()
        self._queue()
        d.handle_pending_silent(45, 5, int(time.time()))
        d.run_command.assert_called_once_with("/usr/local/bin/silent --force --auto")
        self.assertFalse(os.path.exists(self.bd.PENDING_SILENT_FILE))

    def test_stale_request_is_dropped(self):
        d = self._daemon()
        self._queue(when=int(time.time()) - self.bd.PENDING_SILENT_MAX_AGE_S - 60)
        d.handle_pending_silent(84, 90, int(time.time()))
        self.assertFalse(os.path.exists(self.bd.PENDING_SILENT_FILE))
        d.run_command.assert_not_called()


class TestStatsHeaderMigration(unittest.TestCase):
    def setUp(self):
        import boost_daemon as bd
        self.bd = bd
        self.tmpdir = tempfile.mkdtemp()
        self.orig = bd.STATS_FILE
        bd.STATS_FILE = os.path.join(self.tmpdir, "stats.csv")

    def tearDown(self):
        self.bd.STATS_FILE = self.orig

    def test_creates_the_file_with_the_component_columns(self):
        _make_daemon().ensure_stats_header()
        with open(self.bd.STATS_FILE) as f:
            self.assertEqual(f.readline().strip(), self.bd.STATS_HEADER)

    def test_widens_an_old_header_and_keeps_the_rows(self):
        old_header = "epoch,iso,profile,cpu_load,cpu_temp,gpu_temp,gpu_power,gpu_limit,pl1,pl2,governor,epp,turbo,battery_pct,battery_status"
        with open(self.bd.STATS_FILE, "w") as f:
            f.write(old_header + "\n")
            f.write("1,2026-01-01T00:00:00,balanced,5,40,0,0,0,0,0,powersave,power,OFF,,Unknown\n")
        _make_daemon().ensure_stats_header()
        with open(self.bd.STATS_FILE) as f:
            lines = f.readlines()
        self.assertEqual(lines[0].strip(), self.bd.STATS_HEADER)
        self.assertEqual(len(lines), 2)
        self.assertIn("balanced", lines[1])


if __name__ == "__main__":
    unittest.main()
