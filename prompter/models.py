"""Speech models: the ones Prompter offers, which are on this computer, and downloading them.

Nothing here touches the network except download(), which only runs when you ask for a model
in Setup. Downloads go to ~/.local/share/prompter/models. Models already in the Hugging Face
cache (from earlier versions of Prompter, or other apps) are used where they are.

    python -m prompter.models download NAME   (what the app runs: JSON lines on stdout)

Plain Python with no GTK, so the speech worker can use it too.
"""

import json
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Model:
    name: str  # faster-whisper's name for it
    repo: str  # where it's downloaded from on Hugging Face
    label: str
    mb: int  # download size
    english_only: bool
    needs_gpu: bool  # too slow to keep up with speech on a processor


CATALOGUE = (
    Model("tiny.en", "Systran/faster-whisper-tiny.en", "Tiny", 78, True, False),
    Model("tiny", "Systran/faster-whisper-tiny", "Tiny", 78, False, False),
    Model("base.en", "Systran/faster-whisper-base.en", "Base", 148, True, False),
    Model("base", "Systran/faster-whisper-base", "Base", 148, False, False),
    Model("distil-small.en", "Systran/faster-distil-whisper-small.en", "Distil small", 336, True, False),
    Model("small.en", "Systran/faster-whisper-small.en", "Small", 486, True, False),
    Model("small", "Systran/faster-whisper-small", "Small", 486, False, False),
    Model("distil-medium.en", "Systran/faster-distil-whisper-medium.en", "Distil medium", 792, True, True),
    Model("medium.en", "Systran/faster-whisper-medium.en", "Medium", 1530, True, True),
    Model("medium", "Systran/faster-whisper-medium", "Medium", 1531, False, True),
    Model("distil-large-v3", "Systran/faster-distil-whisper-large-v3", "Distil large v3", 1516, True, True),
    Model("distil-large-v3.5", "distil-whisper/distil-large-v3.5-ct2", "Distil large v3.5", 1516, True, True),
    Model("large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "Large v3 turbo", 1622, False, True),
    Model("large-v3", "Systran/faster-whisper-large-v3", "Large v3", 3091, False, True),
)
BY_NAME = {m.name: m for m in CATALOGUE}

# "Automatic" uses the first of these that's downloaded, by (device, reading English). The first
# is what Setup recommends downloading: accurate, and fast enough to keep up on that device.
AUTO = {
    ("cuda", True): ("distil-large-v3", "distil-large-v3.5", "large-v3-turbo", "large-v3", "medium.en",
                     "distil-medium.en", "medium", "small.en", "distil-small.en", "small", "base.en", "base",
                     "tiny.en", "tiny"),
    ("cuda", False): ("large-v3-turbo", "large-v3", "medium", "small", "base", "tiny"),
    ("cpu", True): ("small.en", "distil-small.en", "small", "base.en", "base", "tiny.en", "tiny", "distil-medium.en",
                    "medium.en", "medium", "distil-large-v3", "distil-large-v3.5", "large-v3-turbo", "large-v3"),
    ("cpu", False): ("small", "base", "tiny", "medium", "large-v3-turbo", "large-v3"),
}
FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def models_dir():
    data = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data) / "prompter" / "models"


def hf_cache():
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    home = os.environ.get("HF_HOME") or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "huggingface"
    return Path(home) / "hub"


def is_model_dir(path):
    """A CTranslate2 Whisper model: what faster-whisper loads."""
    path = Path(path)
    return (path / "model.bin").is_file() and (path / "config.json").is_file()


def find(name):
    """Where a catalogue model is on this computer, or None."""
    own = models_dir() / name
    if is_model_dir(own):
        return own
    model = BY_NAME.get(name)
    if model is None:
        return None
    repo = hf_cache() / ("models--" + model.repo.replace("/", "--"))
    try:
        revision = (repo / "refs" / "main").read_text().strip()
        if is_model_dir(repo / "snapshots" / revision):
            return repo / "snapshots" / revision
    except OSError:
        pass
    return next((p for p in sorted((repo / "snapshots").glob("*")) if is_model_dir(p)), None)


def installed():
    """{name: path} for the catalogue models on this computer."""
    return {m.name: path for m in CATALOGUE if (path := find(m.name))}


def title(choice):
    """How a model choice reads in the app."""
    if choice == "auto":
        return "Automatic"
    model = BY_NAME.get(choice)
    if model is None:
        return Path(choice).name  # a model folder
    return f"{model.label} (English)" if model.english_only else model.label


def size(mb):
    return f"{mb} MB" if mb < 1000 else f"{mb / 1000:.1f} GB"


def suits(model, language):
    return language == "en" or not model.english_only


def resolve(choice, language, device):
    """(name, path) for a choice of "auto", a catalogue name or a model folder. path is None when
    it isn't on this computer; for "auto" the name is then the one to recommend downloading."""
    if choice not in BY_NAME and choice != "auto":
        return Path(choice).name, Path(choice) if is_model_dir(choice) else None
    if choice != "auto":
        return choice, find(choice)
    order = AUTO[(device, language == "en")]
    for name in order:
        if path := find(name):
            return name, path
    return order[0], None


def recommended(language, device):
    return AUTO[(device, language == "en")][0]


def delete(name):
    """Remove a downloaded model: ours, or the Hugging Face cache's copy."""
    path = find(name)
    if path is None:
        return
    if path.parent == models_dir():
        shutil.rmtree(path)
        return
    from huggingface_hub import scan_cache_dir

    cache = scan_cache_dir(hf_cache())
    hashes = [rev.commit_hash for repo in cache.repos if repo.repo_id == BY_NAME[name].repo for rev in repo.revisions]
    cache.delete_revisions(*hashes).execute()


# --- downloading (its own process, so the app can show progress and cancel it) -------------


def emit(**msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def download(name):
    """Download a catalogue model into models_dir(), reporting progress as JSON lines. Goes to a
    .part folder first, so a model folder only ever appears complete; an interrupted download
    picks up where it stopped next time."""
    model = BY_NAME[name]
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from huggingface_hub import snapshot_download
    from tqdm.std import tqdm

    bars = []

    class Progress(tqdm):
        """Collects huggingface_hub's progress bars instead of printing them."""

        def __init__(self, *args, **kwargs):
            kwargs.pop("name", None)
            kwargs.update(file=open(os.devnull, "w"), disable=False)
            super().__init__(*args, **kwargs)
            bars.append(self)

    part = models_dir() / f".{name}.part"
    part.mkdir(parents=True, exist_ok=True)
    total = model.mb * 1_000_000
    done = threading.Event()

    def report():
        while not done.wait(0.5):
            # Bytes written, from the library where it reports them; else what's on disk so far
            counted = max((b.n for b in bars if b.unit == "B"), default=0)
            on_disk = sum(f.stat().st_size for f in part.rglob("*") if f.is_file())
            emit(type="progress", done=min(max(counted, on_disk), total), total=total)

    threading.Thread(target=report, daemon=True).start()
    try:
        snapshot_download(model.repo, local_dir=part, allow_patterns=FILES, tqdm_class=Progress)
    finally:
        done.set()
    shutil.rmtree(part / ".cache", ignore_errors=True)  # the library's bookkeeping
    if not is_model_dir(part):
        raise RuntimeError(f"{model.repo} isn't a faster-whisper model")
    target = models_dir() / name
    shutil.rmtree(target, ignore_errors=True)
    part.rename(target)
    emit(type="progress", done=total, total=total)
    emit(type="done", path=str(target))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "download" or argv[1] not in BY_NAME:
        print(f"usage: python -m prompter.models download [{'|'.join(BY_NAME)}]", file=sys.stderr)
        return 2
    try:
        download(argv[1])
    except Exception as e:  # network, disk space, ...: tell the app why
        emit(type="error", msg=str(e) or type(e).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
