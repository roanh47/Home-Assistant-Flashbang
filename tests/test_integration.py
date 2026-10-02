#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end test of the flash sequence against a real (local) HTTP server that
speaks the Home Assistant REST API.

This is the test that proves the whole chain works:

    GET  /api/states                       -> snapshot the selected lamps
    POST /api/services/light/turn_on       -> rgb_color 255,255,255, transition 0   (white phase)
    (hold)
    POST /api/services/light/turn_on       -> the snapshot, transition = fade       (restore)
    ...or POST /api/services/light/turn_off when the mode is "off"

Nothing here needs a display, a real Home Assistant or real lamps.

Run:  python -m unittest tests.test_integration -v
"""

import importlib.util
import json
import os
import queue
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The app is "Home-Assistant-Flashbang.py" - a dashed filename cannot be
# imported by name, so it is loaded from its path.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "flashbang", os.path.join(ROOT, "Home-Assistant-Flashbang.py"))
fb = importlib.util.module_from_spec(_spec)
sys.modules["flashbang"] = fb
_spec.loader.exec_module(fb)

try:
    import requests  # noqa: F401
    HAVE_REQUESTS = True
except Exception:
    HAVE_REQUESTS = False


STATES = [
    {"entity_id": "light.bureau", "state": "on", "attributes": {
        "friendly_name": "Bureau lamp",
        "supported_color_modes": ["rgb", "color_temp"],
        "brightness": 128, "rgb_color": [255, 120, 0], "color_mode": "rgb"}},
    {"entity_id": "light.plafond", "state": "on", "attributes": {
        "friendly_name": "Plafondlamp",
        "supported_color_modes": ["color_temp"],
        "brightness": 180, "color_temp_kelvin": 2700, "color_mode": "color_temp"}},
    {"entity_id": "light.hal", "state": "off", "attributes": {
        "friendly_name": "Hal",
        "supported_color_modes": ["brightness"], "color_mode": "brightness"}},
    {"entity_id": "sensor.temperature", "state": "21.4", "attributes": {
        "friendly_name": "Temperature"}},
]

WHITE = {"light.bureau", "light.plafond", "light.hal"}


class FakeHomeAssistant:
    """A tiny real HTTP server that records every call it receives."""

    def __init__(self):
        self.calls = []                 # [(path, payload), ...]
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):     # keep the test output clean
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                with outer.lock:
                    outer.calls.append((self.path, {"_method": "GET"}))
                if self.path == "/api/states":
                    self._send(STATES)
                elif self.path == "/api/":
                    self._send({"message": "API running."})
                else:
                    self._send({"message": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw or b"{}")
                except Exception:
                    payload = {}
                with outer.lock:
                    outer.calls.append((self.path, payload))
                self._send([])

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    def service_calls(self):
        with self.lock:
            return [c for c in self.calls if "/api/services/" in c[0]]

    def paths(self):
        with self.lock:
            return [c[0] for c in self.calls]


def entities_in(call):
    eid = call[1].get("entity_id")
    return set(eid if isinstance(eid, list) else [eid])


def call_for(calls, entity_id, service=None):
    """The call whose entity_id list contains this lamp."""
    for path, payload in calls:
        if service and not path.endswith("/" + service):
            continue
        if entity_id in entities_in((path, payload)):
            return path, payload
    return None


@unittest.skipUnless(HAVE_REQUESTS, "the 'requests' module is not installed")
class FlashSequenceTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeHomeAssistant()
        self.addCleanup(self.server.close)

    def flash(self, **kw):
        settings = fb.Settings(hass_url=self.server.url, token="test-token",
                               entities=["light.bureau", "light.plafond", "light.hal"],
                               hold=0.05, fade=0.3)
        for key, value in kw.items():
            setattr(settings, key, value)
        fb.Controller(settings, queue.Queue())._flash()
        return self.server.service_calls()

    # -- mode: current ---------------------------------------------------- #
    def test_current_mode_white_then_restore(self):
        calls = self.flash(mode="current")
        self.assertTrue(calls)

        # every lamp went full white with no transition, in the first half
        white_calls, back_calls = calls[:3], calls[3:]
        self.assertEqual(len(white_calls), 3)
        self.assertEqual(set().union(*[entities_in(c) for c in white_calls]), WHITE)
        for path, payload in white_calls:
            self.assertTrue(path.endswith("/turn_on"))
            self.assertEqual(payload["brightness"], 255)
            self.assertEqual(payload["transition"], 0.0)

        # ...and every lamp was put back exactly as the snapshot found it
        bureau = call_for(back_calls, "light.bureau")
        self.assertIsNotNone(bureau, "light.bureau was never restored")
        self.assertEqual(bureau[1]["brightness"], 128)
        self.assertEqual(bureau[1]["rgb_color"], [255, 120, 0])
        self.assertEqual(bureau[1]["transition"], 0.3)

        plafond = call_for(back_calls, "light.plafond")
        self.assertEqual(plafond[1]["color_temp_kelvin"], 2700)
        self.assertEqual(plafond[1]["brightness"], 180)
        self.assertNotIn("rgb_color", plafond[1])

        hal = call_for(back_calls, "light.hal")
        self.assertTrue(hal[0].endswith("/turn_off"), "the lamp that was off must go off")

    # -- mode: off -------------------------------------------------------- #
    def test_off_mode_turns_them_off_again(self):
        calls = self.flash(mode="off")
        turn_ons = [c for c in calls if c[0].endswith("/turn_on")]
        turn_offs = [c for c in calls if c[0].endswith("/turn_off")]
        self.assertEqual(len(turn_offs), 1)
        self.assertEqual(entities_in(turn_offs[0]), WHITE)
        self.assertEqual(turn_offs[0][1]["transition"], 0.3)
        for path, payload in turn_ons:
            self.assertEqual(payload["brightness"], 255)
            self.assertEqual(payload["transition"], 0.0)
        # nothing is restored in this mode: the lamps go back off, not to a
        # remembered colour
        self.assertFalse(any(c[1].get("rgb_color") == [255, 120, 0] or
                             c[1].get("color_temp_kelvin") == 2700 for c in calls))

    # -- the details ------------------------------------------------------ #
    def test_states_are_read_before_anything_is_switched(self):
        self.flash(mode="current")
        self.assertEqual(self.server.paths()[0], "/api/states")

    def test_white_lamps_get_a_temperature_not_a_colour(self):
        calls = self.flash(mode="off")
        plafond = call_for(calls, "light.plafond", service="turn_on")
        self.assertEqual(plafond[1]["color_temp_kelvin"], fb.WHITE_TEMP_K)
        self.assertNotIn("rgb_color", plafond[1])
        bureau = call_for(calls, "light.bureau", service="turn_on")
        self.assertEqual(bureau[1]["rgb_color"], [255, 255, 255])

    def test_hold_is_honoured(self):
        started = time.perf_counter()
        self.flash(mode="off", hold=0.4)
        elapsed = time.perf_counter() - started
        self.assertGreaterEqual(elapsed, 0.35)
        self.assertLess(elapsed, 2.0)

    def test_missing_lamps_are_reported_not_fatal(self):
        calls = self.flash(mode="current", entities=["light.does_not_exist"])
        self.assertEqual(calls, [])


@unittest.skipUnless(HAVE_REQUESTS, "the 'requests' module is not installed")
class StartWatchingTests(unittest.TestCase):
    """Pressing Start with the 'off' mode must darken the lamps straight away."""

    def setUp(self):
        self.server = FakeHomeAssistant()
        self.addCleanup(self.server.close)
        real = fb.ScreenGrabber
        fb.ScreenGrabber = _FakeGrabber
        self.addCleanup(lambda: setattr(fb, "ScreenGrabber", real))

    def controller(self, **kw):
        settings = fb.Settings(hass_url=self.server.url, token="test-token",
                               entities=["light.bureau", "light.plafond"],
                               fade=0.3, fps_target=30.0)
        for key, value in kw.items():
            setattr(settings, key, value)
        ctrl = fb.Controller(settings, queue.Queue())
        self.addCleanup(ctrl.stop)
        return ctrl

    def wait_for_a_call(self, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            calls = self.server.service_calls()
            if calls:
                return calls
            time.sleep(0.02)
        return []

    def test_start_in_off_mode_switches_the_lamps_off(self):
        ctrl = self.controller(mode="off")
        ctrl.start()
        calls = self.wait_for_a_call()
        self.assertTrue(calls, "Start in 'off' mode never switched the lamps off")
        path, payload = calls[0]
        self.assertTrue(path.endswith("/turn_off"), path)
        self.assertEqual(entities_in(calls[0]), {"light.bureau", "light.plafond"})
        self.assertEqual(payload["transition"], 0.3)

    def test_start_in_off_mode_does_not_wait_for_a_flash(self):
        self.controller(mode="off").start()
        self.wait_for_a_call()
        # turn_off only: no /api/states read, so this is not a flash in disguise
        self.assertEqual(self.server.paths(), ["/api/services/light/turn_off"])

    def test_start_in_current_mode_leaves_the_lamps_alone(self):
        self.controller(mode="current").start()
        time.sleep(0.5)
        self.assertEqual(self.server.service_calls(), [])

    def test_unchecking_it_leaves_the_lamps_alone(self):
        self.controller(mode="off", off_on_start=False).start()
        time.sleep(0.5)
        self.assertEqual(self.server.service_calls(), [])

    def test_stop_returns_promptly(self):
        ctrl = self.controller(mode="off", fps_target=60.0)
        ctrl.start()
        self.wait_for_a_call()
        started = time.perf_counter()
        ctrl.stop()
        self.assertLess(time.perf_counter() - started, 0.5)


class _FakeGrabber:
    """Stands in for the screen so the watching loop can run headless.

    It always reports a dark screen, so no flash can be triggered: every call
    the server sees in these tests comes from the start/stop path.
    """

    def __init__(self, *args, **kwargs):
        pass

    def open(self):
        return "fake"

    def stats(self):
        time.sleep(0.005)
        return (40.0, 0.002)

    def close(self):
        pass


@unittest.skipUnless(HAVE_REQUESTS, "the 'requests' module is not installed")
class ConnectionTests(unittest.TestCase):
    """The 'Connect / load lamps' path, without needing Home Assistant."""

    def setUp(self):
        self.server = FakeHomeAssistant()
        self.addCleanup(self.server.close)

    def test_url_normalising(self):
        self.assertEqual(fb.normalize_url("homeassistant.local:8123"),
                         "http://homeassistant.local:8123")
        self.assertEqual(fb.normalize_url("http://ha.local:8123/"),
                         "http://ha.local:8123")

    def test_ping_and_light_listing(self):
        client = fb.HassClient(self.server.url, "token")
        ok, message = client.check()
        self.assertTrue(ok, message)
        self.assertEqual([l.entity_id for l in client.lights()],
                         ["light.bureau", "light.hal", "light.plafond"])

    def test_unreachable_host_gives_a_message_not_a_traceback(self):
        client = fb.HassClient("http://127.0.0.1:1", "token")
        ok, message = client.check()
        self.assertFalse(ok)
        self.assertTrue(message)

    def test_bad_token_is_reported(self):
        server = FakeHomeAssistant()
        self.addCleanup(server.close)
        client = fb.HassClient(server.url, "token", session=_Unauthorized())
        ok, message = client.check()
        self.assertFalse(ok)
        self.assertIn("401", message)


class _Unauthorized:
    """Stands in for a session whose token is rejected."""

    class _Resp:
        status_code = 401
        text = "401: Unauthorized"

        def json(self):
            return {}

    def request(self, *a, **kw):
        return self._Resp()


if __name__ == "__main__":
    unittest.main(verbosity=2)
