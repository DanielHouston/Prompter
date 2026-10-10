"""Camera self-view for the prompter: frames from a V4L2 camera as GTK textures, via ffmpeg.

A camera can usually be read by only one app at a time (v4l2loopback devices included), so
to see yourself here while Zoom or OBS uses the same camera, give the prompter its own copy:
see "Camera self-view" in the README.
"""

import fcntl
import glob
import os
import struct

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib  # noqa: E402

WIDTH, HEIGHT = 480, 270
FRAME = WIDTH * HEIGHT * 3
RETRY_SECONDS = 3


class CameraFeed:
    def __init__(self, on_frame, on_status):
        self.on_frame = on_frame  # (Gdk.Texture) -> None
        self.on_status = on_status  # (str) -> None
        self.proc = None
        self.wanted = False
        self.device = None
        self.input_args = None  # tests can swap the camera for another ffmpeg input
        self._retry = 0

    def start(self, device):
        if self.proc and device != self.device:
            self.stop()  # switching cameras
        self.wanted, self.device = True, device
        if not device:
            self.on_status("Choose a camera below")
            return
        if not self.proc:
            self._spawn()

    def stop(self):
        self.wanted = False
        if self._retry:
            GLib.source_remove(self._retry)
            self._retry = 0
        if self.proc:
            self.proc.force_exit()
            self.proc = None

    def _spawn(self):
        self._retry = 0
        source = self.input_args or ["-f", "v4l2", "-i", self.device]
        argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *source,
                "-vf", f"scale={WIDTH}:{HEIGHT}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        try:
            self.proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error as e:
            self.on_status(f"Can't start ffmpeg: {e.message}")
            return
        self.on_status("Connecting to the camera…")
        self._read(self.proc, self.proc.get_stdout_pipe(), bytearray())
        self.proc.wait_async(None, self._exited)

    def _read(self, proc, stream, buf):
        def got(stream, result):
            try:
                chunk = stream.read_bytes_finish(result)
            except GLib.Error:
                return
            if proc is not self.proc or chunk.get_size() == 0:
                return
            if not buf and not getattr(proc, "live", False):
                proc.live = True
                self.on_status("Live")
            buf.extend(chunk.get_data())
            while len(buf) >= FRAME:
                frame = GLib.Bytes.new(bytes(buf[:FRAME]))
                del buf[:FRAME]
                self.on_frame(Gdk.MemoryTexture.new(WIDTH, HEIGHT, Gdk.MemoryFormat.R8G8B8, frame, WIDTH * 3))
            self._read(proc, stream, buf)

        stream.read_bytes_async(FRAME - len(buf), GLib.PRIORITY_DEFAULT, None, got)

    def _exited(self, proc, result):
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        if proc is not self.proc:
            return
        self.proc = None
        self.on_frame(None)
        if self.wanted:  # camera off, busy, or not set up yet: keep trying in the background
            self.on_status(f"No picture from {self.device} (in use by another app, or off). Retrying…")
            self._retry = GLib.timeout_add_seconds(RETRY_SECONDS, self._respawn)

    def _respawn(self):
        self._retry = 0
        if self.wanted and not self.proc:
            self._spawn()
        return GLib.SOURCE_REMOVE


VIDIOC_QUERYCAP = 0x80685600  # _IOR('V', 0, struct v4l2_capability), 104 bytes
CAP_VIDEO_CAPTURE, CAP_DEVICE_CAPS = 0x00000001, 0x80000000


def cameras():
    """[(device path, name)] of V4L2 nodes that can capture video."""
    found = []
    for path in sorted(glob.glob("/dev/video*"), key=lambda p: int(p[10:]) if p[10:].isdigit() else 1 << 30):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            buf = bytearray(104)
            fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)
        except OSError:
            continue
        finally:
            os.close(fd)
        card = buf[16:48].split(b"\0")[0].decode(errors="replace").strip().strip('"')
        caps, device_caps = struct.unpack_from("<II", buf, 84)
        if (device_caps if caps & CAP_DEVICE_CAPS else caps) & CAP_VIDEO_CAPTURE:
            found.append((path, card or path))
    return found
