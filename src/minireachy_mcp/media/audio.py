"""Microphone audio buffer and a simple energy-based voice activity detector (16 kHz mono float32)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

RATE = 16_000
WINDOW = 480  # 30 ms


class AudioRing:
    """Fixed-size ring buffer addressed by an absolute, ever-increasing sample index."""

    def __init__(self, seconds: float = 60.0) -> None:
        self.capacity = int(seconds * RATE)
        self._buf = np.zeros(self.capacity, dtype=np.float32)
        self.total = 0  # absolute index of the next sample
        self.changed = asyncio.Event()

    def append(self, samples: np.ndarray) -> None:
        n = len(samples)
        if n == 0:
            return
        if n >= self.capacity:
            samples = samples[-self.capacity :]
            n = self.capacity
        start = self.total % self.capacity
        end = start + n
        if end <= self.capacity:
            self._buf[start:end] = samples
        else:
            first = self.capacity - start
            self._buf[start:] = samples[:first]
            self._buf[: n - first] = samples[first:]
        self.total += n
        self.changed.set()

    def read(self, start: int, end: int | None = None) -> np.ndarray:
        """Samples in [start, end) (absolute indices), clipped to what is still buffered."""
        end = self.total if end is None else min(end, self.total)
        start = max(start, self.total - self.capacity, 0)
        if end <= start:
            return np.zeros(0, dtype=np.float32)
        idx = np.arange(start, end) % self.capacity
        return self._buf[idx].copy()

    def rms_last(self, seconds: float) -> float:
        chunk = self.read(self.total - int(seconds * RATE))
        return float(np.sqrt(np.mean(chunk**2))) if len(chunk) else 0.0


@dataclass
class Utterance:
    audio: np.ndarray
    waited_s: float
    duration_s: float
    threshold: float
    noise_floor: float
    truncated: bool
    # Result of the transcription started speculatively at the first short pause, if it was
    # still valid when the end of the utterance was confirmed (the caller awaits it).
    speculative: asyncio.Task[Any] | None = None
    speculations_discarded: int = 0
    speech_ended_at: float = field(default_factory=time.monotonic)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x**2))) if len(x) else 0.0


async def capture_utterance(
    ring: AudioRing,
    *,
    max_wait_s: float,
    max_utterance_s: float,
    silence_ms: int = 800,
    min_speech_ms: int = 200,
    pre_roll_ms: int = 300,
    min_threshold: float = 0.008,
    sensitivity: float = 3.0,
    early_silence_ms: int | None = None,
    speculate: Callable[[np.ndarray], Awaitable[Any]] | None = None,
) -> Utterance | None:
    """Wait for speech that starts after this call, then record until `silence_ms` of silence.

    Returns None if no speech started within `max_wait_s`. Audio captured before the
    call (e.g. the robot's own voice) is ignored, except to estimate the noise floor.

    Speculative endpointing: after `early_silence_ms` of silence, `speculate(audio)` is
    started in the background (typically speech-to-text) while the detector keeps watching.
    If speech resumes, that result is discarded; if the silence lasts `silence_ms`, the
    task is handed back in `Utterance.speculative` and is usually already finished.
    """
    began = time.monotonic()
    cursor = ring.total
    floor_audio = ring.read(cursor - RATE)  # last second, for the noise floor
    windows = [_rms(floor_audio[i : i + WINDOW]) for i in range(0, len(floor_audio) - WINDOW + 1, WINDOW)]
    noise_floor = float(np.percentile(windows, 30)) if windows else 0.0
    threshold = max(min_threshold, noise_floor * sensitivity)

    need_speech = max(1, min_speech_ms // 30)
    need_silence = max(1, silence_ms // 30)
    need_early = max(1, early_silence_ms // 30) if early_silence_ms and speculate else None
    speech_run = 0
    silence_run = 0
    speech_start: int | None = None
    spec: asyncio.Task[Any] | None = None
    discarded = 0

    while True:
        while ring.total - cursor >= WINDOW:
            level = _rms(ring.read(cursor, cursor + WINDOW))
            if speech_start is None:
                speech_run = speech_run + 1 if level > threshold else 0
                if speech_run >= need_speech:
                    speech_start = cursor - (need_speech - 1) * WINDOW
            else:
                if level <= threshold:
                    silence_run += 1
                else:
                    silence_run = 0
                    if spec is not None:  # the speaker went on: the speculative result is stale
                        spec.cancel()
                        spec = None
                        discarded += 1
                if need_early is not None and spec is None and silence_run == need_early:
                    audio = ring.read(speech_start - pre_roll_ms * RATE // 1000, cursor + WINDOW)
                    spec = asyncio.ensure_future(speculate(audio))  # type: ignore[misc]
                if silence_run >= need_silence:
                    end = cursor + WINDOW
                    utt = _finish(ring, speech_start, end, pre_roll_ms, began, threshold, noise_floor, False)
                    utt.speculative, utt.speculations_discarded = spec, discarded
                    utt.speech_ended_at = time.monotonic() - (need_silence * WINDOW) / RATE
                    return utt
                if (cursor - speech_start) / RATE >= max_utterance_s:
                    if spec is not None:
                        spec.cancel()
                    return _finish(ring, speech_start, cursor + WINDOW, pre_roll_ms, began, threshold, noise_floor, True)
            cursor += WINDOW
        if speech_start is None and time.monotonic() - began > max_wait_s:
            return None
        ring.changed.clear()
        try:
            await asyncio.wait_for(ring.changed.wait(), 1.0)
        except TimeoutError:
            if time.monotonic() - began > max_wait_s + max_utterance_s + 5:
                return None  # audio stopped flowing


def _finish(
    ring: AudioRing, start: int, end: int, pre_roll_ms: int, began: float, threshold: float, floor: float, truncated: bool
) -> Utterance:
    audio = ring.read(start - pre_roll_ms * RATE // 1000, end)
    return Utterance(
        audio=audio,
        waited_s=round(time.monotonic() - began, 2),
        duration_s=round(len(audio) / RATE, 2),
        threshold=threshold,
        noise_floor=floor,
        truncated=truncated,
    )
