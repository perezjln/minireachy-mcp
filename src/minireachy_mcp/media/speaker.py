"""Outgoing WebRTC audio track: the robot's voice, sent to the daemon's speaker.

The daemon's media server makes its audio transceiver bidirectional and plays whatever
a consumer sends through a persistent pipeline (Opus decode -> speaker, plus the
head-wobbler analysis). Compared with upload + /api/media/play_sound, this avoids the
~1 s pipeline rebuild the daemon performs for every sound file, lets playback start
while later sentences are still being synthesised, and tells us exactly when the last
sample has been sent.

The track always produces 20 ms frames in real time: queued speech, otherwise silence.
"""

from __future__ import annotations

import asyncio
import collections
import fractions
import time
from typing import Any

import numpy as np

RATE = 48_000
FRAME = 960  # 20 ms
_TIME_BASE = fractions.Fraction(1, RATE)


def resample_int16(pcm: np.ndarray, rate: int) -> np.ndarray:
    """Mono int16 at `rate` -> mono int16 at 48 kHz (PyAV / libswresample)."""
    if rate == RATE:
        return pcm.astype(np.int16, copy=False)
    import av

    frame = av.AudioFrame.from_ndarray(pcm.astype(np.int16).reshape(1, -1), format="s16", layout="mono")
    frame.sample_rate = rate
    resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    out = [f.to_ndarray().reshape(-1) for f in resampler.resample(frame) + resampler.resample(None)]
    return np.concatenate(out).astype(np.int16) if out else np.zeros(0, dtype=np.int16)


def create_speaker_track() -> Any:
    """Build the track (aiortc is imported lazily, like everywhere in the media package)."""
    from aiortc import MediaStreamTrack

    class _Speaker(MediaStreamTrack):
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self._queue: collections.deque[np.ndarray] = collections.deque()
            self._queued = 0  # samples waiting
            self._pts = 0
            self._start: float | None = None
            self._drained = asyncio.Event()
            self._drained.set()
            self.frames_sent = 0
            self.speech_frames_sent = 0
            self.last_frame_at: float | None = None

        # -- producer side ------------------------------------------------------------

        def enqueue(self, pcm48: np.ndarray) -> None:
            if len(pcm48):
                self._queue.append(pcm48.astype(np.int16, copy=False))
                self._queued += len(pcm48)
                self._drained.clear()

        def clear(self) -> None:
            self._queue.clear()
            self._queued = 0
            self._drained.set()

        @property
        def queued_seconds(self) -> float:
            return self._queued / RATE

        @property
        def busy(self) -> bool:
            return not self._drained.is_set()

        async def drained(self) -> None:
            await self._drained.wait()

        # -- aiortc side --------------------------------------------------------------

        def _next_samples(self) -> tuple[np.ndarray, bool]:
            out = np.zeros(FRAME, dtype=np.int16)
            filled = 0
            while filled < FRAME and self._queue:
                head = self._queue[0]
                take = min(FRAME - filled, len(head))
                out[filled : filled + take] = head[:take]
                filled += take
                if take == len(head):
                    self._queue.popleft()
                else:
                    self._queue[0] = head[take:]
            self._queued -= filled
            if filled and not self._queue:
                self._drained.set()
            return out, filled > 0

        async def recv(self) -> Any:
            import av

            if self._start is None:
                self._start = time.monotonic()
            target = self._start + self._pts / RATE
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.5:  # we fell behind (e.g. CPU hiccup): resynchronise the clock
                self._start = time.monotonic() - self._pts / RATE
            samples, speech = self._next_samples()
            frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = RATE
            frame.pts = self._pts
            frame.time_base = _TIME_BASE
            self._pts += FRAME
            self.frames_sent += 1
            self.speech_frames_sent += int(speech)
            self.last_frame_at = time.monotonic()
            return frame

    return _Speaker()
