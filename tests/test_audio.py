"""Ring buffer and voice-activity endpointing on synthetic signals."""

import asyncio

import numpy as np
import pytest

from minireachy_mcp.media.audio import RATE, AudioRing, capture_utterance

pytestmark = pytest.mark.anyio


def tone(seconds: float, amp: float = 0.2, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def noise(seconds: float, amp: float = 0.002) -> np.ndarray:
    return (np.random.default_rng(0).standard_normal(int(seconds * RATE)) * amp).astype(np.float32)


def test_ring_wraps_and_reads_by_absolute_index() -> None:
    ring = AudioRing(seconds=1.0)
    data = np.arange(int(1.5 * RATE), dtype=np.float32)
    ring.append(data[:10_000])
    ring.append(data[10_000:])
    assert ring.total == len(data)
    tail = ring.read(ring.total - 100)
    assert np.array_equal(tail, data[-100:])
    # older than capacity is clipped
    assert len(ring.read(0)) == ring.capacity


async def feed(ring: AudioRing, chunks: list[np.ndarray], step: float = 0.02) -> None:
    for chunk in chunks:
        for i in range(0, len(chunk), 320):  # 20 ms packets, like WebRTC
            ring.append(chunk[i : i + 320])
            await asyncio.sleep(step / 20)


async def test_captures_one_utterance_and_stops_on_silence() -> None:
    ring = AudioRing()
    ring.append(noise(1.0))  # history: noise floor
    task = asyncio.create_task(capture_utterance(ring, max_wait_s=5, max_utterance_s=10, silence_ms=600))
    await asyncio.sleep(0.01)
    await feed(ring, [noise(0.5), tone(1.2), noise(1.5), tone(1.0)])
    utt = await task
    assert utt is not None and not utt.truncated
    # 1.2 s of speech + 0.3 s pre-roll + ~0.6 s trailing silence; the 2nd tone is not included
    assert 1.6 <= utt.duration_s <= 2.4
    assert utt.threshold > utt.noise_floor


async def test_audio_before_the_call_is_ignored() -> None:
    ring = AudioRing()
    ring.append(tone(2.0))  # e.g. the robot's own voice, already played
    utt = await asyncio.wait_for(
        asyncio.gather(capture_utterance(ring, max_wait_s=0.5, max_utterance_s=5), feed(ring, [noise(1.0)])), 5
    )
    assert utt[0] is None


async def test_no_speech_times_out() -> None:
    ring = AudioRing()
    ring.append(noise(1.0))
    result = await asyncio.gather(capture_utterance(ring, max_wait_s=0.5, max_utterance_s=5), feed(ring, [noise(1.0)]))
    assert result[0] is None


async def test_long_speech_is_truncated() -> None:
    ring = AudioRing()
    ring.append(noise(1.0))
    task = asyncio.create_task(capture_utterance(ring, max_wait_s=5, max_utterance_s=1.0))
    await asyncio.sleep(0.01)
    await feed(ring, [tone(3.0)])
    utt = await task
    assert utt is not None and utt.truncated and utt.duration_s <= 1.5


async def test_speculative_transcription_is_reused() -> None:
    ring = AudioRing()
    ring.append(noise(1.0))
    calls: list[int] = []

    async def speculate(audio: np.ndarray) -> str:
        calls.append(len(audio))
        return "résultat"

    task = asyncio.create_task(capture_utterance(ring, max_wait_s=5, max_utterance_s=10, silence_ms=700,
                                                 early_silence_ms=300, speculate=speculate))
    await asyncio.sleep(0.01)
    await feed(ring, [noise(0.3), tone(1.0), noise(1.2)])
    utt = await task
    assert utt is not None and utt.speculative is not None
    assert await utt.speculative == "résultat"
    assert len(calls) == 1 and utt.speculations_discarded == 0


async def test_speculation_discarded_when_speech_resumes() -> None:
    ring = AudioRing()
    ring.append(noise(1.0))
    started: list[float] = []

    async def speculate(audio: np.ndarray) -> float:
        started.append(len(audio) / RATE)
        await asyncio.sleep(0.05)
        return len(audio) / RATE

    task = asyncio.create_task(capture_utterance(ring, max_wait_s=5, max_utterance_s=10, silence_ms=700,
                                                 early_silence_ms=300, speculate=speculate))
    await asyncio.sleep(0.01)
    # short pause (450 ms) between two words: speculation starts, then must be discarded
    await feed(ring, [noise(0.3), tone(0.8), noise(0.45), tone(0.8), noise(1.2)])
    utt = await task
    assert utt is not None and utt.speculations_discarded == 1 and len(started) == 2
    # the kept speculation covers both words
    assert await utt.speculative > 1.8
