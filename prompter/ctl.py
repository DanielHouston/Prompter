"""prompterctl: drive the running prompter from a keybinding or a script, starting it if needed.

    prompterctl toggle            start or pause
    prompterctl next-section      ...any action below

Global shortcuts through the desktop's portal are the first choice; this is for compositors
without one, or for scripts.
"""

import subprocess
import sys
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from . import APP_ID  # noqa: E402
from .actions import ACTIONS  # noqa: E402


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    action = argv[0] if argv else "control"
    if action in ("-h", "--help") or action not in ACTIONS:
        print(f"usage: prompterctl [{'|'.join(ACTIONS)}]")
        return 0 if action in ("-h", "--help") else 2

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    if not running(bus):
        if action == "quit":
            return 0
        subprocess.Popen([sys.executable, "-m", "prompter"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            time.sleep(0.05)
            if running(bus):
                break
        else:
            print("prompterctl: the prompter didn't start", file=sys.stderr)
            return 1
        if action == "control":
            return 0  # starting it shows the control window already

    activate(bus, action)
    return 0


def running(bus, app_id=APP_ID):
    reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
                          GLib.Variant("(s)", (app_id,)), GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, -1, None)
    return reply.unpack()[0]


def activate(bus, action, app_id=APP_ID):
    """GApplication exports its actions on the session bus under its ID."""
    bus.call_sync(app_id, "/" + app_id.replace(".", "/"), "org.gtk.Actions", "Activate",
                  GLib.Variant("(sava{sv})", (action, [], {})), None, Gio.DBusCallFlags.NONE, -1, None)


if __name__ == "__main__":
    sys.exit(main())
