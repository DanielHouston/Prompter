"""Following a clicker, so the notes move when the slides do.

Wayland doesn't let one app see the keys pressed in another, so Prompter reads the clickers
you choose directly (Linux evdev). It reads without taking them over: every press still
reaches your presentation. It only opens them while you're started, and only reacts to
slide keys pressed on their own (not with Ctrl, Alt, Shift or Super). Reading a clicker needs
a one-time permission for just that device: see rule().

Only clickers, never keyboards: being able to read a keyboard means being able to read
everything typed on it, which isn't worth having for this.
"""

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

from gi.repository import Gio, GLib

EVENT = struct.Struct("llHHi")  # struct input_event: seconds, microseconds, type, code, value
EV_KEY = 1
LONG_BITS = struct.calcsize("l") * 8  # the kernel's capability bitmaps are in longs
# Linux key codes: what presentation apps and clickers use to move between slides
ACTIONS = {
    109: "next", 106: "next", 108: "next", 57: "next", 28: "next", 96: "next",  # Page Down, →, ↓, Space, Enter
    104: "previous", 105: "previous", 103: "previous", 14: "previous",  # Page Up, ←, ↑, Backspace
    102: "first", 107: "last",  # Home, End
}
MODIFIERS = {29, 97, 42, 54, 56, 100, 125, 126}  # Ctrl, Shift, Alt, Super, left and right
KEY_A, KEY_Z, KEY_RIGHT, KEY_PAGEDOWN = 30, 44, 106, 109
# Some clickers say they're whole keyboards (letters and all); their names give them away
CLICKER_WORDS = ("present", "clicker", "remote", "pointer", "spotlight")


@dataclass
class Device:
    id: str  # "vendor:product": stays the same when it's plugged in again
    name: str
    nodes: list = field(default_factory=list)  # its /dev/input/event* files
    letters: bool = False  # it can type letters, as keyboards (and some clickers) can
    readable: bool = True


def clickers(sys_root="/sys/class/input", dev_root="/dev/input"):
    """Clickers (presentation remotes): devices with slide keys that aren't keyboards. One
    entry per physical device, which may show up as several event files."""
    return [d for d in devices(sys_root, dev_root) if is_clicker(d)]


def is_clicker(device):
    return not device.letters or any(word in device.name.lower() for word in CLICKER_WORDS)


def devices(sys_root="/sys/class/input", dev_root="/dev/input"):
    """Everything with Page Down or →: clickers and keyboards (and mice that type)."""
    found = {}
    for event in sorted(Path(sys_root).glob("event*"), key=lambda p: int(p.name[5:] or 0)):
        info = event / "device"
        keys = key_codes(_read(info / "capabilities" / "key"))
        if not keys & {KEY_RIGHT, KEY_PAGEDOWN}:
            continue
        vendor, product, name = _read(info / "id" / "vendor"), _read(info / "id" / "product"), _read(info / "name")
        device = found.setdefault(f"{vendor}:{product}", Device(f"{vendor}:{product}", name))
        if len(name) < len(device.name):  # "Logitech R400", not "Logitech R400 Consumer Control"
            device.name = name
        node = f"{dev_root}/{event.name}"
        device.nodes.append(node)
        device.letters = device.letters or {KEY_A, KEY_Z} <= keys
        device.readable = device.readable and os.access(node, os.R_OK)
    return list(found.values())


def key_codes(bitmap):
    """The key codes set in a capabilities bitmap from sysfs (hex longs, most significant first)."""
    codes = set()
    for i, word in enumerate(reversed(bitmap.split())):
        value = int(word, 16)
        while value:
            low = value & -value
            codes.add(i * LONG_BITS + low.bit_length() - 1)
            value ^= low
    return codes


def rule(device):
    """The one-time permission, as shell commands: a udev rule that lets whoever is at the
    computer (not other users, nor the network) read just this device."""
    vendor, product = device.id.split(":")
    match = f'SUBSYSTEM=="input", KERNEL=="event*", ATTRS{{id/vendor}}=="{vendor}", ATTRS{{id/product}}=="{product}"'
    if vendor in ("0000", "0001") and not set("'\"\\") & set(device.name):  # virtual devices share ids
        match += f', ATTRS{{name}}=="{device.name}"'
    path = f"/etc/udev/rules.d/71-prompter-{vendor}-{product}.rules"
    return (f"echo '{match}, TAG+=\"uaccess\"' | sudo tee {path}\n"
            f"sudo udevadm control --reload && sudo udevadm trigger --action=change {' '.join(device.nodes)}")


def decode(data, held):
    """Slide actions from raw input events. held: modifier keys down, kept between calls."""
    actions = []
    for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
        _sec, _usec, kind, code, value = EVENT.unpack_from(data, offset)
        if kind != EV_KEY:
            continue
        if code in MODIFIERS:
            (held.add if value else held.discard)(code)
        elif value == 1 and not held and code in ACTIONS:  # presses, not repeats or releases
            actions.append(ACTIONS[code])
    return actions


class SlideKeys:
    """Reads the followed clickers and calls on_action("next" | "previous" | "first" | "last").
    on_devices() when clickers come and go, or their permissions change."""

    def __init__(self, on_action, on_devices):
        self.on_action, self.on_devices = on_action, on_devices
        self.wanted = set()
        self.open = {}  # node -> (fd, watch, modifiers held)
        self._refresh_id = 0
        self.monitor = Gio.File.new_for_path("/dev/input").monitor_directory(Gio.FileMonitorFlags.NONE, None)
        self.monitor.connect("changed", lambda *_: self._refresh_soon())

    def follow(self, ids):
        """Read these clickers (by id), and nothing else; an empty list closes them all."""
        self.wanted = set(ids)
        self.refresh()

    def refresh(self):
        nodes = {node for d in clickers() if d.id in self.wanted and d.readable for node in d.nodes}
        for node in set(self.open) - nodes:
            self._close(node)
        for node in nodes - set(self.open):
            self._open(node)

    def _refresh_soon(self):
        if not self._refresh_id:  # plugging in makes a burst of changes, and permissions land after
            self._refresh_id = GLib.timeout_add(600, self._refreshed)

    def _refreshed(self):
        self._refresh_id = 0
        self.refresh()
        self.on_devices()
        return GLib.SOURCE_REMOVE

    def _open(self, node):
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            return
        channel = GLib.IOChannel.unix_new(fd)
        watch = GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT,
                                  GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR, self._ready, node)
        self.open[node] = (fd, watch, set())

    def _close(self, node):
        fd, watch, _held = self.open.pop(node)
        GLib.source_remove(watch)
        os.close(fd)

    def _ready(self, _channel, condition, node):
        if node not in self.open:
            return False
        fd, watch, held = self.open[node]
        if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            self.open.pop(node)
            os.close(fd)
            return False  # unplugged: it's reopened if it comes back
        try:
            data = os.read(fd, EVENT.size * 64)
        except BlockingIOError:
            return True
        except OSError:
            self.open.pop(node)
            os.close(fd)
            return False
        for action in decode(data, held):
            self.on_action(action)
        return True


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""
