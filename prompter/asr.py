"""Runs the speech recognition worker (prompter.asr_worker) and relays its messages."""

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

from gi.repository import Gio, GLib

LOG = Path(GLib.get_user_cache_dir()) / "prompter" / "asr.log"


def cuda_library_path():
    """Where pip's NVIDIA wheels (the "cuda" extra) put cuBLAS/cuDNN, which faster-whisper's
    CTranslate2 loads at runtime; empty if they aren't installed (system CUDA or CPU only)."""
    spec = importlib.util.find_spec("nvidia")
    dirs = []
    for root in (spec.submodule_search_locations or []) if spec else []:
        dirs += [str(Path(root) / lib / "lib") for lib in ("cublas", "cudnn") if (Path(root) / lib / "lib").is_dir()]
    return ":".join(dirs)


class AsrWorker:
    def __init__(self, on_message, extra_args=()):
        self.on_message = on_message
        self.extra_args = list(extra_args)  # e.g. ["--record", path] for diagnostics
        self.proc = None
        self.stdin = None
        self.missing = False  # the worker said its model isn't downloaded, then stopped

    def start(self, model, mic=None, language="en", device="auto"):
        self.stop()
        self.missing = False
        LOG.parent.mkdir(parents=True, exist_ok=True)
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE)
        launcher.set_stderr_file_path(str(LOG))
        libs = cuda_library_path()
        if libs:
            launcher.setenv("LD_LIBRARY_PATH", ":".join(p for p in (libs, os.environ.get("LD_LIBRARY_PATH")) if p), True)
        launcher.setenv("HF_HUB_DISABLE_TELEMETRY", "1", True)
        launcher.setenv("HF_HUB_OFFLINE", "1", True)  # models are only ever downloaded when you ask

        argv = [sys.executable, "-m", "prompter.asr_worker", "--model", model, "--language", language,
                "--device", device]
        if mic:
            argv += ["--mic", mic]
        argv += self.extra_args
        try:
            self.proc = launcher.spawnv(argv)
        except GLib.Error as e:
            self.on_message({"type": "error", "msg": e.message})
            return

        self.stdin = self.proc.get_stdin_pipe()
        stdout = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self._read(stdout, self.proc)
        self.proc.wait_async(None, self._exited)

    def stop(self):
        if self.proc:
            self.send(cmd="quit")
            proc = self.proc
            GLib.timeout_add_seconds(3, lambda: proc.force_exit() or False)
        self.proc = self.stdin = None

    def listen(self):
        self.send(cmd="listen")

    def pause(self):
        self.send(cmd="pause")

    def prompt(self, text):
        self.send(cmd="prompt", text=text)

    def send(self, **msg):
        if not self.stdin:
            return
        try:
            self.stdin.write_all((json.dumps(msg) + "\n").encode(), None)
        except GLib.Error:
            self.stdin = None

    def _read(self, stream, proc):
        def on_line(stream, result):
            try:
                line, _ = stream.read_line_finish_utf8(result)
            except GLib.Error:
                return
            if line is None or proc is not self.proc:
                return
            try:
                msg = json.loads(line)
            except ValueError:
                msg = None
            if msg:
                self.missing = self.missing or msg.get("type") == "missing"
                self.on_message(msg)
            self._read(stream, proc)

        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)

    def _exited(self, proc, result):
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        if proc is self.proc:
            self.proc = self.stdin = None
            if not self.missing:  # that stop was expected
                self.on_message({"type": "error", "msg": f"The speech engine stopped (see {LOG})"})


class ModelDownload:
    """Downloads a speech model in the background (`python -m prompter.models download NAME`):
    the only thing in Prompter that goes online, and only when you ask."""

    def __init__(self, name, on_progress, on_finished):
        """on_progress(fraction); on_finished(error), error None on success or "" if cancelled."""
        self.name = name
        self.on_progress, self.on_finished = on_progress, on_finished
        self.error = None
        self.cancelled = False
        LOG.parent.mkdir(parents=True, exist_ok=True)
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE)
        launcher.set_stderr_file_path(str(LOG.with_name("download.log")))
        launcher.setenv("HF_HUB_DISABLE_TELEMETRY", "1", True)
        launcher.unsetenv("HF_HUB_OFFLINE")
        try:
            self.proc = launcher.spawnv([sys.executable, "-m", "prompter.models", "download", name])
        except GLib.Error as e:
            self.proc = None
            GLib.idle_add(lambda: on_finished(e.message) and False)
            return
        self._read(Gio.DataInputStream.new(self.proc.get_stdout_pipe()))
        self.proc.wait_async(None, self._exited)

    def cancel(self):
        if self.proc:
            self.cancelled = True
            self.proc.force_exit()

    def _read(self, stream):
        def on_line(stream, result):
            try:
                line, _ = stream.read_line_finish_utf8(result)
            except GLib.Error:
                return
            if line is None:
                return
            try:
                msg = json.loads(line)
            except ValueError:
                msg = {}
            if msg.get("type") == "progress" and msg.get("total"):
                self.on_progress(msg["done"] / msg["total"])
            elif msg.get("type") == "error":
                self.error = msg.get("msg") or "The download failed"
            self._read(stream)

        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)

    def _exited(self, proc, result):
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        if self.cancelled:
            from .models import models_dir  # a cancelled download shouldn't keep the space

            shutil.rmtree(models_dir() / f".{self.name}.part", ignore_errors=True)
            self.on_finished("")
        elif proc.get_if_exited() and proc.get_exit_status() == 0:
            self.on_finished(None)
        else:
            self.on_finished(self.error or f"The download stopped (see {LOG.with_name('download.log')})")
