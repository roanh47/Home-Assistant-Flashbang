#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests for Roan's Flashbang.

Run:  python -m unittest discover -s tests -v
      (or simply:  python -m unittest tests.test_flashbang -v)

No display, no Home Assistant and no Windows needed: the capture layer is the
only thing that is not covered here, everything around it is.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import flashbang as fb  # noqa: E402


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #

class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    """Records the calls and answers from a handler."""

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or (lambda method, url, kwargs: FakeResponse(200, {}))

    def request(self, method, url, headers=None, **kwargs):  # noqa: D401
        self.calls.append({"method": method, "url": url, "kwargs": kwargs,
                           "headers": headers})
        return self.handler(method, url, kwargs)

    # convenience
    @property
    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]

    @property
    def paths(self):
        return [c["url"] for c in self.calls]


def light_state(entity_id, name, state="on", modes=("rgb",), brightness=120,
                rgb_color=(255, 0, 0), color_temp_kelvin=None, color_mode="rgb"):
    attrs = {
        "friendly_name": name,
        "supported_color_modes": list(modes),
        "brightness": brightness,
        "color_mode": color_mode,
    }
    if rgb_color is not None:
        attrs["rgb_color"] = list(rgb_color)
    if color_temp_kelvin is not None:
        attrs["color_temp_kelvin"] = color_temp_kelvin
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


def sample_states():
    return [
        light_state("light.desk", "Desk lamp"),
        light_state("light.strip", "LED strip", modes=("color_temp",),
                    rgb_color=None, color_temp_kelvin=2700, color_mode="color_temp"),
        light_state("light.shelf", "Shelf", state="off", brightness=None,
                    modes=("brightness",), rgb_color=None, color_mode="brightness"),
        {"entity_id": "sensor.temp", "state": "21", "attributes": {}},
        {"entity_id": "switch.fan", "state": "on", "attributes": {}},
    ]


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #

class TestSettings(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            s = fb.Settings()
            s.hass_url = "http://ha.local:8123/"
            s.token = "abc"
            s.entities = ["light.a", "light.b"]
            s.mode = "off"
            s.hold = 1.25
            s.save(path)
            back = fb.Settings.load(path)
            self.assertEqual(back.hass_url, "http://ha.local:8123/")
            self.assertEqual(back.token, "abc")
            self.assertEqual(back.entities, ["light.a", "light.b"])
            self.assertEqual(back.mode, "off")
            self.assertAlmostEqual(back.hold, 1.25)

    def test_unknown_keys_are_ignored(self):
        s = fb.Settings.from_dict({"hass_url": "http://x", "wat": 1, "mode": "off"})
        self.assertEqual(s.hass_url, "http://x")
        self.assertEqual(s.mode, "off")

    def test_missing_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = fb.Settings.load(Path(tmp) / "nope.json")
            self.assertEqual(s.mode, "current")
            self.assertEqual(s.hold, 1.0)

    def test_corrupt_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ not json", encoding="utf-8")
            self.assertEqual(fb.Settings.load(path).mode, "current")

    def test_mode_survives(self):
        for mode in ("current", "off"):
            self.assertEqual(fb.Settings.from_dict({"mode": mode}).mode, mode)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

class TestHelpers(unittest.TestCase):
    def test_normalize_url(self):
        self.assertEqual(fb.normalize_url("homeassistant.local:8123"),
                         "http://homeassistant.local:8123")
        self.assertEqual(fb.normalize_url("http://ha/"), "http://ha")
        self.assertEqual(fb.normalize_url("https://ha:8123/"), "https://ha:8123")
        self.assertEqual(fb.normalize_url(""), "")

    def test_lights_filtered_and_sorted(self):
        lights = fb.lights_from_states(sample_states())
        self.assertEqual([l.entity_id for l in lights],
                         ["light.desk", "light.strip", "light.shelf"])
        self.assertEqual(lights[0].name, "Desk lamp")
        self.assertEqual(lights[0].supported, ("rgb",))
        self.assertEqual(lights[2].state, "off")

    def test_lights_name_fallback(self):
        lights = fb.lights_from_states(
            [{"entity_id": "light.x", "state": "on", "attributes": {}}])
        self.assertEqual(lights[0].name, "light.x")

    def test_snapshot_keeps_what_is_needed(self):
        snap = fb.snapshot_from_states(sample_states(), ["light.desk", "light.shelf"])
        self.assertEqual(set(snap), {"light.desk", "light.shelf"})
        self.assertEqual(snap["light.desk"]["state"], "on")
        self.assertEqual(snap["light.desk"]["brightness"], 120)
        self.assertEqual(snap["light.desk"]["rgb_color"], [255, 0, 0])
        self.assertEqual(snap["light.shelf"]["state"], "off")

    def test_snapshot_skips_missing_entity(self):
        snap = fb.snapshot_from_states(sample_states(), ["light.ghost"])
        self.assertEqual(snap, {})


# --------------------------------------------------------------------------- #
# white / restore payloads
# --------------------------------------------------------------------------- #

class TestPayloads(unittest.TestCase):
    def client(self, handler=None):
        session = FakeSession(handler)
        return fb.HassClient("http://ha:8123", "tok", session=session), session

    def test_headers_use_bearer(self):
        client, _ = self.client()
        self.assertEqual(client.headers()["Authorization"], "Bearer tok")

    def test_white_rgb_light(self):
        client, _ = self.client()
        light = fb.Light("light.desk", "Desk", "on", ("rgb",))
        data = client.white_data(light)
        self.assertEqual(data["rgb_color"], [255, 255, 255])
        self.assertEqual(data["brightness"], 255)
        self.assertEqual(data["transition"], 0.0)

    def test_white_color_temp_light(self):
        client, _ = self.client()
        light = fb.Light("light.strip", "Strip", "on", ("color_temp",))
        data = client.white_data(light)
        self.assertNotIn("rgb_color", data)
        self.assertEqual(data["color_temp_kelvin"], fb.WHITE_TEMP_K)
        self.assertEqual(data["brightness"], 255)

    def test_white_brightness_only_light(self):
        client, _ = self.client()
        light = fb.Light("light.shelf", "Shelf", "on", ("brightness",))
        data = client.white_data(light)
        self.assertEqual(data, {"entity_id": "light.shelf", "transition": 0.0,
                                "brightness": 255})

    def test_white_unknown_capabilities_tries_real_white(self):
        client, _ = self.client()
        light = fb.Light("light.weird", "Weird", "on", ())
        data = client.white_data(light)
        self.assertEqual(data["rgb_color"], [255, 255, 255])

    def test_turn_white_groups_identical_payloads(self):
        client, session = self.client()
        lights = [
            fb.Light("light.a", "A", "on", ("rgb",)),
            fb.Light("light.b", "B", "on", ("rgb",)),
            fb.Light("light.c", "C", "on", ("color_temp",)),
        ]
        done = client.turn_white(lights)
        self.assertEqual(sorted(done), ["light.a", "light.b", "light.c"])
        self.assertEqual(len(session.posts), 2)
        first = session.posts[0]["kwargs"]["json"]
        self.assertEqual(first["entity_id"], ["light.a", "light.b"])
        self.assertEqual(first["rgb_color"], [255, 255, 255])

    def test_turn_white_falls_back_to_brightness(self):
        def handler(method, url, kwargs):
            if method == "POST" and "rgb_color" in kwargs.get("json", {}):
                return FakeResponse(400, text="color not supported")
            return FakeResponse(200, {})

        client, session = self.client(handler)
        done = client.turn_white([fb.Light("light.a", "A", "on", ("rgb",))])
        self.assertEqual(done, ["light.a"])
        self.assertEqual(len(session.posts), 2)
        self.assertEqual(session.posts[1]["kwargs"]["json"]["brightness"], 255)
        self.assertNotIn("rgb_color", session.posts[1]["kwargs"]["json"])

    def test_restore_on_light_gets_colour_and_brightness_back(self):
        client, session = self.client()
        snap = fb.snapshot_from_states(sample_states(), ["light.desk"])
        client.restore(snap, transition=0.6)
        payload = session.posts[0]["kwargs"]["json"]
        self.assertEqual(payload["entity_id"], "light.desk")
        self.assertEqual(payload["brightness"], 120)
        self.assertEqual(payload["rgb_color"], [255, 0, 0])
        self.assertEqual(payload["transition"], 0.6)

    def test_restore_color_temp_light_uses_kelvin(self):
        client, session = self.client()
        snap = fb.snapshot_from_states(sample_states(), ["light.strip"])
        client.restore(snap)
        payload = session.posts[0]["kwargs"]["json"]
        self.assertEqual(payload["color_temp_kelvin"], 2700)
        self.assertNotIn("rgb_color", payload)

    def test_restore_off_light_turns_it_off_again(self):
        client, session = self.client()
        snap = fb.snapshot_from_states(sample_states(), ["light.shelf"])
        client.restore(snap)
        self.assertTrue(session.posts[0]["url"].endswith("/api/services/light/turn_off"))

    def test_turn_off_batches_and_handles_empty(self):
        client, session = self.client()
        self.assertTrue(client.turn_off([]))
        self.assertEqual(session.posts, [])
        client.turn_off(["light.a", "light.b"], transition=0.5)
        payload = session.posts[0]["kwargs"]["json"]
        self.assertEqual(payload["entity_id"], ["light.a", "light.b"])
        self.assertEqual(payload["transition"], 0.5)

    def test_http_error_raises(self):
        client, _ = self.client(lambda m, u, k: FakeResponse(401, text="unauthorized"))
        with self.assertRaises(fb.HassError):
            client.states()

    def test_check_reports_missing_fields(self):
        client, _ = self.client()
        client.token = ""
        ok, message = client.check()
        self.assertFalse(ok)
        self.assertIn("token", message.lower())

    def test_check_ok(self):
        client, _ = self.client(lambda m, u, k: FakeResponse(200, {"message": "API running."}))
        ok, message = client.check()
        self.assertTrue(ok)

    def test_lights_from_live_states_call(self):
        session = FakeSession(lambda m, u, k: FakeResponse(200, sample_states()))
        client = fb.HassClient("http://ha:8123", "tok", session=session)
        lights = client.lights()
        self.assertEqual(len(lights), 3)
        self.assertTrue(session.paths[0].endswith("/api/states"))


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #

class TestDetector(unittest.TestCase):
    def setUp(self):
        self.s = fb.Settings()
        self.t = 1000.0

    def feed(self, det, mean, ratio, frames=1):
        out = []
        for _ in range(frames):
            self.t += 1.0 / 60.0
            out.append(det.update(mean, ratio, now=self.t))
        return out

    def warm_up(self, det, mean=40.0, ratio=0.0):
        # a dark screen long enough to have a baseline
        self.feed(det, mean, ratio, frames=20)

    def test_flashbang_triggers(self):
        det = fb.FlashDetector(self.s)
        self.warm_up(det)
        self.assertEqual(self.feed(det, 252.0, 0.97), [True])

    def test_normal_gameplay_does_not_trigger(self):
        det = fb.FlashDetector(self.s)
        self.warm_up(det, mean=90.0, ratio=0.02)
        for mean, ratio in ((140.0, 0.1), (60.0, 0.0), (200.0, 0.2), (110.0, 0.05)):
            self.assertEqual(self.feed(det, mean, ratio), [False])

    def test_a_white_page_after_a_dark_one_is_one_flash_only(self):
        det = fb.FlashDetector(self.s)
        self.warm_up(det)
        self.assertEqual(self.feed(det, 250.0, 0.99), [True])
        # still white, nothing new may happen even after the cooldown
        self.feed(det, 250.0, 0.99, frames=600)
        self.assertEqual(det.state.flashes, 1)

    def test_screen_already_white_at_start_does_not_trigger(self):
        det = fb.FlashDetector(self.s)
        self.feed(det, 250.0, 0.99, frames=120)
        self.assertEqual(det.state.flashes, 0)

    def test_white_frame_that_is_not_full_screen_is_ignored(self):
        det = fb.FlashDetector(self.s)
        self.warm_up(det)
        # e.g. a dialog: bright, but far from every pixel white
        self.assertEqual(self.feed(det, 245.0, 0.30), [False])

    def test_bright_screen_to_slightly_brighter_is_not_a_jump(self):
        det = fb.FlashDetector(self.s)
        self.warm_up(det, mean=215.0, ratio=0.4)
        self.assertEqual(self.feed(det, 240.0, 0.9), [False])

    def test_cooldown_prevents_double_fire(self):
        det = fb.FlashDetector(self.s)
        self.warm_up(det)
        self.assertEqual(self.feed(det, 250.0, 0.99), [True])
        self.feed(det, 30.0, 0.0, frames=10)        # dark again, 0.17s later
        self.assertEqual(self.feed(det, 250.0, 0.99), [False])

    def test_rearms_after_cooldown_and_dark(self):
        det = fb.FlashDetector(self.s)
        self.warm_up(det)
        self.feed(det, 250.0, 0.99)
        self.feed(det, 30.0, 0.0, frames=25)        # long enough of a pause
        self.t += self.s.cooldown + 0.1
        self.assertEqual(self.feed(det, 250.0, 0.99), [True])
        self.assertEqual(det.state.flashes, 2)

    def test_first_frames_are_warmup(self):
        det = fb.FlashDetector(self.s)
        # white on the very first frame: no baseline, no trigger
        self.assertFalse(det.update(255.0, 1.0, now=self.t))

    def test_state_is_exposed_for_the_ui(self):
        det = fb.FlashDetector(self.s)
        self.warm_up(det)
        det.update(252.0, 0.97, now=self.t + 0.05)
        self.assertTrue(det.state.white)
        self.assertGreater(det.state.ratio, 0.9)
        self.assertEqual(det.state.flashes, 1)


# --------------------------------------------------------------------------- #
# maths behind the capture (needs numpy)
# --------------------------------------------------------------------------- #

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None


@unittest.skipIf(np is None, "numpy not installed")
class TestStats(unittest.TestCase):
    def frame(self, b, g, r):
        return np.zeros((8, 8, 4), dtype=np.uint8) + np.array([b, g, r, 255], dtype=np.uint8)

    def test_black_frame(self):
        mean, ratio = fb.bgra_stats(self.frame(0, 0, 0))
        self.assertAlmostEqual(mean, 0.0, places=3)
        self.assertAlmostEqual(ratio, 0.0)

    def test_white_frame(self):
        mean, ratio = fb.bgra_stats(self.frame(255, 255, 255))
        self.assertAlmostEqual(mean, 255.0, places=3)
        self.assertAlmostEqual(ratio, 1.0)

    def test_mid_gray(self):
        mean, ratio = fb.bgra_stats(self.frame(128, 128, 128))
        self.assertAlmostEqual(mean, 128.0, delta=1.0)
        self.assertAlmostEqual(ratio, 0.0)

    def test_green_brightness_weighting(self):
        mean, _ = fb.bgra_stats(self.frame(0, 255, 0))
        self.assertAlmostEqual(mean, 255 * 0.587, delta=1.0)

    def test_raw_bytes_path_matches_array_path(self):
        arr = self.frame(255, 255, 255)
        mean_a, ratio_a = fb.bgra_stats(arr)
        mean_b, ratio_b = fb.raw_stats(arr.tobytes(), 8, 8)
        self.assertAlmostEqual(mean_a, mean_b, places=6)
        self.assertAlmostEqual(ratio_a, ratio_b, places=6)

    def test_short_buffer_is_safe(self):
        self.assertEqual(fb.raw_stats(b"\x00" * 10, 8, 8), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
