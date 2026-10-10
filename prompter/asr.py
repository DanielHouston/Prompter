"""Runs the speech recognition worker (prompter.asr_worker) and relays its messages."""

import importlib.util
import json
import os
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

    def start(self, model, mic=None, language="en"):
        self.stop()
        LOG.parent.mkdir(parents=True, exist_ok=True)
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE)
        launcher.set_stderr_file_path(str(LOG))
        libs = cuda_library_path()
        if libs:
            launcher.setenv("LD_LIBRARY_PATH", ":".join(p for p in (libs, os.environ.get("LD_LIBRARY_PATH")) if p), True)
        launcher.setenv("HF_HUB_DISABLE_TELEMETRY", "1", True)

        argv = [sys.executable, "-m", "prompter.asr_worker", "--model", model, "--language", language]
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
                self.on_message(json.loads(line))
            except ValueError:
                pass
            self._read(stream, proc)

        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)

    def _exited(self, proc, result):
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        if proc is self.proc:
            self.proc = self.stdin = None
            self.on_message({"type": "error", "msg": f"The speech engine stopped (see {LOG})"})
