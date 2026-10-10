"""`prompter --install-desktop`: the launcher entry and icon for the current user, which pip and
pipx installs don't provide."""

import os
import shutil
import sys
from importlib import resources
from pathlib import Path

from . import APP_ID


def install():
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    files = resources.files("prompter") / "data"

    launcher = Path(sys.argv[0])
    exe = str(launcher.absolute()) if launcher.name == "prompter" else shutil.which("prompter")
    exe = exe or f"{sys.executable} -m prompter"

    entry = (files / f"{APP_ID}.desktop").read_text().replace("Exec=prompter", f"Exec={exe}")
    apps = data / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / f"{APP_ID}.desktop").write_text(entry)

    icons = data / "icons" / "hicolor" / "scalable" / "apps"
    icons.mkdir(parents=True, exist_ok=True)
    (icons / f"{APP_ID}.svg").write_bytes((files / f"{APP_ID}.svg").read_bytes())

    print(f"Installed {apps / f'{APP_ID}.desktop'} (runs {exe})")
    print(f"Installed {icons / f'{APP_ID}.svg'}")
