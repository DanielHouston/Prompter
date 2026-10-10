"""Speech recognition worker, run by the app as `python -m prompter.asr_worker`.

Captures the mic with pw-record and, while there is speech, re-transcribes the last few
seconds with Whisper several times a second: on an NVIDIA GPU if there is one, otherwise on
the CPU. Only loads a model that's already on this computer: it never downloads one, and
says "missing" instead (the app offers the download). Talks JSON lines:
  stdout: {"type": "ready"|"missing"|"level"|"hyp"|"error", ...}
  stdin:  {"cmd": "listen"|"pause"|"prompt"|"quit", ...}
"""

import argparse
import json
import subprocess
import sys
import threading
import time
import wave

import numpy as np

from .models import resolve

RATE = 16000
BLOCK = RATE // 10  # 100 ms, the unit for levels and the speech gate


def emit(**msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


class Ring:
    """The last few seconds of audio."""

    def __init__(self, seconds):
        self.buf = np.zeros(int(seconds * RATE), np.float32)
        self.filled = 0
        self.total = 0  # samples ever pushed; the "at" clock when not recording
        self.lock = threading.Lock()

    def push(self, samples):
        n = min(len(samples), len(self.buf))
        if n == 0:
            return
        with self.lock:
            self.buf[:-n] = self.buf[n:]
            self.buf[-n:] = samples[-n:]
            self.filled = min(len(self.buf), self.filled + n)
            self.total += len(samples)

    def last(self, seconds):
        with self.lock:
            n = min(int(seconds * RATE), self.filled)
            return self.buf[len(self.buf) - n :].copy()


def capture_mic(ring, args, stop, recorder):
    cmd = ["pw-record", "--raw", "--rate", str(RATE), "--channels", "1", "--format", "f32", "--latency", "20ms"]
    if args.mic:
        cmd += ["--target", args.mic]
    proc = subprocess.Popen(cmd + ["-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while not stop.is_set():
            data = proc.stdout.read(BLOCK // 2 * 4)  # 50 ms
            if not data:
                emit(type="error", msg="microphone capture stopped")
                break
            samples = np.frombuffer(data[: len(data) // 4 * 4], np.float32)
            ring.push(samples)
            recorder.write(samples)
    finally:
        proc.terminate()


def play_file(ring, args, stop, recorder):
    """Feed a 16 kHz mono WAV in real time, for testing without a microphone."""
    with wave.open(args.file) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    chunk, start = BLOCK // 2, time.monotonic()
    for i in range(0, len(pcm), chunk):
        if stop.is_set():
            return
        ring.push(pcm[i : i + chunk])
        recorder.write(pcm[i : i + chunk])
        time.sleep(max(0.0, start + (i + chunk) / RATE - time.monotonic()))
    ring.push(np.zeros(RATE * 3, np.float32))
    emit(type="eof")


class Recorder:
    """Diagnostic recording of the mic, only while listening (not the whole time the app is
    open). Its length is the clock that "at" timestamps refer to, so audio and events line up.
    wave patches the header on every write, so the file stays valid if the worker is killed."""

    def __init__(self, path):
        self.wav = None
        self.listening = False
        self.samples = 0
        if path:
            self.wav = wave.open(path, "wb")
            self.wav.setnchannels(1)
            self.wav.setsampwidth(2)
            self.wav.setframerate(RATE)

    def write(self, samples):
        if self.wav and self.listening:
            self.wav.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
            self.samples += len(samples)


def load_model(choice, language, prefer):
    """(model, device, model name), loaded from this computer: on the GPU when it works and
    prefer allows it ("auto", "cuda" or "cpu"), else the CPU. None, after saying "missing",
    when the model isn't downloaded."""
    import ctranslate2

    gpu = prefer != "cpu" and ctranslate2.get_cuda_device_count() > 0
    if prefer == "cuda" and not gpu:
        emit(type="error", msg="No NVIDIA graphics card found: using the processor")
    for device in (["cuda"] if gpu else []) + ["cpu"]:
        name, path = resolve(choice, language, device)
        if path is None:
            emit(type="missing", model=name, device=device)
            return None
        from faster_whisper import WhisperModel

        compute = "float16" if device == "cuda" else "int8"
        try:
            return WhisperModel(str(path), device=device, compute_type=compute), device, name
        except Exception as e:  # e.g. CUDA present but its libraries aren't: fall back to the CPU
            if device == "cpu":
                raise
            emit(type="error", msg=f"GPU unavailable ({e}); using the CPU")
    raise RuntimeError("no device to run the speech model on")


def block_rms(audio):
    n = len(audio) // BLOCK * BLOCK
    if n == 0:
        return np.zeros(0)
    return np.sqrt(np.mean(audio[:n].reshape(-1, BLOCK) ** 2, axis=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="auto", help='"auto", a model name (prompter.models) or a model folder')
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"), help="where to run it")
    ap.add_argument("--language", default="en", help="ISO 639-1 code of the language being read")
    ap.add_argument("--mic", help="PipeWire node to record from (default source if omitted)")
    ap.add_argument("--file", help="16 kHz mono WAV to play instead of the mic")
    ap.add_argument("--window", type=float, default=5.0, help="seconds transcribed per tick")
    ap.add_argument("--interval", type=float, default=0.3, help="seconds between ticks")
    ap.add_argument("--listen", action="store_true", help="start listening immediately")
    ap.add_argument("--record", help="also save the mic audio to this WAV (diagnostics)")
    ap.add_argument("--debug", action="store_true", help="report ticks skipped by the speech gate")
    args = ap.parse_args()

    loaded = load_model(args.model, args.language, args.device)
    if loaded is None:
        return  # not downloaded: the app offers it
    model, device, name = loaded
    model.transcribe(np.zeros(RATE, np.float32), language=args.language, beam_size=1)

    ring, stop, recorder = Ring(12), threading.Event(), Recorder(args.record)
    recorder.listening = args.listen
    source = play_file if args.file else capture_mic
    threading.Thread(target=source, args=(ring, args, stop, recorder), daemon=True).start()

    state = {"listen": args.listen, "prompt": ""}

    def commands():
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            cmd = msg.get("cmd")
            if cmd == "listen":
                state["listen"] = recorder.listening = True
            elif cmd == "pause":
                state["listen"] = recorder.listening = False
            elif cmd == "prompt":
                state["prompt"] = msg.get("text", "")
            elif cmd == "quit":
                break
        stop.set()

    threading.Thread(target=commands, daemon=True).start()
    emit(type="ready", model=name, device=device)

    last_level = last_tick = 0.0
    while not stop.is_set():
        time.sleep(0.02)
        now = time.monotonic()

        if now - last_level >= 0.1:
            last_level = now
            recent = ring.last(0.1)
            emit(type="level", rms=round(float(np.sqrt(np.mean(recent**2))) if len(recent) else 0.0, 5))

        if not state["listen"] or now - last_tick < args.interval:
            continue
        last_tick = now

        audio = ring.last(args.window)
        at = round((recorder.samples if recorder.wav else ring.total) / RATE, 2)
        levels = block_rms(ring.last(10))
        if len(audio) < RATE // 2 or len(levels) < 6:
            continue
        # Speech gate: something well above the room's noise floor in the last 600 ms.
        # Skipping silence saves the GPU and stops Whisper inventing "Thank you."
        floor = float(np.percentile(levels, 10))
        threshold = max(0.006, 3 * floor)
        if levels[-6:].max() < threshold:
            if args.debug:
                emit(type="gate", at=at, peak=round(float(levels[-6:].max()), 5), threshold=round(threshold, 5))
            continue

        t0 = time.monotonic()
        segments, _ = model.transcribe(
            audio,
            language=args.language,
            beam_size=1,
            temperature=0.0,
            condition_on_previous_text=False,
            without_timestamps=True,
            initial_prompt=state["prompt"] or None,
        )
        text = " ".join(s.text.strip() for s in segments)
        emit(type="hyp", text=text, at=at, ms=round((time.monotonic() - t0) * 1000))


if __name__ == "__main__":
    main()
