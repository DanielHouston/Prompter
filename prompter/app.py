"""The prompter application: state, actions (also driven over D-Bus by prompterctl), config."""

import json
import os
import shutil
import time
from collections import deque
from importlib import resources
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, displays, tilde  # noqa: E402
from . import script as scripts  # noqa: E402
from .actions import ACTIONS, GLOBAL_ONLY, KEYS  # noqa: E402
from .aligner import Aligner  # noqa: E402
from . import decks, models  # noqa: E402
from .asr import AsrWorker, ModelDownload  # noqa: E402
from .audio import microphones  # noqa: E402
from .camera import CameraFeed, cameras  # noqa: E402
from .control import ControlWindow  # noqa: E402
from .ddc import ScreenControl  # noqa: E402
from .readouts import DEFAULT_READOUTS, Clock, Stopwatch, duration  # noqa: E402
from .script import words_of  # noqa: E402
from .shortcuts import GlobalShortcuts  # noqa: E402
from .slidekeys import SlideKeys  # noqa: E402
from .slidekeys import clickers  # noqa: E402
from .tray import SEPARATOR, MenuItem, Tray  # noqa: E402
from .view import DEFAULT_APPEARANCE, DEFAULT_CROSSHAIR, DEFAULT_PREVIEW, PrompterView  # noqa: E402

CONFIG = Path(GLib.get_user_config_dir()) / "prompter" / "config.json"
DEFAULTS = {
    "scripts_dir": None,  # None = "Prompter" in your Documents folder
    "file": None,
    "font_size": 52,
    "wpm": 140,
    "mode": "voice",  # or "fixed"
    "language": "en",  # what you read in (ISO 639-1)
    "model": "auto",  # "auto" (the best downloaded one), a name from prompter.models, or a model folder
    "device": "auto",  # where the speech model runs: auto | cuda | cpu
    "mic": None,  # PipeWire node name; None = default source
    "display": None,  # monitor connector for the script; None = find the Elgato Prompter
    "positions": {},  # script path -> token index, to resume where you left off
    "record": False,  # save mic audio + every transcript/decision under ~/.cache/prompter/sessions
    "appearance": DEFAULT_APPEARANCE,
    "crosshair": DEFAULT_CROSSHAIR,
    "preview": DEFAULT_PREVIEW,
    "readouts": DEFAULT_READOUTS,
    "slides": {"devices": []},  # clickers followed while started ("vendor:product")
}
SESSIONS = Path(GLib.get_user_cache_dir()) / "prompter" / "sessions"
SUFFIXES = (".md", ".markdown", ".txt") + decks.SUFFIXES
ACCELS = {"reload": ["<Control>r"], "quit": ["<Control>q"]}  # single keys live in the window, not app-wide
RATE_WINDOW = 40  # seconds of voice-follow progress used to estimate your speaking rate


class PrompterApp(Adw.Application):
    APP_ID = APP_ID
    ASR_ARGS = ()

    def __init__(self):
        super().__init__(application_id=self.APP_ID)
        self.script = None
        self.aligner = None
        self.file = None
        self.pos = 0  # next token to be read
        self.fixed_pos = 0.0
        self.playing = False
        self.calibrating = False
        self.asr_ready = False
        self.status = "Starting…"
        self.session = None
        self.engine = None  # (model, device) once the speech engine is ready
        self.missing = None  # the model voice follow needs but isn't downloaded (for "auto", the one to get)
        self.device = None  # where the engine last ran or would run: "cuda" or "cpu"
        self.download = None  # the model download in progress, if any
        # The slide your presentation is on, as far as the clicker says, and the animation clicks
        # played on it. Voice follow can be in the next slide's notes before you click.
        self.slide = self.slide_step = 0
        self._clicker = ""  # what the status says about the followed clicker
        self._events = None
        self._rate = deque()  # (monotonic time, position) while voice-following
        self._primed_at = -100
        self._save_id = self._reload_id = self._readout_tick = 0
        self.stopwatch = Stopwatch()
        self.clock = Clock()
        self._monitor = None

    # --- lifecycle ----------------------------------------------------------

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.config = _load_config()

        for name in ACTIONS:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", getattr(self, "on_" + name.replace("-", "_")))
            self.add_action(action)
            self.set_accels_for_action("app." + name, ACCELS.get(name, []))

        self.view = PrompterView()
        self.view.set_font_size(self.config["font_size"])
        self.view.set_style(self.config["appearance"], self.config["crosshair"])
        self.view.on_frame = self._frame
        self.display = Gtk.Window(application=self, title="Prompter", decorated=False, child=self.view)
        self.display.connect("close-request", lambda *_: self.quit() or False)

        self.control = ControlWindow(self)
        self.control.set_style(self.config["appearance"], self.config["crosshair"])
        self.control.set_preview(self.config["preview"])
        self.control.set_speech(self.config["language"], self.config["model"], self.config["device"])
        # Closing the control window only hides it: the tray brings it back
        self.tray = Tray(self.get_application_id(), "Prompter", resources.files("prompter") / "data" / f"{APP_ID}.svg",
                         self._tray_clicked, lambda: self.activate_action("toggle"))
        self.slide_keys = SlideKeys(self._slide_key, self.refresh_slide_devices)
        self.refresh_devices()
        Gdk.Display.get_default().get_monitors().connect("items-changed", lambda *_: GLib.idle_add(self._place_display))

        global_shortcuts = [(action, what, trigger) for _key, action, what, trigger in KEYS] + list(GLOBAL_ONLY)
        self.shortcuts = GlobalShortcuts(self.get_application_id(), global_shortcuts, self.activate_action,
                                         self.control.set_global_shortcuts)

        self.camera = CameraFeed(self.view.set_preview_frame, self.control.set_camera_status)
        self.screen = ScreenControl(self.control.set_screen)
        if self.screen.available:
            self.screen.refresh()
        else:
            self.control.set_screen("Needs a one-time setup: see “Prompter brightness” in the README")
        self._apply_preview()
        self._apply_readouts()

        self.asr = AsrWorker(self._asr_message)
        if self.config["record"]:
            self._open_session()
        self._start_asr()
        self._show_recording()

        self._watch_folder()
        self.control.set_scripts_dir(self.folder, self.config["scripts_dir"] is not None)
        self._open_first()
        self._sync_state()

    def do_activate(self):
        self._place_display()
        self.control.present()

    def do_shutdown(self):
        self.cancel_download()
        self._save()
        self.asr.stop()
        self.camera.stop()
        Adw.Application.do_shutdown(self)

    @property
    def mode(self):
        return self.config["mode"]

    @property
    def folder(self):
        if self.config["scripts_dir"]:
            return Path(os.path.expanduser(self.config["scripts_dir"]))
        return default_folder()

    # --- devices ------------------------------------------------------------

    def refresh_devices(self):
        self._refresh_displays()
        self.control.set_mics(microphones(), self.config["mic"])
        self.control.set_cameras(cameras(), self.config["preview"]["device"])
        self.refresh_slide_devices()

    def refresh_slide_devices(self):
        self.control.set_slide_devices(clickers(), self.config["slides"]["devices"])
        if self.playing:
            self._sync_state()  # the status names what's followed

    def _refresh_displays(self):
        monitors = displays.monitors(Gdk.Display.get_default())
        self.control.set_displays([(m.get_connector(), displays.label(m)) for m in monitors], self.config["display"])

    def _place_display(self):
        """Fullscreen the script on the Prompter (or the chosen display); hide it while it's missing."""
        monitor = displays.find(Gdk.Display.get_default(), self.config["display"])
        self.control.set_prompter_found(monitor is not None)
        if monitor is None:
            self.display.set_visible(False)
        else:
            self.display.fullscreen_on_monitor(monitor)
            self.display.present()
        self._refresh_displays()
        return GLib.SOURCE_REMOVE

    def set_display(self, connector):
        if connector == self.config["display"]:
            return
        self.config["display"] = connector
        self._save_soon()
        self._place_display()

    def set_camera(self, device):
        if device != self.config["preview"]["device"]:
            self.set_preview("device", device)

    # --- scripts --------------------------------------------------------------

    def _names(self):
        if not self.folder.is_dir():
            return []
        return sorted(p.name for p in self.folder.iterdir() if p.suffix.lower() in SUFFIXES and p.is_file())

    def refresh_files(self):
        entries = []
        for name in self._names():
            try:
                script = scripts.load(self.folder / name)
                if hasattr(script, "slides"):
                    entries.append((name, script_title(name), f"{len(script.slides)} slides"))
                else:
                    words = sum(1 for t in script.tokens if not t.optional)
                    entries.append((name, script_title(name), f"{words} words"))
            except (OSError, UnicodeDecodeError, ValueError):
                entries.append((name, script_title(name), "can't read this file"))
        self.control.set_files(entries, self.file.name if self.file else None)

    def set_scripts_dir(self, path):
        """Read scripts from another folder; None goes back to the default."""
        path = Path(path) if path else None
        if (path or default_folder()) == self.folder:
            return
        self._remember()
        self.config["scripts_dir"] = None if path in (None, default_folder()) else tilde(path)
        self._save_soon()
        if self._monitor:
            self._monitor.cancel()
        self.file = None
        self._watch_folder()
        self.control.set_scripts_dir(self.folder, self.config["scripts_dir"] is not None)
        self._open_first()

    def _open_first(self):
        """Open the last script if it's in the folder, else the first one there is."""
        names = self._names()
        self.refresh_files()
        if names:
            self.open_named(self.config["file"] if self.config["file"] in names else names[0])
        else:
            self._unload()

    def open_named(self, name):
        path = self.folder / name
        if self.file == path:
            return
        self._remember()
        self.file = path
        self.config["file"] = name
        self.stopwatch.reset()
        self.slide = self.slide_step = 0
        self._rate.clear()
        positions = self.config["positions"]
        self._load(positions.get(str(path), positions.get(name, 0)))  # by name: saved before folders could change

    def _unload(self):
        """Nothing to read: an empty folder."""
        self.script = self.aligner = self.file = None
        self.pos = 0
        self.stopwatch.reset()
        self.view.set_script(None, 0)
        self.control.set_script(None, "No script")
        self.refresh_files()
        self._update_tray()

    def _load(self, pos):
        try:
            self.script = scripts.load(self.file)
        except (OSError, UnicodeDecodeError, ValueError) as e:
            self._set_status(f"Can't read {self.file.name}: {e}")
            return
        self._copy_script_to_session()
        pos = min(pos, len(self.script))
        self.aligner = Aligner(self.script.tokens, pos)
        self.view.set_script(self.script, pos)
        self.control.set_script(self.script, script_title(self.file.name))
        self.refresh_files()
        self.set_pos(pos, prime=True)
        self._update_tray()

    def _watch_folder(self):
        if not self.folder.exists():  # first run: something to read straight away
            self.folder.mkdir(parents=True)
            (self.folder / "welcome.md").write_bytes((resources.files("prompter") / "data" / "welcome.md").read_bytes())
        self._monitor = Gio.File.new_for_path(str(self.folder)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self._monitor.connect("changed", self._folder_changed)

    def _folder_changed(self, _monitor, file, other, _event):
        names = {f.get_basename() for f in (file, other) if f}
        if self.file and self.file.name in names and not self._reload_id:
            self._reload_id = GLib.timeout_add(250, self._reload)  # editors save in bursts
        if any(n.lower().endswith(SUFFIXES) for n in names):
            # an empty folder just got its first script: open it; otherwise just update the list
            GLib.idle_add(lambda: (self._open_first() if self.script is None else self.refresh_files()) and False)

    def _reload(self):
        self._reload_id = 0
        if self.file and self.file.exists():
            self._load(self.pos)
        return GLib.SOURCE_REMOVE

    # --- position -----------------------------------------------------------

    def set_pos(self, pos, source="user", prime=False):
        if not self.script:
            return
        pos = max(0, min(pos, len(self.script)))
        moved = pos != self.pos
        if source == "user" and self.script.section_at(pos) != self.slide:
            self.slide, self.slide_step = self.script.section_at(pos), 0  # moving the notes yourself puts the slides there
        self.pos = pos
        if source != "voice":
            self.aligner.pos = pos
            self._rate.clear()  # a jump says nothing about how fast you speak
        else:
            self._track_rate(pos)
        if source != "fixed":
            self.fixed_pos = float(pos)
            self.view.set_position(pos)
        self.view.set_spoken(pos)
        self._publish_position()
        if prime or abs(pos - self._primed_at) >= 8:
            self._primed_at = pos
            self.asr.prompt(self.script.context(pos))  # bias recognition toward what's coming
        if moved:
            self._save_soon()
            if source == "user":
                self._log(type="jump", pos=pos)

    def jump(self, pos):
        self.set_pos(pos, prime=True)

    def _track_rate(self, pos):
        now = time.monotonic()
        self._rate.append((now, pos))
        while self._rate and now - self._rate[0][0] > RATE_WINDOW:
            self._rate.popleft()

    def speaking_rate(self):
        """Words per minute: measured while voice-following, otherwise the fixed-speed setting."""
        if self.mode == "voice" and len(self._rate) >= 2:
            (t0, p0), (t1, p1) = self._rate[0], self._rate[-1]
            if t1 - t0 >= 8 and p1 - p0 >= 8:
                return (p1 - p0) / (t1 - t0) * 60
        return self.config["wpm"]

    def _publish_position(self):
        n = len(self.script)
        section = self.script.section_at(self.pos)
        if self.pos >= n:
            where = "Finished"
        else:
            where = f"{self.script.sections[section].title} · {self._percent()}% · ~{duration(self._seconds_left())} left"
        if self.slide_keys.open:  # following a clicker: the slide on screen, when your notes are elsewhere, and its animations
            clicks = decks.clicks(self.script)[self.slide]
            shown = [f"slide {self.slide + 1} on screen"] if self.slide != section else []
            shown += [f"animation {self.slide_step} of {clicks}"] if clicks else []
            if shown:
                where += " · " + ", ".join(shown)
        self.control.set_position(self.pos, section, where, self.pos / max(n, 1))
        self._update_readouts()

    def _percent(self):
        return self.pos * 100 // max(len(self.script), 1)

    def _seconds_left(self):
        return max(0, len(self.script) - self.pos) / self.speaking_rate() * 60

    def _frame(self, dt):
        if self.mode != "fixed" or not self.playing or not self.script:
            return
        self.fixed_pos = min(float(len(self.script)), self.fixed_pos + self.config["wpm"] / 60 * dt)
        self.view.set_position(self.fixed_pos)
        if int(self.fixed_pos) != self.pos:
            self.set_pos(int(self.fixed_pos), source="fixed")
        if self.fixed_pos >= len(self.script):
            self.on_toggle()

    # --- speech recognition -------------------------------------------------

    def _start_asr(self):
        args = list(self.ASR_ARGS)
        if self.session:
            args += ["--record", str(self.session / "mic.wav"), "--debug"]
        self.asr.extra_args = args
        self.asr_ready = False
        self.engine = self.missing = None
        self.control.set_engine(None)
        self.asr.start(self.config["model"], self.config["mic"], self.config["language"], self.config["device"])
        self._sync_state()

    def _asr_message(self, msg):
        kind = msg.get("type")
        if kind == "ready":
            self.asr_ready = True
            self.engine = (msg.get("model"), msg.get("device"))
            self._set_device(msg.get("device"))
            self.control.set_engine(self.engine)
            self._primed_at = -100
            self.set_pos(self.pos, prime=True)
            self._sync_state()
        elif kind == "level":
            self.control.set_level(msg["rms"])
        elif kind == "hyp":
            if self.mode == "voice" and self.playing and self.aligner:
                new = self.aligner.update(words_of(msg["text"]))
                self._log(type="hyp", at=msg.get("at"), text=msg["text"], ms=msg["ms"], **self.aligner.last)
                if new is not None:
                    self.set_pos(new, source="voice")
                self._set_status(f"Listening · {msg['ms']} ms{self._clicker}")
        elif kind == "gate":
            self._log(**msg)
        elif kind == "missing":
            self.missing = msg.get("model")
            self._set_device(msg.get("device"))
            self.control.set_engine(None, missing=self.missing)
            self._sync_state()
        elif kind == "status":
            self._set_status(msg["msg"])
        elif kind == "error":
            if self.asr.proc is None:  # it stopped; otherwise it carries on (e.g. on the processor)
                self.asr_ready = False
            self._set_status(msg["msg"])

    def set_speech(self, **changes):
        """language, model and/or device: restarts the speech engine with them."""
        changes = {k: v for k, v in changes.items() if self.config[k] != v}
        if not changes:
            return
        self.config.update(changes)
        if "device" in changes:
            self.device = None  # where it ran before says nothing about where it will now
        self.control.set_speech(self.config["language"], self.config["model"], self.config["device"])
        self._save_soon()
        self._start_asr()

    def speech_device(self):
        """Where the speech model runs, or would: what to recommend downloading for."""
        if self.config["device"] == "cpu":
            return "cpu"
        return self.device or ("cuda" if Path("/proc/driver/nvidia/version").exists() else "cpu")

    def _set_device(self, device):
        """Where the engine says it runs, which may not be where speech_device() guessed."""
        if device and device != self.device:
            self.device = device
            self.control.models_changed()  # recommends, and picks "auto", for that device

    # --- speech models: only ever downloaded when you ask -------------------

    def download_model(self, name):
        if self.download:
            return
        self.download = ModelDownload(name, lambda f: self.control.set_download(name, f), self._downloaded)
        self.control.set_download(name, 0.0)

    def cancel_download(self):
        if self.download:
            self.download.cancel()

    def _downloaded(self, error):
        name, self.download = self.download.name, None
        self.control.set_download(None, 0.0)  # which also lists it as downloaded
        if error:
            self.control.models_message(f"Couldn't download {models.BY_NAME[name].label}: {error}")
        elif self._should_switch_to(name):
            self._start_asr()

    def _should_switch_to(self, name):
        """Whether the speech engine should restart now that name has downloaded."""
        if self.missing or self.config["model"] == name:
            return True  # now there's something to run
        if self.config["model"] != "auto":
            return False
        if self.engine is None:
            return self.asr.proc is not None  # still loading, perhaps not the best one any more
        return models.resolve("auto", self.config["language"], self.engine[1])[0] != self.engine[0]

    def delete_model(self, name):
        try:
            models.delete(name)
        except Exception as e:  # e.g. permissions in the Hugging Face cache
            self.control.models_message(f"Couldn't delete it: {e}")
        self.control.models_changed()
        if self.engine and self.engine[0] == name:
            self._start_asr()  # "auto" moves on to another, or asks for one

    def set_mic(self, name):
        if name == self.config["mic"]:
            return
        self.config["mic"] = name
        self._save_soon()
        self._start_asr()  # the worker records from the mic it was started with

    # --- diagnostics (session recordings) -----------------------------------

    def set_recording(self, on):
        if on == bool(self.config["record"]):
            return
        self.config["record"] = on
        self._save_soon()
        self._close_session()
        if on:
            self._open_session()
        self._start_asr()
        self._show_recording()

    def _open_session(self):
        self.session = SESSIONS / time.strftime("%Y%m%d-%H%M%S")
        self.session.mkdir(parents=True, exist_ok=True)
        self._events = open(self.session / "events.jsonl", "a", buffering=1)
        self._copy_script_to_session()

    def _close_session(self):
        if self._events:
            self._events.close()
        self._events = self.session = None

    def _copy_script_to_session(self):
        if self.session and self.file and self.file.exists():  # analysis needs the script as it was read
            (self.session / self.file.name).write_bytes(self.file.read_bytes())
            self._log(type="script", file=self.file.name)

    def sessions_summary(self):
        dirs = [d for d in SESSIONS.iterdir() if d.is_dir()] if SESSIONS.is_dir() else []
        size = sum(f.stat().st_size for d in dirs for f in d.iterdir() if f.is_file())
        return len(dirs), size

    def delete_sessions(self):
        for d in SESSIONS.iterdir() if SESSIONS.is_dir() else []:
            if d.is_dir() and d != self.session:  # keep the one being written right now
                shutil.rmtree(d, ignore_errors=True)
        self._show_recording()

    def _show_recording(self):
        count, size = self.sessions_summary()
        self.control.set_recording(bool(self.config["record"]), count, size)

    def _log(self, **event):
        if self._events:
            self._events.write(json.dumps({"t": round(time.time(), 2), **event}) + "\n")

    # --- actions ------------------------------------------------------------

    def on_toggle(self, *_):
        self.playing = not self.playing
        self._log(type="play" if self.playing else "pause", pos=self.pos, mode=self.mode)
        if self.playing and self.script and self.pos >= len(self.script):
            self.set_pos(0)
        self._rate.clear()
        self._sync_state()

    def on_mode(self, *_):
        self.set_mode("fixed" if self.mode == "voice" else "voice")

    def set_mode(self, mode):
        if mode == self.mode:
            return
        self.config["mode"] = mode
        self._rate.clear()
        self.set_pos(self.pos)
        self._sync_state()
        self._save_soon()

    def on_faster(self, *_):
        self.set_wpm(self.config["wpm"] + 10)

    def on_slower(self, *_):
        self.set_wpm(self.config["wpm"] - 10)

    def on_bigger(self, *_):
        self.set_font_size(self.config["font_size"] + 4)

    def on_smaller(self, *_):
        self.set_font_size(self.config["font_size"] - 4)

    def on_next_section(self, *_):
        if self.script:
            later = [s.token for s in self.script.sections if s.token > self.pos]
            self.jump(later[0] if later else len(self.script))

    def on_prev_section(self, *_):
        if self.script:
            current = self.script.section_at(self.pos)
            # within the first few words of a section, go to the one before it
            if self.pos - self.script.sections[current].token < 4 and current > 0:
                current -= 1
            self.jump(self.script.sections[current].token)

    def on_back(self, *_):
        """Back to the start of the current paragraph, or the previous one if already there."""
        if not self.script or not self.script.tokens:
            return
        block = self.script.tokens[min(self.pos, len(self.script) - 1)].block
        start = self.script.blocks[block].first_token
        if self.pos - start < 3:
            prev = [b for b in self.script.blocks[:block] if b.end_token > b.first_token]
            start = prev[-1].first_token if prev else 0
        self.jump(start)

    def on_restart(self, *_):
        self.stopwatch.reset()
        self.jump(0)

    def on_preview(self, *_):
        self.set_preview("enabled", not self.config["preview"]["enabled"])

    def on_timer(self, *_):
        self.set_readout("timer", not self.config["readouts"]["timer"])

    def on_progress(self, *_):
        self.set_readout("progress", not self.config["readouts"]["progress"])

    def on_calibrate(self, *_):
        self.set_calibrating(not self.calibrating)

    def on_control(self, *_):
        self.control.present()

    def _tray_clicked(self):
        """Show the controls, or hide them if they're already in front."""
        if self.control.is_visible() and self.control.is_active():
            self.control.set_visible(False)
        else:
            self.control.present()

    def on_open_folder(self, *_):
        """Show the scripts in the file manager. "Open containing folder" goes through the
        FileManager1 service, so it opens the real file manager even when the default app for
        folders is something else (a terminal, say)."""
        self.folder.mkdir(parents=True, exist_ok=True)
        if self.file and self.file.exists():
            Gtk.FileLauncher.new(Gio.File.new_for_path(str(self.file))).open_containing_folder(self.control, None, None)
        else:
            Gtk.FileLauncher.new(Gio.File.new_for_path(str(self.folder))).launch(self.control, None, None)

    def on_shortcuts(self, *_):
        self.control.show_shortcuts()

    def on_reload(self, *_):
        if self.file:
            self._load(self.pos)

    def on_quit(self, *_):
        self.quit()

    # --- settings -----------------------------------------------------------

    def set_wpm(self, wpm):
        self.config["wpm"] = int(max(60, min(260, wpm)))
        self._sync_state()
        if self.script:
            self._publish_position()
        self._save_soon()

    def set_font_size(self, size):
        self.config["font_size"] = int(max(24, min(140, size)))
        self.view.set_font_size(self.config["font_size"])
        self._sync_state()
        self._save_soon()

    def set_style(self, group, key, value):
        """group is "appearance" or "crosshair"."""
        self.config[group][key] = value
        self.view.set_style(self.config["appearance"], self.config["crosshair"])
        self.control.set_style(self.config["appearance"], self.config["crosshair"])
        self._save_soon()

    def reset_style(self):
        self.config["appearance"] = {**DEFAULT_APPEARANCE, "center_word": self.config["appearance"]["center_word"]}
        self.config["crosshair"] = {**DEFAULT_CROSSHAIR, "enabled": self.config["crosshair"]["enabled"]}
        self.view.set_style(self.config["appearance"], self.config["crosshair"])
        self.control.set_style(self.config["appearance"], self.config["crosshair"])
        self._save_soon()

    def set_preview(self, key, value):
        self.config["preview"][key] = value
        self._apply_preview()
        self._save_soon()

    def _apply_preview(self):
        """Only hold the camera while the self-view is showing."""
        preview = self.config["preview"]
        self.view.set_preview(preview)
        self.control.set_preview(preview)
        if preview["enabled"]:
            self.camera.start(preview["device"])
        else:
            self.camera.stop()
            self.control.set_camera_status("Off")
        self._update_tray()

    def set_readout(self, key, value):
        self.config["readouts"][key] = value
        self._apply_readouts()
        self._save_soon()

    def _apply_readouts(self):
        self.control.set_readouts(self.config["readouts"])
        self._update_readouts()
        timer = self.config["readouts"]["timer"]
        if timer and not self._readout_tick:  # the view only redraws when the text changes
            self._readout_tick = GLib.timeout_add(250, lambda: self._update_readouts() or GLib.SOURCE_CONTINUE)
        elif not timer and self._readout_tick:
            GLib.source_remove(self._readout_tick)
            self._readout_tick = 0

    def _update_readouts(self):
        readouts = self.config["readouts"]
        shown = []
        if readouts["timer"]:
            if readouts["timer_shows"] == "clock":
                shown.append((readouts["timer_position"], self.clock.text()))
            elif readouts["timer_shows"] != "left":
                shown.append((readouts["timer_position"], duration(self.stopwatch.seconds)))
            elif self.script:
                shown.append((readouts["timer_position"], f"{duration(self._seconds_left())} left"))
        if readouts["progress"] and self.script:
            shown.append((readouts["progress_position"], f"{self._percent()}%"))
        self.view.set_readouts(shown)

    # --- following a clicker ---------------------------------------------------

    def _slide_key(self, action):
        """A slide key on a followed clicker: the presentation just moved, so the notes do too."""
        if not (self.playing and self.script) or self.control.is_active() or self.display.is_active():
            return  # with Prompter in front the key went to it, not the presentation (and its shortcuts act on it)
        self.slide, self.slide_step, pos = decks.follow(self.script, self.pos, self.slide, self.slide_step, action)
        self._log(type="slide", action=action, slide=self.slide, step=self.slide_step)
        if pos != self.pos:
            self.set_pos(pos, source="slides")
        else:
            self._publish_position()  # an animation played, or you'd already started on this slide

    def _following(self):
        """For the status: the clickers being followed, or what's stopping them (nothing if none are switched on)."""
        wanted = self.config["slides"]["devices"]
        present = [d for d in clickers() if d.id in wanted]
        if readable := [d.name for d in present if d.readable]:
            return " · following " + ", ".join(readable)
        if present:
            return f" · {present[0].name} needs a one-time permission: see Setup → Slides"
        return " · your clicker isn't connected" if wanted else ""

    def set_slide_device(self, device_id, follow):
        followed = [d for d in self.config["slides"]["devices"] if d != device_id] + ([device_id] if follow else [])
        self.config["slides"]["devices"] = followed
        self._save_soon()
        self._sync_state()

    def set_calibrating(self, on):
        """Find-the-lens mode: the Prompter goes black apart from the crosshair."""
        self.calibrating = on
        if on and not self.config["crosshair"]["enabled"]:
            self.set_style("crosshair", "enabled", True)
        self.view.set_calibrating(on)
        self.control.set_calibrating(on)

    def nudge_crosshair(self, dx, dy):
        ch = self.config["crosshair"]
        for key, delta in (("x", dx), ("y", dy)):
            if delta:
                self.set_style("crosshair", key, round(max(0.0, min(100.0, ch[key] + delta)), 1))

    def reading_line_to_crosshair(self):
        self.set_style("appearance", "reading_line", int(round(max(10, min(90, self.config["crosshair"]["y"])))))

    def _set_status(self, text):
        self.status = text
        self.control.set_status(text)

    def _sync_state(self):
        voice = self.mode == "voice"
        self.stopwatch.run(self.playing)
        if voice and self.playing:
            self.asr.listen()
        else:
            self.asr.pause()
        # Clickers are only read while you're started
        self.slide_keys.follow(self.config["slides"]["devices"] if self.playing else [])
        self._clicker = self._following() if self.playing else ""
        self.view.set_running(self.playing and not voice)
        self.control.set_state(self.playing, voice, self.config["wpm"], self.config["font_size"])
        if voice and self.missing:
            self._set_status("Voice follow needs a speech model")
        elif voice and not self.asr_ready:
            if self.asr.proc is not None:
                self._set_status("Loading the speech model…")
        elif not self.playing:
            self._set_status("Paused")
        elif not voice:
            self._set_status(f"Scrolling at {self.config['wpm']} words per minute{self._clicker}")
        else:
            self._set_status(f"Listening{self._clicker}")
        self._update_tray()
        self._update_readouts()

    def _update_tray(self):
        voice = self.mode == "voice"
        self.tray.set_menu([
            MenuItem("Show Controls", lambda: self.activate_action("control")),
            SEPARATOR,
            MenuItem("Pause" if self.playing else "Start", lambda: self.activate_action("toggle"), enabled=self.script is not None),
            MenuItem("Voice Follow", lambda: self.activate_action("mode"), checked=voice),
            MenuItem("Camera Self-View", lambda: self.activate_action("preview"), checked=self.config["preview"]["enabled"]),
            SEPARATOR,
            MenuItem("Quit", lambda: self.activate_action("quit")),
        ])
        if not self.playing:
            state = "Paused"
        elif voice:
            state = "Following your voice"
        else:
            state = f"Scrolling at {self.config['wpm']} words per minute"
        self.tray.set_tooltip(f"{script_title(self.file.name) if self.file else 'No script'} · {state}")

    # --- config -------------------------------------------------------------

    def _remember(self):
        if self.file:
            self.config["positions"][str(self.file)] = self.pos
            self.config["positions"].pop(self.file.name, None)  # older configs keyed by name

    def _save_soon(self):
        if not self._save_id:
            self._save_id = GLib.timeout_add_seconds(2, self._save)

    def _save(self):
        self._save_id = 0
        self._remember()
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.config, indent=2))
        tmp.replace(CONFIG)
        return GLib.SOURCE_REMOVE


def default_folder():
    """"Prompter" in the user's Documents folder."""
    documents = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
    return Path(documents or Path.home() / "Documents") / "Prompter"


def script_title(name):
    """"my_first-video.md" -> "My first video"."""
    stem = Path(name).stem.replace("_", " ").replace("-", " ").strip()
    return stem[:1].upper() + stem[1:] if stem else name


def _load_config():
    config = json.loads(json.dumps(DEFAULTS))
    try:
        saved = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return config
    for key, value in saved.items():
        if isinstance(config.get(key), dict) and isinstance(value, dict) and key != "positions":
            config[key].update(value)  # keep defaults for settings added since this was saved
        else:
            config[key] = value
    config["model"] = models.ALIASES.get(config["model"], config["model"])
    if config["mode"] not in ("voice", "fixed"):
        config["mode"] = "voice"
    return config
