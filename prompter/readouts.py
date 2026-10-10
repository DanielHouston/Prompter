"""The timer and progress shown on the Prompter."""

import time

from gi.repository import Gio, GLib

DEFAULT_READOUTS = {
    "timer": False,
    "timer_shows": "elapsed",  # elapsed | left | clock
    "timer_position": "top-left",
    "progress": False,  # how far through the script, in percent
    "progress_position": "top-right",
}
# Where a readout can sit; two in the same place go side by side
POSITIONS = (("top-left", "Top left"), ("top-center", "Top centre"), ("top-right", "Top right"),
             ("bottom-left", "Bottom left"), ("bottom-center", "Bottom centre"), ("bottom-right", "Bottom right"))
# What the timer can show: config value, label, what it means
TIMER_SHOWS = (
    ("elapsed", "Reading time", "Runs while the script does; Back to the Top resets it"),
    ("left", "Time left", "Roughly, at the speed you're reading"),
    ("clock", "Time of day", "In your desktop's 12- or 24-hour format"),
)


class Stopwatch:
    """Reading time: runs while the script does, so pauses between takes don't count."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._since = None  # when it last started, while it's running
        self._total = 0.0  # from earlier runs

    def run(self, running):
        if running and self._since is None:
            self._since = self._clock()
        elif not running and self._since is not None:
            self._total += self._clock() - self._since
            self._since = None

    def reset(self):
        self._total = 0.0
        if self._since is not None:
            self._since = self._clock()

    @property
    def seconds(self):
        return self._total + (self._clock() - self._since if self._since is not None else 0.0)


def duration(seconds):
    """4:05, or 1:02:03 past an hour."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


class Clock:
    """The time of day, in the desktop's 12- or 24-hour format where it has one."""

    def __init__(self):
        source = Gio.SettingsSchemaSource.get_default()
        schema = source.lookup("org.gnome.desktop.interface", True) if source else None
        self.settings = Gio.Settings.new("org.gnome.desktop.interface") if schema and schema.has_key("clock-format") else None

    def text(self, now=None):
        now = now or GLib.DateTime.new_now_local()
        twelve_hour = self.settings is not None and self.settings.get_string("clock-format") == "12h"
        return now.format("%-I:%M %p" if twelve_hour else "%H:%M").strip()
