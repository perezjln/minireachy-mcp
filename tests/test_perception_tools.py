"""Perception and voice MCP tools with stubbed media services and the simulated daemon."""

import asyncio
import base64
import io
import json
import math
import time
import wave

import av
import numpy as np
import pytest
from PIL import Image as PILImage

from minireachy_mcp.media.audio import RATE, AudioRing
from minireachy_mcp.media.speech import Speech, Transcript

from .conftest import text_of
from .fake_daemon import Override

pytestmark = pytest.mark.anyio


class StubSpeaker:
    def __init__(self) -> None:
        self.samples = 0
        self.cleared = 0
        self._busy = False

    def enqueue(self, pcm48: np.ndarray) -> None:
        self.samples += len(pcm48)
        self._busy = True

    def clear(self) -> None:
        self.cleared += 1
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    async def drained(self) -> None:
        await asyncio.sleep(self.samples / 48_000 * 0.01)  # 100x faster than real time
        self._busy = False


class StubLink:
    def __init__(self) -> None:
        self.speaker: StubSpeaker | None = StubSpeaker()
        self.fail = False
        self.audio = AudioRing()
        self.audio.append(np.zeros(RATE, dtype=np.float32))
        img = np.zeros((1080, 1920, 3), dtype=np.uint8)
        img[:, :960] = (0, 0, 255)
        self.frame = av.VideoFrame.from_ndarray(img, format="rgb24")
        self.connected = 0
        self.speech_after_s: float | None = None

    async def ensure_connected(self, timeout_s: float | None = None) -> None:
        if self.fail:
            from minireachy_mcp.media.webrtc import MediaUnavailableError

            raise MediaUnavailableError("stub link down")
        self.connected += 1
        if self.speech_after_s is not None:
            asyncio.get_running_loop().call_later(self.speech_after_s, self._speak)

    def _speak(self) -> None:
        t = np.arange(int(1.0 * RATE)) / RATE
        self.audio.append((0.2 * np.sin(2 * np.pi * 200 * t)).astype(np.float32))
        self.audio.append(np.zeros(int(1.5 * RATE), dtype=np.float32))

    async def latest_frame(self, max_age_s: float = 2.0) -> tuple[av.VideoFrame, float]:
        await self.ensure_connected()
        return self.frame, 0.04

    def status(self) -> dict:
        return {"state": "stub"}

    async def close(self) -> None:
        pass


class StubSTT:
    loaded = True

    def warmup(self) -> float:
        return 0.01

    def __init__(self) -> None:
        self.calls: list[np.ndarray] = []

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> Transcript:
        self.calls.append(audio)
        return Transcript(text="Bonjour Reachy", language=language or "fr", language_probability=0.99,
                          no_speech_probability=0.01, seconds=0.1)


class StubTTS:
    loaded = True

    def stream(self, text: str, length_scale: float | None = None):
        for _ in range(2):  # two "sentences" of 0.5 s at 22.05 kHz
            yield np.full(11025, 1000, dtype=np.int16), 22050

    def warmup(self) -> float:
        return 0.01

    def synthesize(self, text: str, length_scale: float | None = None) -> Speech:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(b"\x00\x00" * 2205)
        return Speech(wav=buf.getvalue(), duration_s=0.1, seconds=0.01)


class StubMedia:
    def __init__(self) -> None:
        self.link = StubLink()
        self.stt = StubSTT()
        self.tts = StubTTS()

    async def aclose(self) -> None:
        pass


@pytest.fixture
def media() -> StubMedia:
    return StubMedia()


async def test_perception_refused_by_default_and_nothing_captured(fake, mcp_session, media) -> None:
    async with mcp_session(media=media) as (client, _):
        for tool in ("capture_image", "listen", "converse"):
            r = await client.call_tool(tool, {})
            assert r.is_error and "ENABLE_PERCEPTION" in text_of(r)
    assert media.link.connected == 0 and fake.requests == []


async def test_capture_image_returns_jpeg(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_perception=True) as (client, _):
        r = await client.call_tool("capture_image", {"max_width": 640})
    assert not r.is_error, text_of(r)
    image = next(c for c in r.content if c.type == "image")
    assert image.mime_type == "image/jpeg"
    pil = PILImage.open(io.BytesIO(base64.b64decode(image.data)))
    assert pil.size == (640, 360)
    meta = json.loads(next(c.text for c in r.content if c.type == "text"))
    assert meta["source_resolution"] == [1920, 1080]


async def test_listen_transcribes_one_utterance(fake, mcp_session, media) -> None:
    media.link.speech_after_s = 0.3
    async with mcp_session(media=media, enable_perception=True) as (client, _):
        started = time.monotonic()
        r = await client.call_tool("listen", {"max_wait_s": 5, "silence_ms": 600})
    assert not r.is_error, text_of(r)
    out = r.structured_content
    assert out["heard"] is True and out["text"] == "Bonjour Reachy"
    assert out["direction_of_arrival"]["speech_detected"] is False  # from the simulated daemon
    assert 1.0 <= out["duration_s"] <= 2.2 and time.monotonic() - started < 5
    assert len(media.stt.calls) == 1
    assert fake.non_get_calls() == []  # listening sends no command


async def test_listen_nobody_speaks(fake, mcp_session, media) -> None:
    async def trickle() -> None:  # keep audio flowing, silence only
        for _ in range(60):
            media.link.audio.append(np.zeros(320, dtype=np.float32))
            await asyncio.sleep(0.02)

    async with mcp_session(media=media, enable_perception=True) as (client, _):
        task = asyncio.create_task(trickle())
        r = await client.call_tool("listen", {"max_wait_s": 1})
        task.cancel()
    assert r.structured_content["heard"] is False and media.stt.calls == []


async def test_speak_uploads_plays_and_cleans_previous(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True) as (client, _):
        r1 = await client.call_tool("speak", {"text": "Bonjour !"})
        r2 = await client.call_tool("speak", {"text": "Comment ça va ?", "wait": False})
    assert not r1.is_error, text_of(r1)
    assert r1.structured_content["said"] == "Bonjour !" and r2.structured_content["playback"] == "started"
    calls = [(r.method, r.path) for r in fake.non_get_calls()]
    assert calls[0] == ("POST", "/api/media/sounds/upload")
    assert calls[1] == ("POST", "/api/media/play_sound")
    played = fake.calls("POST")[1].body["file"]
    assert played.startswith("mcp_tts_") and played.endswith(".wav")
    # second utterance deletes the first temp file
    assert ("DELETE", f"/api/media/sounds/{played}") in calls


async def test_speak_refused_without_physical(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_perception=True) as (client, _):
        r = await client.call_tool("speak", {"text": "Bonjour"})
    assert r.is_error and fake.requests == []


@pytest.mark.parametrize(
    "angle,expected_yaw",
    [(math.pi / 2, 0.0), (math.pi / 2 + 0.5, -0.5), (0.0, 1.2), (math.pi, -1.2)],
)
async def test_turn_toward_sound(fake, mcp_session, media, angle, expected_yaw) -> None:
    fake.overrides["GET /api/state/doa"] = Override(body={"angle": angle, "speech_detected": True})
    async with mcp_session(media=media, enable_physical_actions=True) as (client, _):
        r = await client.call_tool("turn_toward_sound", {})
    assert r.structured_content["turned"] is True
    goto = fake.calls("POST")[0]
    assert goto.path == "/api/move/goto"
    assert goto.body["head_pose"]["yaw"] == pytest.approx(expected_yaw, abs=1e-3)


async def test_perception_status_does_not_connect(fake, mcp_session, media) -> None:
    async with mcp_session(media=media) as (client, _):
        r = await client.call_tool("get_perception_status", {})
    assert r.structured_content["perception_enabled"] is False and media.link.connected == 0


async def test_listen_ignores_non_speech_and_keeps_listening(fake, mcp_session, media) -> None:
    """A first sound rejected by STT (noise / own voice) must not end the listening window."""
    replies = iter([
        Transcript(text="", language="fr", language_probability=0.5, no_speech_probability=0.9, seconds=0.1,
                   rejected_reason="empty"),
        Transcript(text="Salut", language="fr", language_probability=0.99, no_speech_probability=0.01, seconds=0.1),
    ])
    media.stt.transcribe = lambda audio, language=None: next(replies)  # type: ignore[method-assign]

    async def two_sounds() -> None:
        media.link._speak()
        await asyncio.sleep(1.2)
        media.link._speak()

    async def trickle() -> None:  # keep audio flowing so the detector polls
        for _ in range(400):
            media.link.audio.append(np.zeros(160, dtype=np.float32))
            await asyncio.sleep(0.01)

    async with mcp_session(media=media, enable_perception=True) as (client, _):
        tasks = [asyncio.create_task(two_sounds()), asyncio.create_task(trickle())]
        r = await client.call_tool("listen", {"max_wait_s": 8, "silence_ms": 600})
        for t in tasks:
            t.cancel()
    out = r.structured_content
    assert out["heard"] is True and out["text"] == "Salut"
    assert len(out["ignored_sounds"]) == 1 and "empty" in out["ignored_sounds"][0]


def json_part(result) -> dict:
    return json.loads(next(c.text for c in result.content if c.type == "text"))


async def test_converse_speaks_then_listens_in_one_call(fake, mcp_session, media) -> None:
    media.link.speech_after_s = 0.4  # the person answers after Reachy's sentence
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        r = await client.call_tool("converse", {"say_text": "Comment vas-tu ?", "max_wait_s": 5})
    assert not r.is_error, text_of(r)
    out = json_part(r)
    assert out["said"] == "Comment vas-tu ?" and out["heard"] is True and out["text"] == "Bonjour Reachy"
    assert out["answer"]["timings_s"]["end_of_speech_to_text"] < 2
    assert out["output"] == "webrtc" and fake.calls("POST") == []  # voice streamed, no upload/play_sound
    assert media.link.speaker.samples == pytest.approx(48_000, rel=0.01)  # 2 x 0.5 s resampled to 48 kHz
    assert not any(c.type == "image" for c in r.content)


async def test_converse_gesture_runs_with_speech_and_look_returns_image(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        started = time.monotonic()
        r = await client.call_tool("converse", {"say_text": "Salut !", "gesture_name": "salut",
                                                "listen_after": False, "look": True})
        elapsed = time.monotonic() - started
    assert not r.is_error, text_of(r)
    out = json_part(r)
    gotos = [c for c in fake.calls("POST") if c.path == "/api/move/goto"]
    assert len(gotos) == out["gesture"]["steps_sent"] == 4
    assert all(set(g.body) == {"antennas", "duration", "interpolation"} for g in gotos)  # antennas only
    assert gotos[-1].body["antennas"] == [0.0, 0.0]
    assert any(c.type == "image" for c in r.content) and out["image"]["image_size"] == [1024, 576]
    # speech (~0.7 s with margin) and gesture (~1.5 s) overlapped instead of adding up
    assert elapsed < 3.0


async def test_converse_speech_refused_without_physical(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_perception=True) as (client, _):
        refused = await client.call_tool("converse", {"say_text": "Bonjour"})
        media.link.speech_after_s = 0.2
        listen_only = await client.call_tool("converse", {"max_wait_s": 3})
    assert refused.is_error and "ENABLE_PHYSICAL_ACTIONS" in text_of(refused)
    assert not listen_only.is_error and json_part(listen_only)["heard"] is True
    assert fake.non_get_calls() == []


async def test_warmup_loads_models_and_opens_stream_without_robot_action(fake) -> None:
    from minireachy_mcp.media.services import MediaServices

    stub = StubMedia()
    services = MediaServices(link=stub.link, stt=stub.stt, tts=stub.tts)
    services.start_warmup()
    await services._warmup_task
    assert services.warmup_report["state"] == "done"
    assert services.warmup_report["speech_to_text_s"] == 0.01 and "media_link_s" in services.warmup_report
    assert stub.link.connected == 1 and fake.requests == []
    await services.aclose()


async def test_speak_streams_on_webrtc_when_perception_enabled(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        r = await client.call_tool("speak", {"text": "Bonjour. Comment vas-tu ?"})
    out = r.structured_content
    assert out["output"] == "webrtc" and out["duration_s"] == pytest.approx(1.0, abs=0.02)
    assert out["playback"].startswith("finished") and out["timings_s"]["first_audio"] < 0.5
    assert fake.non_get_calls() == []


async def test_speak_falls_back_to_daemon_when_stream_unavailable(fake, mcp_session, media) -> None:
    media.link.speaker = None
    media.link.fail = True
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        r = await client.call_tool("speak", {"text": "Bonjour"})
    assert r.structured_content["output"] == "daemon_play_sound"
    assert [c.path for c in fake.calls("POST")] == ["/api/media/sounds/upload", "/api/media/play_sound"]


async def test_speech_output_daemon_forced(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True,
                           speech_output="daemon") as (client, _):
        r = await client.call_tool("speak", {"text": "Bonjour"})
    assert r.structured_content["output"] == "daemon_play_sound" and media.link.speaker.samples == 0


async def test_speech_output_webrtc_forced_fails_clearly(fake, mcp_session, media) -> None:
    media.link.speaker = None
    media.link.fail = True
    async with mcp_session(media=media, enable_physical_actions=True, speech_output="webrtc") as (client, _):
        r = await client.call_tool("speak", {"text": "Bonjour"})
    assert r.is_error and "media_unavailable" in text_of(r) and fake.requests == []


async def test_stop_sound_clears_the_voice_stream(fake, mcp_session, media) -> None:
    media.link.speaker.enqueue(np.zeros(48_000, dtype=np.int16))
    async with mcp_session(media=media) as (client, _):
        r = await client.call_tool("stop_sound", {})
    assert r.structured_content["voice_stream_cleared"] is True and media.link.speaker.cleared == 1
