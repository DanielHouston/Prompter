import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from prompter import models


def make_model(path):
    path.mkdir(parents=True)
    (path / "model.bin").write_bytes(b"\0")
    (path / "config.json").write_text("{}")
    return path


class ModelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = {"XDG_DATA_HOME": str(root / "data"), "HF_HUB_CACHE": str(root / "hf")}
        self.saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.hf = root / "hf"

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_nothing_downloaded(self):
        self.assertEqual(models.installed(), {})
        self.assertEqual(models.resolve("auto", "en", "cuda"), ("distil-large-v3", None))
        self.assertEqual(models.resolve("auto", "de", "cpu"), ("small", None))
        self.assertEqual(models.resolve("small.en", "en", "cpu"), ("small.en", None))

    def test_finds_our_downloads_and_the_hugging_face_cache(self):
        ours = make_model(models.models_dir() / "small.en")
        repo = self.hf / "models--Systran--faster-distil-whisper-large-v3"
        snapshot = make_model(repo / "snapshots" / "abc123")
        (repo / "refs").mkdir()
        (repo / "refs" / "main").write_text("abc123")
        self.assertEqual(models.installed(), {"small.en": ours, "distil-large-v3": snapshot})

    def test_incomplete_downloads_dont_count(self):
        (models.models_dir() / "tiny").mkdir(parents=True)
        (models.models_dir() / "tiny" / "config.json").write_text("{}")  # no model.bin yet
        self.assertEqual(models.installed(), {})

    def test_automatic_uses_the_best_downloaded_model_for_the_language(self):
        make_model(models.models_dir() / "small.en")
        make_model(models.models_dir() / "base")
        self.assertEqual(models.resolve("auto", "en", "cuda")[0], "small.en")
        self.assertEqual(models.resolve("auto", "en", "cpu")[0], "small.en")
        self.assertEqual(models.resolve("auto", "de", "cuda")[0], "base")  # small.en is English only

    def test_model_folder(self):
        folder = make_model(Path(self.tmp.name) / "my-model")
        self.assertEqual(models.resolve(str(folder), "en", "cpu"), ("my-model", folder))
        self.assertEqual(models.resolve(str(folder.parent / "nope"), "en", "cpu"), ("nope", None))

    def test_delete_our_download(self):
        make_model(models.models_dir() / "tiny.en")
        models.delete("tiny.en")
        self.assertEqual(models.installed(), {})

    def test_titles_and_sizes(self):
        self.assertEqual(models.title("auto"), "Automatic")
        self.assertEqual(models.title("small.en"), "Small (English)")
        self.assertEqual(models.title("large-v3-turbo"), "Large v3 turbo")
        self.assertEqual(models.title("/home/me/models/whisper-nl"), "whisper-nl")
        self.assertEqual((models.size(486), models.size(1516)), ("486 MB", "1.5 GB"))

    def test_catalogue(self):
        self.assertEqual(len(models.BY_NAME), len(models.CATALOGUE))
        for order in models.AUTO.values():
            self.assertTrue(set(order) <= set(models.BY_NAME))

    def test_worker_never_downloads(self):
        """With nothing on this computer the worker says what's missing and stops: no network."""
        try:
            import ctranslate2  # noqa: F401
        except ImportError:
            self.skipTest("the speech engine isn't installed")
        env = dict(os.environ, HF_HUB_OFFLINE="1", **self.env)
        run = subprocess.run([sys.executable, "-m", "prompter.asr_worker", "--model", "auto", "--device", "cpu"],
                             capture_output=True, text=True, timeout=60, env=env, stdin=subprocess.DEVNULL)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual([json.loads(line) for line in run.stdout.splitlines()],
                         [{"type": "missing", "model": "small.en", "device": "cpu"}])


if __name__ == "__main__":
    unittest.main()
