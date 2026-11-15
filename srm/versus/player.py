"""Gap-free switching between versions held in memory.

``MixerCore`` is pure NumPy (testable without audio hardware): all versions are decoded to int16
stereo at one sample rate, one shared frame position advances, and switching the active version
changes only which buffer is read - so the listener stays at exactly the same moment of the song.
Loudness matching and the listening volume are gains applied to the samples being played; the
files and the in-memory buffers are never modified.
"""

from __future__ import annotations

import threading

import numpy as np

RATE = 48000
FADE_FRAMES = 480  # 10 ms crossfade on switch / seek: no click, no audible gap


class MixerCore:
    def __init__(self, rate: int = RATE):
        self.rate = rate
        self.tracks: dict[str, np.ndarray] = {}
        self.gains: dict[str, float] = {}  # linear, loudness matching (<= 1.0)
        self.active: str | None = None
        self.pos = 0  # frames; shared by all versions
        self.loop: tuple[int, int] | None = None
        self.volume = 1.0
        self.playing = False
        self._fade_from: str | None = None
        self._lock = threading.Lock()

    # ---- content
    def set_tracks(self, tracks: dict[str, np.ndarray], gains_db: dict[str, float] | None = None) -> None:
        with self._lock:
            self.tracks = dict(tracks)
            self.gains = {k: float(10 ** ((gains_db or {}).get(k, 0.0) / 20)) for k in tracks}
            self.active = next(iter(tracks), None)
            self.pos, self.loop, self.playing, self._fade_from = 0, None, False, None

    def length(self, label: str | None = None) -> int:
        label = label or self.active
        return int(self.tracks[label].shape[0]) if label in self.tracks else 0

    # ---- control
    def switch(self, label: str) -> None:
        """Change the audible version; the position is untouched."""
        with self._lock:
            if label not in self.tracks or label == self.active:
                return
            self._fade_from = self.active if self.playing else None
            self.active = label

    def seek(self, frame: int) -> None:
        with self._lock:
            self.pos = max(0, int(frame))
            self._fade_from = None

    def set_loop(self, start: int | None, end: int | None) -> None:
        with self._lock:
            self.loop = (int(start), int(end)) if start is not None and end is not None and end - start > 2400 else None

    # ---- audio
    def _read(self, label: str, pos: int, frames: int) -> np.ndarray:
        buf = self.tracks[label]
        chunk = buf[pos: pos + frames].astype(np.float32)
        if chunk.shape[0] < frames:
            chunk = np.vstack([chunk, np.zeros((frames - chunk.shape[0], 2), np.float32)])
        return chunk * self.gains.get(label, 1.0)

    def render(self, frames: int) -> np.ndarray:
        """Next ``frames`` of int16 stereo. Silence when paused; stops at the end of the active version."""
        with self._lock:
            out = np.zeros((frames, 2), np.float32)
            if not self.playing or self.active not in self.tracks or frames <= 0:
                return out.astype(np.int16)
            done = 0
            while done < frames:
                n = frames - done
                in_loop = bool(self.loop and self.loop[0] <= self.pos < self.loop[1])
                if in_loop:
                    n = min(n, self.loop[1] - self.pos)
                remaining = self.length() - self.pos
                if remaining <= 0:
                    break
                n = min(n, remaining)
                chunk = self._read(self.active, self.pos, n)
                if self._fade_from in self.tracks:
                    f = min(n, FADE_FRAMES)
                    ramp = np.linspace(0.0, 1.0, f, dtype=np.float32)[:, None]
                    chunk[:f] = chunk[:f] * ramp + self._read(self._fade_from, self.pos, f) * (1.0 - ramp)
                self._fade_from = None
                out[done: done + n] = chunk
                done += n
                self.pos += n
                if in_loop and self.pos >= self.loop[1]:
                    self.pos = self.loop[0]  # only a position that was inside the loop wraps around
            if self.pos >= self.length():
                self.playing = False  # end of the audible version: stop, the rest of the block is silence
            out *= self.volume
            return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def make_device(core: MixerCore, parent=None):
    """QIODevice that an audio sink pulls from. Imported lazily so the core stays Qt-free."""
    from PySide6.QtCore import QIODevice

    class MixerDevice(QIODevice):
        def __init__(self):
            super().__init__(parent)
            self.core = core

        def readData(self, maxlen):  # noqa: N802 - Qt naming
            frames = max(0, int(maxlen)) // 4
            return self.core.render(frames).tobytes() if frames else b""

        def writeData(self, data):  # noqa: N802
            return 0

        def bytesAvailable(self):  # noqa: N802
            return 1 << 20

        def isSequential(self):  # noqa: N802
            return True

    dev = MixerDevice()
    dev.open(QIODevice.ReadOnly)
    return dev


def start_sink(core: MixerCore, parent=None):
    """Open the default audio output and let it pull from ``core``. Returns (sink, device)."""
    from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

    dev = QMediaDevices.defaultAudioOutput()
    if dev.isNull():
        raise RuntimeError("устройство вывода звука не найдено")
    fmt = QAudioFormat()
    fmt.setSampleRate(core.rate)
    fmt.setChannelCount(2)
    fmt.setSampleFormat(QAudioFormat.Int16)
    sink = QAudioSink(dev, fmt, parent)
    sink.setBufferSize(core.rate * 4 // 12)  # ~80 ms: switching is heard almost at once
    device = make_device(core, parent)
    sink.start(device)
    return sink, device
