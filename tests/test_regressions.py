"""Regression tests for the bugs found in the 2026-10-16 code review (one per bug)."""

import asyncio
import threading
import wave

import numpy as np
import pytest

from minireachy_mcp.media.speech import TextToSpeech

from .conftest import text_of
from .fake_daemon import Override
from .test_perception_tools import StubMedia, StubTTS

pytestmark = pytest.mark.anyio


def gotos(fake) -> list:
    return [c for c in fake.calls("POST") if c.path == "/api/move/goto"]


# -- 1. stop_sound stops a speech still being synthesised -----------------------------------


class _SlowTTS(StubTTS):
    """First sentence at once, the second only once `release` is set (synthesis still running)."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def stream(self, text, length_scale=None, *, speed=1.0):
        yield np.full(11025, 1000, dtype=np.int16), 22050
        self.release.wait(5)
        yield np.full(11025, 1000, dtype=np.int16), 22050


async def test_stop_sound_drops_sentences_synthesised_afterwards(fake, mcp_session) -> None:
    media = StubMedia()
    media.tts = _SlowTTS()
    speaker = media.link.speaker
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        speech = asyncio.create_task(client.call_tool("speak", {"text": "Une. Deux.", "wait": False}))
        while speaker.samples == 0:
            await asyncio.sleep(0.01)
        first_sentence = speaker.samples
        await client.call_tool("stop_sound", {})
        media.tts.release.set()
        r = await speech
    assert not r.is_error, text_of(r)
    assert speaker.samples == first_sentence  # the second sentence never reached the speaker
    assert r.structured_content["playback"].startswith("stopped")


# -- 2. TTS_LENGTH_SCALE applies to speak/converse, combined with `speed` ---------------------


class _Chunk:
    audio_int16_array = np.zeros(10, dtype=np.int16)
    sample_rate = 22050


class _FakeVoice:
    def __init__(self) -> None:
        self.length_scales: list[float] = []

    def synthesize(self, text, syn_config):
        self.length_scales.append(syn_config.length_scale)
        yield _Chunk()

    def synthesize_wav(self, text, wf: wave.Wave_write, syn_config) -> None:
        self.length_scales.append(syn_config.length_scale)
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(22050)
        wf.writeframes(b"\x00\x00" * 10)


async def test_configured_length_scale_is_combined_with_speed(fake, mcp_session) -> None:
    tts = TextToSpeech("/unused.onnx", length_scale=1.5)
    tts._voice = voice = _FakeVoice()
    media = StubMedia()
    media.tts = tts
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        await client.call_tool("speak", {"text": "Bonjour"})  # speed 1.0: the configured value, not 1.0
        await client.call_tool("speak", {"text": "Bonjour", "speed": 2.0})
    async with mcp_session(media=media, enable_physical_actions=True, speech_output="daemon") as (client, _):
        await client.call_tool("speak", {"text": "Bonjour", "speed": 0.5})
    assert voice.length_scales == [1.5, 0.75, 3.0]


# -- 3. converse: a failing voice cancels the gesture played with it --------------------------


async def test_converse_failure_leaves_no_gesture_running(fake, mcp_session) -> None:
    media = StubMedia()
    media.link.speaker = None
    media.link.fail = True  # forced WebRTC output: the voice fails at once
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True,
                           speech_output="webrtc") as (client, _):
        r = await client.call_tool("converse", {"say_text": "Bonjour", "gesture_name": "salut",
                                                "listen_after": False, "look": "never"})
        sent = len(gotos(fake))
        await asyncio.sleep(1.2)  # "salut" lasts ~1.45 s: an orphan gesture would keep sending
    assert r.is_error and "media_unavailable" in text_of(r)
    assert len(gotos(fake)) == sent


# -- 4. stop_all_moves does not wait for a goto still in flight -------------------------------


async def test_stop_all_moves_is_not_delayed_by_a_goto_in_flight(fake, mcp_session) -> None:
    fake.running_moves.append("11111111-1111-1111-1111-111111111111")
    fake.overrides["POST /api/move/goto"] = Override(body={"uuid": "22222222-2222-2222-2222-222222222222"},
                                                     delay=0.8)
    async with mcp_session(enable_physical_actions=True) as (client, _):
        move = asyncio.create_task(client.call_tool("goto_pose", {"duration": 1.0, "antennas": [0.1, 0.1]}))
        while not gotos(fake):
            await asyncio.sleep(0.01)
        stop = await client.call_tool("stop_all_moves", {})
        await move
    goto_at = gotos(fake)[0].at
    first_stop = next(c for c in fake.calls("POST") if c.path == "/api/move/stop")
    assert first_stop.at - goto_at < 0.5  # before the daemon answered the goto (0.8 s)
    assert not stop.is_error and stop.structured_content["still_running"] == []


# -- 5. the head speed guard rail still applies when the head pose cannot be read -------------


async def test_head_speed_is_checked_from_the_worst_case_when_pose_unknown(fake, mcp_session) -> None:
    fake.overrides["GET /api/state/present_head_pose"] = Override(status=500, body={"detail": "boom"})
    async with mcp_session(enable_physical_actions=True) as (client, _):
        fast = await client.call_tool("goto_pose", {"duration": 0.5, "head_pose": {"yaw": 1.0}})
        slow = await client.call_tool("goto_pose", {"duration": 1.2, "head_pose": {"yaw": 1.0}})
    # From the far end of the yaw guard rail (-1.2 rad): 2.2 rad needs >= 1.1 s at 2 rad/s.
    assert fast.is_error and "refused_by_safety_policy" in text_of(fast)
    assert not slow.is_error, text_of(slow)
    assert len(gotos(fake)) == 1
