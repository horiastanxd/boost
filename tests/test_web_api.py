"""Unit tests for lib/boost-web.py - valid_hhmm, CSRF, and basic endpoints."""
import importlib.util
import io
import json
import os
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

_LIB = os.path.join(os.path.dirname(__file__), "..", "lib")

# boost-web.py has a hyphen so we load it via importlib.
_spec = importlib.util.spec_from_file_location(
    "boost_web", os.path.join(_LIB, "boost-web.py")
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["boost_web"] = _mod
_spec.loader.exec_module(_mod)

import boost_web  # noqa: E402
from boost_web import Handler, valid_hhmm  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


class TestValidHHMM(unittest.TestCase):
    def test_valid_midnight(self):
        self.assertTrue(valid_hhmm("00:00"))

    def test_valid_end_of_day(self):
        self.assertTrue(valid_hhmm("23:59"))

    def test_valid_common(self):
        self.assertTrue(valid_hhmm("08:30"))
        self.assertTrue(valid_hhmm("22:00"))

    def test_invalid_hour_too_high(self):
        self.assertFalse(valid_hhmm("24:00"))

    def test_invalid_minute_too_high(self):
        self.assertFalse(valid_hhmm("12:60"))

    def test_invalid_no_colon(self):
        self.assertFalse(valid_hhmm("1200"))

    def test_invalid_empty(self):
        self.assertFalse(valid_hhmm(""))

    def test_invalid_short(self):
        self.assertFalse(valid_hhmm("8:30"))

    def test_invalid_non_numeric(self):
        self.assertFalse(valid_hhmm("ab:cd"))

    def test_invalid_negative_hour(self):
        self.assertFalse(valid_hhmm("-1:00"))


# ---------------------------------------------------------------------------
# Live test server - spins up on a free port, torn down after the class.
# ---------------------------------------------------------------------------

def _find_free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestServerBase(unittest.TestCase):
    """Mixin: starts a ThreadingHTTPServer on a random port for the test class."""

    @classmethod
    def setUpClass(cls):
        port = _find_free_port()
        cls._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        cls._port = port
        cls._base = f"http://127.0.0.1:{port}"
        t = threading.Thread(target=cls._server.serve_forever, daemon=True)
        t.start()

    @classmethod
    def tearDownClass(cls):
        cls._server.shutdown()

    def _post(self, path, headers, payload):
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._base + path,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path):
        req = urllib.request.Request(self._base + path)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


class TestCSRFProtection(TestServerBase):
    def test_post_without_origin_returns_403(self):
        status, body = self._post("/api/action", {}, {"action": "boost"})
        self.assertEqual(status, 403)
        self.assertFalse(body.get("ok", True))

    def test_post_with_correct_origin_is_allowed(self):
        with patch("boost_web.run_action", return_value={"ok": True, "message": "test"}):
            status, body = self._post(
                "/api/action",
                {"Origin": f"http://127.0.0.1:{self._port}"},
                {"action": "boost"},
            )
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("message"), "test")

    def test_post_with_wrong_origin_returns_403(self):
        status, body = self._post(
            "/api/action",
            {"Origin": "http://evil.example.com"},
            {"action": "boost"},
        )
        self.assertEqual(status, 403)

    def test_post_with_referer_allowed(self):
        with patch("boost_web.run_action", return_value={"ok": True, "message": "ok"}):
            status, body = self._post(
                "/api/action",
                {"Referer": f"http://127.0.0.1:{self._port}/"},
                {"action": "powersave"},
            )
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))

    def test_post_unknown_path_returns_404(self):
        status, _ = self._post(
            "/api/unknown",
            {"Origin": f"http://127.0.0.1:{self._port}"},
            {"action": "boost"},
        )
        self.assertEqual(status, 404)


class TestGetStatus(TestServerBase):
    def test_status_returns_200(self):
        fake_payload = {
            "ok": True,
            "time": "2026-06-25 12:00:00",
            "auto": {
                "mode": "dynamic",
                "service": "active",
                "quietStart": "22:00",
                "quietEnd": "08:00",
                "summerSilentNights": "no",
                "thresholds": {},
                "modes": [],
                "pause": {
                    "quietActive": False,
                    "todayOff": False,
                    "snoozed": False,
                    "snoozeUntil": 0,
                    "reason": "Suggestions are available.",
                },
                "ambient": {"detected": False, "temp": None, "source": "not detected"},
                "decision": "Current profile looks reasonable.",
            },
            "web": {"service": "active", "url": f"http://127.0.0.1:{8765}"},
            "profile": "balanced",
            "friendlyProfile": "Balanced",
            "cpu": {"load": 10, "temp": 55},
            "gpu": {"temp": "0", "power": "0", "limit": "0"},
            "limits": {"pl1": 0, "pl2": 0},
            "system": {"governor": "powersave", "epp": "balance_power", "turbo": "OFF"},
            "report": {
                "latestExists": False,
                "path": "/var/lib/power-profile/reports/latest.html",
            },
            "summary": {
                "avg_cpu": 0,
                "avg_temp": 0,
                "avg_gpu": 0,
                "max_temp": 0,
                "max_cpu": 0,
            },
            "history": [],
            "profileSwitches": [],
        }
        with patch("boost_web.status_payload", return_value=fake_payload):
            status, body = self._get("/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data.get("ok"))
        self.assertIn("cpu", data)

    def test_root_returns_200(self):
        status, _ = self._get("/")
        self.assertEqual(status, 200)

    def test_unknown_path_returns_404(self):
        status, _ = self._get("/does-not-exist")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
