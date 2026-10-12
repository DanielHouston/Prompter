import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gi.repository import GLib

from prompter import asr

MISSING = '{"type": "missing", "model": "small.en", "device": "cpu"}'


class AsrTest(unittest.TestCase):
    """Runs the worker and downloader with a stand-in for Python that just prints what they would."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loop = GLib.MainLoop()

    def python(self, script):
        """A stand-in for sys.executable that ignores its arguments and runs this shell script."""
        path = Path(self.tmp.name) / "python"
        path.write_text("#!/bin/sh\n" + script + "\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        patch = mock.patch.object(sys, "executable", str(path))
        patch.start()
        self.addCleanup(patch.stop)

    def run_loop(self, until):
        GLib.timeout_add_seconds(5, self.loop.quit)
        def check():
            if until():
                self.loop.quit()
                return False
            return True
        GLib.timeout_add(20, check)
        self.loop.run()

    def test_worker_missing_after_it_exits(self):
        """Its last line can be read after it has exited: still "missing", not "stopped"."""
        self.python(f"( sleep 0.3; echo '{MISSING}' ) &")  # exits first; the line comes later
        messages = []
        worker = asr.AsrWorker(messages.append)
        worker.start("auto")
        self.run_loop(lambda: worker.proc is None)
        self.assertEqual([m["type"] for m in messages], ["missing"])

    def test_worker_that_stops_says_so(self):
        self.python("exit 1")
        messages = []
        worker = asr.AsrWorker(messages.append)
        worker.start("auto")
        self.run_loop(lambda: worker.proc is None)
        self.assertEqual([m["type"] for m in messages], ["error"])

    def test_a_failing_handler_doesnt_stop_the_reading(self):
        """A message handler that raises is a bug, but the lines after it and the exit still get through."""
        self.python("""echo '{"type": "status", "msg": "one"}'
echo '{"type": "status", "msg": "two"}'
exit 1""")
        messages, errors = [], []

        def handle(msg):
            messages.append(msg)
            if len(messages) == 1:
                raise KeyError("ms")

        with mock.patch.object(sys, "excepthook", lambda kind, *_: errors.append(kind)):
            worker = asr.AsrWorker(handle)
            worker.start("auto")
            self.run_loop(lambda: worker.proc is None)
        self.assertEqual([m["type"] for m in messages], ["status", "status", "error"])  # the error: it stopped
        self.assertEqual(errors, [KeyError])  # still reported, as an unhandled error

    def test_download_error_after_it_exits(self):
        self.python("""( sleep 0.3; echo '{"type": "progress", "done": 1, "total": 2}'
  echo '{"type": "error", "msg": "No space left"}' ) &
exit 1""")
        progress, finished = [], []
        asr.ModelDownload("tiny", progress.append, finished.append)
        self.run_loop(lambda: finished)
        self.assertEqual((progress, finished), ([0.5], ["No space left"]))

    def test_download_that_cant_start(self):
        with mock.patch.object(sys, "executable", os.path.join(self.tmp.name, "nothing-here")):
            finished = []
            asr.ModelDownload("tiny", lambda _f: None, finished.append)
        self.run_loop(lambda: finished)
        self.assertEqual(len(finished), 1)
        self.assertTrue(finished[0])  # why it couldn't


if __name__ == "__main__":
    unittest.main()
