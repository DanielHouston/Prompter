"""Minimal DDC/CI (VESA MCCS) client for the Elgato Prompter's brightness.

ddcutil won't use the DisplayLink bus: evdi only forwards the DDC/CI address (0x37), not the
EDID address it probes first. So this speaks the two MCCS messages needed directly over
/dev/i2c-N: Get VCP Feature and Set VCP Feature.
"""

import fcntl
import os
import threading
import time
from pathlib import Path

from gi.repository import GLib

I2C_SLAVE = 0x0703
DDCCI_ADDR = 0x37
HOST = 0x51  # source address byte of host messages
REPLY_DELAY = 0.05  # MCCS: wait >= 40 ms before reading a reply

BRIGHTNESS, CONTRAST, POWER_MODE = 0x10, 0x12, 0xD6


class DdcError(Exception):
    pass


def find_bus(adapter_name="DisplayLink I2C Adapter"):
    """The /dev/i2c-N belonging to the DisplayLink adapter, or None."""
    for dev in sorted(Path("/sys/class/i2c-dev").glob("i2c-*")):
        try:
            if (dev / "name").read_text().strip() == adapter_name:
                return f"/dev/{dev.name}"
        except OSError:
            continue
    return None


def _checksum(first, data):
    c = first
    for b in data:
        c ^= b
    return c


class Ddc:
    def __init__(self, path):
        self.path = path

    def _transact(self, payload, reply_len=0):
        msg = bytes([HOST, 0x80 | len(payload), *payload])
        msg += bytes([_checksum(DDCCI_ADDR << 1, msg)])
        fd = os.open(self.path, os.O_RDWR)
        try:
            fcntl.ioctl(fd, I2C_SLAVE, DDCCI_ADDR)
            os.write(fd, msg)
            if not reply_len:
                time.sleep(REPLY_DELAY)  # give the monitor time before the next command
                return b""
            time.sleep(REPLY_DELAY)
            return os.read(fd, reply_len)
        finally:
            os.close(fd)

    def get(self, code):
        """(current, maximum) of a VCP feature."""
        for _ in range(3):  # DDC is flaky by nature; MCCS expects retries
            reply = self._transact([0x01, code], reply_len=11)
            if len(reply) == 11 and reply[2] == 0x02 and reply[4] == code and \
                    _checksum(0x50, reply[:-1]) == reply[-1]:
                if reply[3] != 0:
                    raise DdcError(f"VCP 0x{code:02x} not supported")
                return (reply[8] << 8) | reply[9], (reply[6] << 8) | reply[7]
            time.sleep(REPLY_DELAY)
        raise DdcError(f"no valid reply for VCP 0x{code:02x} (last: {reply.hex()})")

    def set(self, code, value):
        self._transact([0x03, code, (value >> 8) & 0xFF, value & 0xFF])


class ScreenControl:
    """Brightness/contrast for the app: DDC runs on a worker thread (each exchange takes
    ~100 ms) and rapid slider moves collapse into the latest value."""

    FEATURES = {"brightness": BRIGHTNESS, "contrast": CONTRAST}

    def __init__(self, on_values):
        self.on_values = on_values  # (dict name -> (current, max)) or (str error), on the main thread
        self.bus = find_bus()
        self._ddc = Ddc(self.bus) if self.bus else None
        self._pending, self._refresh = {}, False
        self._lock, self._wake = threading.Lock(), threading.Event()
        if self._ddc:
            threading.Thread(target=self._run, daemon=True).start()

    @property
    def available(self):
        return self._ddc is not None

    def refresh(self):
        with self._lock:
            self._refresh = True
        self._wake.set()

    def set(self, name, value):
        with self._lock:
            self._pending[self.FEATURES[name]] = int(value)
        self._wake.set()

    def _run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                pending, self._pending = self._pending, {}
                refresh, self._refresh = self._refresh, False
            try:
                for code, value in pending.items():
                    self._ddc.set(code, value)
                if refresh:
                    values = {name: self._ddc.get(code) for name, code in self.FEATURES.items()}
                    GLib.idle_add(self.on_values, values)
            except (DdcError, OSError) as e:
                GLib.idle_add(self.on_values, f"Screen control failed: {e}")
