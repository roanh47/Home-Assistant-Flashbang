#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Roan's Flashbang -> Home Assistant
==================================

Watches one screen of your Windows PC. When that screen is suddenly and almost
completely white (a flashbang going off in a game), it sets the Home Assistant
lights you picked to full white for one second, then fades them back to exactly
what they were before.

This is a *trigger*, not Hue Sync: nothing is mirrored continuously, the lights
only jump when a white flash is detected.

Run:   pythonw flashbang.py     (no console window)
       python  flashbang.py     (with console, useful for errors)

Requires: Python 3.9+, `mss`, `numpy`, `requests`. Tkinter ships with Python.
"""

from __future__ import annotations

import json
import os
import queue
import statistics
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

APP_NAME = "Roan's Flashbang"
VERSION = "1.0.0"

#: A flashbang is white. Pure white.
WHITE_RGB = [255, 255, 255]
WHITE_TEMP_K = 6500

try:  # Tkinter is optional at import time so the logic stays testable headless.
    import tkinter as tk
    from tkinter import messagebox, ttk
except Exception:  # pragma: no cover - only hit on a stripped Python
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #

def config_dir() -> Path:
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "Flashbang"
    return Path.home() / ".config" / "Flashbang"


CONFIG_FILE = config_dir() / "config.json"


@dataclass
class Settings:
    """Everything the app remembers between runs."""

    hass_url: str = "http://homeassistant.local:8123"
    token: str = ""
    monitor: int = 0
    entities: list = field(default_factory=list)
    mode: str = "current"          # "current" = restore what was there, "off" = lamps are off
    white_threshold: float = 235.0  # mean luminance (0-255) from which the screen counts as white
    white_ratio: float = 0.85       # fraction of pixels that must be near-white
    jump: float = 55.0              # required rise of the mean luminance vs. the frames before
    warmup: int = 6                 # frames to ignore after starting (get a baseline)
    cooldown: float = 3.0           # seconds before a new flash may fire
    hold: float = 1.0               # seconds the lamps stay full white
    fade: float = 0.6               # seconds to fade back
    backend: str = "auto"           # "auto" | "mss" | "bettercam"
    step: int = 8                   # sample every Nth pixel (8 = plenty and fast)
    fps_target: float = 60.0

    def to_dict(self) -> dict:
        return {
            "version": VERSION,
            "hass_url": self.hass_url,
            "token": self.token,
            "monitor": int(self.monitor),
            "entities": list(self.entities),
            "mode": self.mode,
            "white_threshold": float(self.white_threshold),
            "white_ratio": float(self.white_ratio),
            "jump": float(self.jump),
            "warmup": int(self.warmup),
            "cooldown": float(self.cooldown),
            "hold": float(self.hold),
            "fade": float(self.fade),
            "backend": self.backend,
            "step": int(self.step),
            "fps_target": float(self.fps_target),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        known = {f: None for f in cls().to_dict()}
        clean = {k: v for k, v in (data or {}).items() if k in known}
        clean.pop("version", None)
        return cls(**clean)

    def save(self, path: Optional[Path] = None) -> Path:
        path = Path(path) if path else CONFIG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)  # the token lives in here
        except Exception:
            pass
        return path

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Settings":
        path = Path(path) if path else CONFIG_FILE
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return cls()


# --------------------------------------------------------------------------- #
# screen capture
# --------------------------------------------------------------------------- #

def list_monitors() -> list:
    """Return the physical monitors as dicts: index / width / height / name."""
    try:
        import mss
    except Exception:
        return []
    try:
        with mss.mss() as sct:
            out = []
            for i, mon in enumerate(sct.monitors[1:]):
                out.append({
                    "index": i,
                    "left": mon["left"],
                    "top": mon["top"],
                    "width": mon["width"],
                    "height": mon["height"],
                    "name": "Monitor %d  (%dx%d)" % (i + 1, mon["width"], mon["height"]),
                })
            return out
    except Exception:
        return []


def bgra_stats(arr, step: int = 8):
    """Mean luminance (0-255) and near-white fraction of a BGRA / BGR array."""
    import numpy as np

    sub = arr[::step, ::step, :3]
    if sub.size == 0:
        return 0.0, 0.0
    b = sub[:, :, 0].astype(np.float32)
    g = sub[:, :, 1].astype(np.float32)
    r = sub[:, :, 2].astype(np.float32)
    mean = 0.114 * float(b.mean()) + 0.587 * float(g.mean()) + 0.299 * float(r.mean())
    darkest = np.minimum(np.minimum(b, g), r)
    ratio = float(np.count_nonzero(darkest >= 250)) / float(darkest.size)
    return float(mean), float(ratio)


def raw_stats(buf, width: int, height: int, step: int = 8):
    """Same as bgra_stats(), but from raw BGRA bytes (what mss hands back)."""
    import numpy as np

    arr = np.frombuffer(buf, dtype=np.uint8)
    want = width * height * 4
    if arr.size < want:
        return 0.0, 0.0
    return bgra_stats(arr[:want].reshape(height, width, 4), step)


class ScreenGrabber:
    """Grabs one monitor and reduces it to two cheap numbers."""

    def __init__(self, monitor: int = 0, step: int = 8, backend: str = "auto"):
        self.monitor = int(monitor)
        self.step = int(step)
        self.want_backend = backend or "auto"
        self.backend = ""
        self._sct = None
        self._cam = None
        self._monitor_info = {}
        self._lock = threading.Lock()

    # -- opening ---------------------------------------------------------- #
    def open(self) -> str:
        order = {
            "auto": ["bettercam", "mss"],
            "mss": ["mss"],
            "bettercam": ["bettercam", "mss"],
        }.get(self.want_backend, ["mss"])
        errors = []
        for name in order:
            try:
                if name == "bettercam":
                    self._open_bettercam()
                else:
                    self._open_mss()
                self.backend = name
                return name
            except Exception as exc:  # try the next backend
                errors.append("%s: %s" % (name, exc))
        raise RuntimeError("No screen backend available. " + " | ".join(errors))

    def _open_mss(self) -> None:
        import mss

        self._sct = mss.mss()
        mons = self._sct.monitors
        idx = self.monitor + 1  # [0] is the virtual "everything" desktop
        if idx >= len(mons):
            idx = 1 if len(mons) > 1 else 0
            self.monitor = idx - 1 if idx else 0
        self._monitor_info = dict(mons[idx])

    def _open_bettercam(self) -> None:
        import bettercam

        cam = bettercam.create(output_idx=self.monitor, output_color="BGRA")
        if cam is None:
            raise RuntimeError("bettercam could not open output %d" % (self.monitor,))
        self._cam = cam

    # -- per frame -------------------------------------------------------- #
    def stats(self):
        with self._lock:
            if self._cam is not None:
                frame = self._cam.grab()
                if frame is None:      # no new frame since last call
                    return None
                return bgra_stats(frame, self.step)
            if self._sct is None:
                return None
            shot = self._sct.grab(self._monitor_info)
            return raw_stats(shot.bgra, shot.width, shot.height, self.step)

    def close(self) -> None:
        with self._lock:
            cam, self._cam = self._cam, None
            sct, self._sct = self._sct, None
        for obj in (cam, sct):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# flash detection
# --------------------------------------------------------------------------- #

@dataclass
class DetectorState:
    mean: float = 0.0
    ratio: float = 0.0
    baseline: float = 0.0
    white: bool = False
    armed: bool = True
    warm: bool = False
    flashes: int = 0


class FlashDetector:
    """Turns a stream of (mean, ratio) frames into 'a flashbang just happened'."""

    def __init__(self, settings: Settings):
        self.s = settings
        self._hist = deque(maxlen=10)
        self._armed = True
        self._last_trigger = -1e9
        self._white_since = None
        self._flashes = 0
        self.state = DetectorState()

    def update(self, mean: float, ratio: float, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        s = self.s
        baseline = statistics.median(self._hist) if self._hist else 0.0
        white = mean >= s.white_threshold and ratio >= s.white_ratio

        if white:
            if self._white_since is None:
                self._white_since = now
            stuck_white = (now - self._white_since) > 2.5
        else:
            self._white_since = None
            stuck_white = False

        warm = len(self._hist) >= max(2, int(s.warmup))

        # re-arm once the cooldown has passed (the cooldown itself is what
        # prevents a double fire; a still-white screen is blocked by the
        # baseline check below, so nothing fires twice on one flash).
        if (now - self._last_trigger) >= s.cooldown:
            self._armed = True

        triggered = False
        if (
            white
            and not stuck_white
            and self._armed
            and warm
            and baseline <= (s.white_threshold - s.jump)
            and (now - self._last_trigger) >= s.cooldown
        ):
            triggered = True
            self._armed = False
            self._last_trigger = now
            self._flashes += 1

        self._hist.append(mean)
        self.state = DetectorState(
            mean=float(mean),
            ratio=float(ratio),
            baseline=float(baseline),
            white=bool(white),
            armed=bool(self._armed),
            warm=bool(warm),
            flashes=int(self._flashes),
        )
        return triggered


# --------------------------------------------------------------------------- #
# Home Assistant
# --------------------------------------------------------------------------- #

class HassError(RuntimeError):
    pass


@dataclass
class Light:
    entity_id: str
    name: str
    state: str = "unknown"
    supported: tuple = ()
    brightness: Optional[int] = None


SNAPSHOT_KEYS = (
    "brightness",
    "rgb_color",
    "rgbw_color",
    "color_temp_kelvin",
    "xy_color",
    "hs_color",
    "color_mode",
    "effect",
)


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return url
    if "://" not in url:
        url = "http://" + url
    return url


def lights_from_states(states: Iterable[dict]) -> list:
    """All light.* entities out of a /api/states payload, sorted by name."""
    out = []
    for st in states or []:
        eid = str(st.get("entity_id") or "")
        if not eid.startswith("light."):
            continue
        attrs = st.get("attributes") or {}
        out.append(Light(
            entity_id=eid,
            name=str(attrs.get("friendly_name") or eid),
            state=str(st.get("state") or "unknown"),
            supported=tuple(attrs.get("supported_color_modes") or ()),
            brightness=attrs.get("brightness"),
        ))
    out.sort(key=lambda l: l.name.lower())
    return out


def snapshot_from_states(states: Iterable[dict], entity_ids: Iterable[str]) -> dict:
    """Everything needed to put the selected lamps back exactly as they were."""
    wanted = list(entity_ids or [])
    index = {str(st.get("entity_id")): st for st in (states or [])}
    snap = {}
    for eid in wanted:
        st = index.get(eid)
        if st is None:
            continue
        attrs = st.get("attributes") or {}
        entry = {"state": str(st.get("state") or "off")}
        for key in SNAPSHOT_KEYS:
            if key in attrs:
                entry[key] = attrs[key]
        snap[eid] = entry
    return snap


class HassClient:
    """Small REST client. Only needs a long-lived access token."""

    def __init__(self, base_url: str, token: str, session=None, timeout: float = 5.0):
        self.base_url = normalize_url(base_url)
        self.token = (token or "").strip()
        self.timeout = float(timeout)
        if session is None:
            if requests is None:
                raise HassError("The 'requests' module is missing (pip install requests)")
            session = requests.Session()
        self._session = session

    # -- plumbing --------------------------------------------------------- #
    def headers(self) -> dict:
        return {
            "Authorization": "Bearer %s" % self.token,
            "Content-Type": "application/json",
        }

    def url(self, path: str) -> str:
        return self.base_url + path

    def _request(self, method: str, path: str, **kw):
        kw.setdefault("timeout", self.timeout)
        return self._session.request(method, self.url(path), headers=self.headers(), **kw)

    def _json(self, method: str, path: str, expect=(200, 201), **kw):
        resp = self._request(method, path, **kw)
        status = int(getattr(resp, "status_code", 0) or 0)
        if status not in expect:
            text = (getattr(resp, "text", "") or "")[:200]
            raise HassError("HTTP %s on %s: %s" % (status, path, text))
        return resp.json()

    # -- reads ------------------------------------------------------------ #
    def ping(self) -> dict:
        return self._json("GET", "/api/", expect=(200,))

    def check(self):
        """(ok, message) for the 'Connect' button."""
        if not self.base_url:
            return False, "No URL filled in."
        if not self.token:
            return False, "No token filled in."
        try:
            self.ping()
        except Exception as exc:
            return False, "Could not reach Home Assistant: %s" % (exc,)
        return True, "Connected."

    def states(self) -> list:
        data = self._json("GET", "/api/states", expect=(200,))
        if not isinstance(data, list):
            raise HassError("Unexpected answer from /api/states")
        return data

    def state(self, entity_id: str) -> dict:
        return self._json("GET", "/api/states/%s" % entity_id, expect=(200,))

    def lights(self) -> list:
        return lights_from_states(self.states())

    # -- writes ----------------------------------------------------------- #
    def call_service(self, domain: str, service: str, data: dict):
        return self._json("POST", "/api/services/%s/%s" % (domain, service), json=data)

    def white_data(self, light: Light, transition: float = 0.0) -> dict:
        """The exact turn_on payload that makes this lamp as white as it can be."""
        data: dict = {"entity_id": light.entity_id, "transition": float(transition)}
        modes = set(light.supported or ())
        if modes & {"rgb", "rgbw", "rgbww", "hs", "xy"}:
            data["rgb_color"] = list(WHITE_RGB)
            data["brightness"] = 255
        elif "color_temp" in modes:
            data["color_temp_kelvin"] = WHITE_TEMP_K
            data["brightness"] = 255
        elif modes & {"brightness", "white"}:
            data["brightness"] = 255
        else:
            # Unknown capabilities: try real white, the fallback in turn_white()
            # retries brightness-only if the lamp rejects it.
            data["rgb_color"] = list(WHITE_RGB)
            data["brightness"] = 255
        return data

    def turn_white(self, lights: Iterable[Light], transition: float = 0.0) -> list:
        """Full white on every light, one call per identical payload."""
        groups: dict = {}
        payloads: dict = {}
        for light in lights:
            data = self.white_data(light, transition)
            data.pop("entity_id", None)
            key = json.dumps(data, sort_keys=True)
            groups.setdefault(key, []).append(light.entity_id)
            payloads[key] = data
        done: list = []
        for key, entity_ids in groups.items():
            data = dict(payloads[key])
            data["entity_id"] = entity_ids
            try:
                self.call_service("light", "turn_on", data)
                done.extend(entity_ids)
            except Exception:
                for eid in entity_ids:      # retry without colour
                    try:
                        self.call_service("light", "turn_on", {
                            "entity_id": eid,
                            "brightness": 255,
                            "transition": float(transition),
                        })
                        done.append(eid)
                    except Exception:
                        pass
        return done

    def turn_off(self, entity_ids: Iterable[str], transition: float = 0.0) -> bool:
        entity_ids = [e for e in (entity_ids or [])]
        if not entity_ids:
            return True
        try:
            self.call_service("light", "turn_off", {
                "entity_id": entity_ids,
                "transition": float(transition),
            })
            return True
        except Exception:
            return False

    def restore(self, snap: dict, transition: float = 0.6) -> list:
        """Put every lamp back the way the snapshot found it."""
        done: list = []
        for eid, saved in (snap or {}).items():
            try:
                if str(saved.get("state")) != "on":
                    self.call_service("light", "turn_off", {
                        "entity_id": eid, "transition": float(transition)})
                    done.append(eid)
                    continue
                data: dict = {"entity_id": eid, "transition": float(transition)}
                if saved.get("brightness") is not None:
                    data["brightness"] = saved["brightness"]
                mode = saved.get("color_mode")
                if mode == "color_temp" and saved.get("color_temp_kelvin") is not None:
                    data["color_temp_kelvin"] = saved["color_temp_kelvin"]
                elif saved.get("rgb_color"):
                    data["rgb_color"] = saved["rgb_color"]
                elif saved.get("hs_color"):
                    data["hs_color"] = saved["hs_color"]
                elif saved.get("xy_color"):
                    data["xy_color"] = saved["xy_color"]
                elif saved.get("color_temp_kelvin") is not None:
                    data["color_temp_kelvin"] = saved["color_temp_kelvin"]
                if saved.get("effect"):
                    data["effect"] = saved["effect"]
                self.call_service("light", "turn_on", data)
                done.append(eid)
            except Exception:
                pass
        return done


# --------------------------------------------------------------------------- #
# controller (the worker thread)
# --------------------------------------------------------------------------- #

def enable_dpi_awareness() -> None:
    """Without this, mss sees scaled coordinates on a HiDPI Windows desktop."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class Controller:
    """Reads the screen in a thread and fires the lamp sequence on a flash."""

    def __init__(self, settings: Settings, events: "queue.Queue"):
        self.s = settings
        self.events = events
        self._stop = threading.Event()
        self._test = threading.Event()
        self._thread = None
        self._client = None
        self._client_key = None

    # -- lifecycle -------------------------------------------------------- #
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="flashbang", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 2.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=wait)

    def test_flash(self) -> None:
        self._test.set()

    def _emit(self, event: dict) -> None:
        try:
            self.events.put(event)
        except Exception:
            pass

    def _log(self, text: str, level: str = "info") -> None:
        self._emit({"type": "log", "text": text, "level": level})

    def _client_for(self) -> Optional[HassClient]:
        key = (self.s.hass_url, self.s.token)
        if self._client is not None and self._client_key == key:
            return self._client
        try:
            self._client = HassClient(self.s.hass_url, self.s.token)
            self._client_key = key
        except Exception as exc:
            self._client = None
            self._client_key = None
            self._log("Home Assistant client failed: %s" % (exc,), "error")
        return self._client

    # -- the loop --------------------------------------------------------- #
    def _run(self) -> None:
        try:
            grab = ScreenGrabber(self.s.monitor, self.s.step, self.s.backend)
            backend = grab.open()
        except Exception as exc:
            self._emit({"type": "fatal", "text": str(exc)})
            return

        self._log("Reading monitor %d via %s." % (self.s.monitor + 1, backend))
        detector = FlashDetector(self.s)
        frame_dt = 1.0 / max(5.0, float(self.s.fps_target))
        last = time.perf_counter()
        fps = 0.0

        while not self._stop.is_set():
            loop_start = time.perf_counter()
            try:
                stats = grab.stats()
            except Exception as exc:
                self._emit({"type": "fatal", "text": "Screen capture failed: %s" % (exc,)})
                break
            if stats is None:
                time.sleep(0.004)
                continue

            mean, ratio = stats
            triggered = detector.update(mean, ratio)
            elapsed = loop_start - last
            if elapsed > 0:
                fps = 0.85 * fps + 0.15 * (1.0 / elapsed)
            last = loop_start

            st = detector.state
            self._emit({
                "type": "stats",
                "mean": mean,
                "ratio": ratio,
                "baseline": st.baseline,
                "white": st.white,
                "fps": fps,
                "flashes": st.flashes,
            })

            if triggered:
                self._log("FLASHBANG  (brightness %.0f, %.0f%% white)" % (mean, ratio * 100), "flash")
                self._flash()
            if self._test.is_set():
                self._test.clear()
                self._log("Manual test flash.", "flash")
                self._flash()

            spent = time.perf_counter() - loop_start
            if spent < frame_dt:
                time.sleep(frame_dt - spent)

        grab.close()
        self._emit({"type": "stopped"})

    def _flash(self) -> None:
        entities = [e for e in (self.s.entities or []) if e]
        if not entities:
            self._log("No lamps selected - nothing to do.", "error")
            return
        client = self._client_for()
        if client is None:
            return

        try:
            states = client.states()                      # one call: states + snapshot
        except Exception as exc:
            self._log("Could not read Home Assistant: %s" % (exc,), "error")
            return

        selected = [l for l in lights_from_states(states) if l.entity_id in set(entities)]
        if not selected:
            self._log("None of the selected lamps exist anymore.", "error")
            return
        snap = snapshot_from_states(states, entities) if self.s.mode == "current" else {}

        started = time.perf_counter()
        self._emit({"type": "flash", "phase": "white"})
        white = client.turn_white(selected, transition=0.0)
        if not white:
            self._log("Could not switch any lamp to white.", "error")
            self._emit({"type": "flash", "phase": "done"})
            return

        try:
            time.sleep(max(0.0, float(self.s.hold)))
        except Exception:
            pass

        self._emit({"type": "flash", "phase": "restore"})
        if self.s.mode == "current":
            client.restore(snap, transition=self.s.fade)
        else:
            client.turn_off([l.entity_id for l in selected], transition=self.s.fade)
        self._emit({"type": "flash", "phase": "done"})
        self._log("Back to normal after %.2fs." % (time.perf_counter() - started,))


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

BG = "#12121a"
BG2 = "#1b1b26"
FG = "#e8e8f0"
MUTED = "#9a9ab0"
ACCENT = "#ff2d95"      # pink
ACCENT_DIM = "#7a1550"
OK = "#4ade80"
WARN = "#fbbf24"
ERR = "#f87171"


class FlashbangApp:
    def __init__(self, root):
        self.root = root
        self.settings = Settings.load()
        self.events: "queue.Queue" = queue.Queue()
        self.controller: Optional[Controller] = None
        self.lights: list = []
        self.light_vars: dict = {}
        self._flash_reset_job = None
        self._monitor_choices: list = []

        self._style()
        self._build()
        self._apply_settings()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._pump)
        self.root.after(200, self._load_lights_quietly)

    # -- look ------------------------------------------------------------- #
    def _style(self) -> None:
        self.root.configure(bg=BG)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=BG2,
                        bordercolor=ACCENT_DIM, lightcolor=BG2, darkcolor=BG2)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=BG2)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=FG,
                        font=("Segoe UI", 17, "bold"))
        style.configure("Card.TLabelframe", background=BG2, bordercolor=ACCENT_DIM)
        style.configure("Card.TLabelframe.Label", background=BG2, foreground=ACCENT,
                        font=("Segoe UI", 10, "bold"))
        style.configure("TCheckbutton", background=BG2, foreground=FG)
        style.map("TCheckbutton", background=[("active", BG2)],
                  foreground=[("disabled", MUTED)])
        style.configure("TRadiobutton", background=BG2, foreground=FG)
        style.map("TRadiobutton", background=[("active", BG2)])
        style.configure("TButton", background=BG2, foreground=FG, bordercolor=ACCENT_DIM,
                        focuscolor=ACCENT, padding=(10, 5))
        style.map("TButton", background=[("active", "#262634")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        font=("Segoe UI", 10, "bold"), padding=(14, 7))
        style.map("Accent.TButton", background=[("active", "#ff5cae"), ("disabled", ACCENT_DIM)])
        style.configure("TEntry", fieldbackground=BG2, foreground=FG,
                        insertcolor=ACCENT, bordercolor=ACCENT_DIM)
        style.configure("TCombobox", fieldbackground=BG2, background=BG2, foreground=FG,
                        arrowcolor=ACCENT)
        style.map("TCombobox", fieldbackground=[("readonly", BG2)])
        style.configure("Horizontal.TScale", background=BG2, troughcolor=BG, bordercolor=BG2)
        style.configure("Status.TLabel", background=BG2, foreground=FG,
                        font=("Consolas", 10))
        self.root.option_add("*TCombobox*Listbox*background", BG2)
        self.root.option_add("*TCombobox*Listbox*foreground", FG)

    def _card(self, parent, title: str):
        frame = ttk.Labelframe(parent, text="  %s  " % title, style="Card.TLabelframe",
                               padding=10)
        return frame

    # -- build ------------------------------------------------------------ #
    def _build(self) -> None:
        root = self.root
        root.title("%s  -  Home Assistant" % APP_NAME)
        root.minsize(700, 640)

        outer = ttk.Frame(root, padding=14)
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x", pady=(0, 10))
        ttk.Label(head, text="FLASHBANG", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="  screen goes white  ->  lamps go white",
                  style="Muted.TLabel").pack(side="left", pady=(6, 0))
        ttk.Label(head, text="v%s" % VERSION, style="Muted.TLabel").pack(side="right")

        # --- buttons + status --------------------------------------------- #
        # Packed before the cards and anchored to the bottom: pack shrinks the
        # last-packed widgets first, so this order keeps the controls visible
        # even when the window is shorter than the content wants to be.
        self.status = tk.Label(outer, text="idle", bg=BG2, fg=FG, anchor="w",
                               font=("Consolas", 10), padx=10, pady=8)
        self.log = tk.Label(outer, text="", bg=BG, fg=MUTED, anchor="w",
                            font=("Segoe UI", 9), padx=2, wraplength=740,
                            justify="left")
        controls = ttk.Frame(outer)
        self.start_btn = ttk.Button(controls, text="Start watching", style="Accent.TButton",
                                    command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(controls, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.test_btn = ttk.Button(controls, text="Test flash", command=self._test_flash,
                                   state="disabled")
        self.test_btn.pack(side="left")
        self.log.pack(side="bottom", fill="x", pady=(6, 0))
        self.status.pack(side="bottom", fill="x", pady=(0, 6))
        controls.pack(side="bottom", fill="x", pady=(0, 8))

        # --- Home Assistant --------------------------------------------- #
        card = self._card(outer, "Home Assistant")
        card.pack(fill="x", pady=(0, 8))
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text="URL").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.url_var = tk.StringVar()
        ttk.Entry(card, textvariable=self.url_var).grid(row=0, column=1, columnspan=2,
                                                        sticky="ew", pady=2)
        ttk.Label(card, text="Token").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.token_var = tk.StringVar()
        ttk.Entry(card, textvariable=self.token_var, show="*").grid(
            row=1, column=1, columnspan=2, sticky="ew", pady=2)
        self.connect_btn = ttk.Button(card, text="Connect / load lamps",
                                      command=self._connect)
        self.connect_btn.grid(row=2, column=1, sticky="w", pady=(6, 0))
        self.hass_status = ttk.Label(card, text="not connected", style="Muted.TLabel")
        self.hass_status.grid(row=2, column=2, sticky="w", padx=(10, 0))
        ttk.Label(card, text="Long-lived token from your profile page. Stored locally in %s"
                  % CONFIG_FILE, style="Muted.TLabel", wraplength=700).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # --- screen ------------------------------------------------------ #
        card = self._card(outer, "Screen")
        card.pack(fill="x", pady=(0, 8))
        ttk.Label(card, text="Monitor").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.monitor_var = tk.StringVar()
        self.monitor_box = ttk.Combobox(card, textvariable=self.monitor_var,
                                        state="readonly", width=34)
        self.monitor_box.grid(row=0, column=1, sticky="w")
        ttk.Button(card, text="Refresh", command=self._refresh_monitors).grid(
            row=0, column=2, padx=(8, 0))
        self.monitor_hint = ttk.Label(card, text="", style="Muted.TLabel")
        self.monitor_hint.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # --- lamps ------------------------------------------------------- #
        card = self._card(outer, "Lamps")
        card.pack(fill="both", expand=True, pady=(0, 8))
        card.columnconfigure(0, weight=1)
        card.rowconfigure(2, weight=1)

        bar = ttk.Frame(card)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="Search").pack(side="left", padx=(0, 8))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._render_lights())
        ttk.Entry(bar, textvariable=self.search_var, width=24).pack(side="left")
        ttk.Button(bar, text="All", width=6,
                   command=lambda: self._set_all(True)).pack(side="right")
        ttk.Button(bar, text="None", width=6,
                   command=lambda: self._set_all(False)).pack(side="right", padx=4)
        ttk.Button(bar, text="Only on",
                   command=self._select_only_on).pack(side="right", padx=4)

        self.lamp_count = ttk.Label(card, text="no lamps loaded", style="Muted.TLabel")
        self.lamp_count.grid(row=1, column=0, sticky="w", pady=(6, 4))

        wrap = tk.Frame(card, bg=BG2)
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(wrap, bg=BG2, highlightthickness=0, bd=0, height=140)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.lamp_frame = tk.Frame(self.canvas, bg=BG2)
        self._lamp_window = self.canvas.create_window((0, 0), window=self.lamp_frame,
                                                      anchor="nw")
        self.lamp_frame.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._lamp_window, width=e.width))
        for widget in (self.canvas, self.lamp_frame):
            widget.bind("<MouseWheel>", self._on_wheel)
            widget.bind("<Button-4>", self._on_wheel)
            widget.bind("<Button-5>", self._on_wheel)

        # --- mode -------------------------------------------------------- #
        card = self._card(outer, "Lamp behaviour on a flash")
        card.pack(fill="x", pady=(0, 8))
        self.mode_var = tk.StringVar(value="current")
        ttk.Radiobutton(card, value="current", variable=self.mode_var,
                        text="Current  -  read the state from Home Assistant, make it white, "
                             "put it back afterwards").pack(anchor="w")
        ttk.Radiobutton(card, value="off", variable=self.mode_var,
                        text="Off  -  the lamps are off, white on a flash, off again "
                             "afterwards").pack(anchor="w", pady=(2, 0))

        # --- detection --------------------------------------------------- #
        card = self._card(outer, "Detection")
        card.pack(fill="x", pady=(0, 8))
        card.columnconfigure(1, weight=1)
        self.threshold_lbl = tk.StringVar()
        self.ratio_lbl = tk.StringVar()
        self.hold_lbl = tk.StringVar()
        self.fade_lbl = tk.StringVar()

        def scale_row(row, label, var, lo, hi, init, fmt, note):
            ttk.Label(card, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10))
            val = tk.StringVar(value=fmt % init)
            ttk.Scale(card, from_=lo, to=hi, variable=var, orient="horizontal",
                      command=lambda v, s=val, f=fmt: s.set(f % float(v))).grid(
                row=row, column=1, sticky="ew")
            ttk.Label(card, textvariable=val, width=8).grid(row=row, column=2, sticky="e")
            ttk.Label(card, text=note, style="Muted.TLabel").grid(
                row=row, column=3, sticky="w", padx=(10, 0))

        self.threshold_var = tk.DoubleVar(value=self.settings.white_threshold)
        self.ratio_var = tk.DoubleVar(value=self.settings.white_ratio * 100)
        self.hold_var = tk.DoubleVar(value=self.settings.hold)
        self.fade_var = tk.DoubleVar(value=self.settings.fade)
        scale_row(0, "White from", self.threshold_var, 180, 253,
                  self.settings.white_threshold, "%.0f", "mean brightness")
        scale_row(1, "Min. white", self.ratio_var, 40, 100,
                  self.settings.white_ratio * 100, "%.0f%%", "of the pixels")
        scale_row(2, "Hold", self.hold_var, 0.2, 5.0, self.settings.hold, "%.2fs",
                  "full white")
        scale_row(3, "Fade back", self.fade_var, 0.0, 3.0, self.settings.fade, "%.2fs",
                  "to the old colour")

        # --- buttons + status -------------------------------------------- #
        # (built above, before the cards - see the note there)

        # Size the window to what the content needs, but never taller than the
        # screen allows: the lamp list (the only expanding part) shrinks instead.
        root.update_idletasks()
        needed = root.winfo_reqheight()
        available = root.winfo_screenheight() - 90
        root.geometry("780x%d" % max(640, min(needed, available)))

    # -- helpers ---------------------------------------------------------- #
    def _on_wheel(self, event):
        delta = -1 if getattr(event, "delta", 0) > 0 or event.num == 4 else 1
        try:
            self.canvas.yview_scroll(delta, "units")
        except Exception:
            pass

    def _refresh_monitors(self) -> None:
        monitors = list_monitors()
        self._monitor_choices = monitors
        if monitors:
            names = [m["name"] for m in monitors]
            self.monitor_box.configure(values=names)
            idx = min(max(0, int(self.settings.monitor)), len(names) - 1)
            self.monitor_var.set(names[idx])
            self.monitor_hint.configure(
                text="%d monitor(s) found. Only this one is watched." % len(names))
        else:
            self.monitor_box.configure(values=["Monitor 1 (unknown)"])
            self.monitor_var.set("Monitor 1 (unknown)")
            self.monitor_hint.configure(
                text="No monitors found (is mss installed, and is there a display?).")

    def _apply_settings(self) -> None:
        s = self.settings
        self.url_var.set(s.hass_url)
        self.token_var.set(s.token)
        self.mode_var.set(s.mode if s.mode in ("current", "off") else "current")
        self.threshold_var.set(s.white_threshold)
        self.ratio_var.set(s.white_ratio * 100)
        self.hold_var.set(s.hold)
        self.fade_var.set(s.fade)
        self._refresh_monitors()

    def _collect(self) -> Settings:
        s = self.settings
        s.hass_url = self.url_var.get().strip()
        s.token = self.token_var.get().strip()
        s.mode = self.mode_var.get()
        s.white_threshold = float(self.threshold_var.get())
        s.white_ratio = float(self.ratio_var.get()) / 100.0
        s.hold = float(self.hold_var.get())
        s.fade = float(self.fade_var.get())
        s.entities = [eid for eid, var in self.light_vars.items() if var.get()]
        selection = self.monitor_var.get()
        for mon in self._monitor_choices:
            if mon["name"] == selection:
                s.monitor = int(mon["index"])
                break
        return s

    def _save(self) -> None:
        try:
            self._collect().save()
        except Exception:
            pass

    # -- lamps ------------------------------------------------------------ #
    def _connect(self) -> None:
        url = self.url_var.get().strip()
        token = self.token_var.get().strip()
        self.hass_status.configure(text="connecting...", foreground=WARN)
        self.root.update_idletasks()
        try:
            client = HassClient(url, token)
            ok, message = client.check()
        except Exception as exc:
            ok, message = False, str(exc)
        if not ok:
            self.hass_status.configure(text=message, foreground=ERR)
            return
        try:
            self.lights = client.lights()
        except Exception as exc:
            self.hass_status.configure(text="Loading lamps failed: %s" % (exc,),
                                       foreground=ERR)
            return
        self.hass_status.configure(text="%d lamps" % len(self.lights), foreground=OK)
        self._save()
        self._render_lights()

    def _load_lights_quietly(self) -> None:
        """Try once at startup with the stored settings, without nagging."""
        if not (self.settings.hass_url and self.settings.token):
            return
        try:
            client = HassClient(self.settings.hass_url, self.settings.token,
                                timeout=3.0)
            self.lights = client.lights()
            self.hass_status.configure(text="%d lamps" % len(self.lights), foreground=OK)
            self._render_lights()
        except Exception as exc:
            self.hass_status.configure(text="not connected (%s)" % (exc,),
                                       foreground=MUTED)

    def _render_lights(self) -> None:
        for child in self.lamp_frame.winfo_children():
            child.destroy()
        needle = self.search_var.get().strip().lower()
        shown = 0
        for light in self.lights:
            if needle and needle not in light.name.lower() \
                    and needle not in light.entity_id.lower():
                continue
            if light.entity_id not in self.light_vars:
                self.light_vars[light.entity_id] = tk.BooleanVar(value=False)
            dot = "*" if light.state == "on" else "-"
            ttk.Checkbutton(
                self.lamp_frame,
                text="%s  %s   [%s]" % (dot, light.name, light.state),
                variable=self.light_vars[light.entity_id],
            ).pack(anchor="w", fill="x")
            shown += 1
        if not self.lights:
            ttk.Label(self.lamp_frame, text="Connect to load your lamps.",
                      style="Muted.TLabel").pack(anchor="w")
        selected = sum(1 for v in self.light_vars.values() if v.get())
        self.lamp_count.configure(
            text="%d of %d lamps shown  -  %d selected" % (shown, len(self.lights), selected))

    def _set_all(self, value: bool) -> None:
        for var in self.light_vars.values():
            var.set(value)
        self._render_lights()

    def _select_only_on(self) -> None:
        on = {l.entity_id for l in self.lights if l.state == "on"}
        for eid, var in self.light_vars.items():
            var.set(eid in on)
        self._render_lights()

    # -- run -------------------------------------------------------------- #
    def _start(self) -> None:
        self._collect()
        if not self.settings.entities:
            messagebox.showwarning(APP_NAME, "Select at least one lamp first.")
            return
        if not (self.settings.hass_url and self.settings.token):
            messagebox.showwarning(APP_NAME, "Fill in the Home Assistant URL and token.")
            return
        self._save()
        self.controller = Controller(self.settings, self.events)
        self.controller.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.test_btn.configure(state="normal")
        self.start_btn.configure(text="Watching...")

    def _stop(self) -> None:
        if self.controller:
            self.controller.stop()
            self.controller = None
        self.start_btn.configure(state="normal", text="Start watching")
        self.stop_btn.configure(state="disabled")
        self.test_btn.configure(state="disabled")

    def _test_flash(self) -> None:
        if self.controller:
            self.controller.test_flash()

    # -- events ----------------------------------------------------------- #
    def _pump(self) -> None:
        try:
            for _ in range(400):
                event = self.events.get_nowait()
                self._handle(event)
        except queue.Empty:
            pass
        except Exception:
            pass
        self.root.after(80, self._pump)

    def _handle(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "stats":
            self.status.configure(
                text="brightness %3.0f   white %3.0f%%   base %3.0f   |   %d flash%s   |   "
                     "%s   %.0f fps"
                % (
                    event.get("mean", 0.0),
                    event.get("ratio", 0.0) * 100.0,
                    event.get("baseline", 0.0),
                    event.get("flashes", 0),
                    "" if event.get("flashes", 0) == 1 else "es",
                    "WHITE" if event.get("white") else "dark",
                    event.get("fps", 0.0),
                )
            )
        elif kind == "log":
            level = event.get("level", "info")
            color = {"error": ERR, "flash": ACCENT}.get(level, MUTED)
            self.log.configure(text=str(event.get("text", "")), fg=color)
        elif kind == "flash":
            phase = event.get("phase")
            if phase == "white":
                self._flash_on()
            elif phase == "done":
                self._flash_off()
        elif kind == "fatal":
            self._stop()
            self.log.configure(text=str(event.get("text", "")), fg=ERR)
            messagebox.showerror(APP_NAME, str(event.get("text", "")))
        elif kind == "stopped":
            if self.controller is None:
                self.start_btn.configure(state="normal", text="Start watching")

    def _flash_on(self) -> None:
        self.status.configure(bg="#ffffff", fg="#000000")

    def _flash_off(self) -> None:
        self.status.configure(bg=BG2, fg=FG)

    def _on_close(self) -> None:
        try:
            self._save()
        except Exception:
            pass
        if self.controller:
            self.controller.stop()
            self.controller = None
        self.root.destroy()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print("%s %s" % (APP_NAME, VERSION))
        return 0
    if tk is None:
        sys.stderr.write(
            "Tkinter is not available in this Python.\n"
            "  Windows/macOS: reinstall Python and keep 'tcl/tk' checked.\n"
            "  Debian/Ubuntu: sudo apt install python3-tk\n"
        )
        return 2
    enable_dpi_awareness()
    root = tk.Tk()
    FlashbangApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())