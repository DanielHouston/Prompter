"""Prompter: a voice-following teleprompter for the Elgato Prompter on Linux (Wayland)."""

APP_ID = "io.github.DanielHouston.Prompter"
__version__ = "0.1.0"


def tilde(path):
    """A path as it reads in the app: ~ for your home folder."""
    from pathlib import Path

    path, home = str(path), str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path
