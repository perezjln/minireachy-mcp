"""Perception and voice MCP tools with stubbed media services and the simulated daemon."""

import asyncio
import base64
import contextlib
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
        self.generation = 0
        self._busy = False

    def enqueue(self, pcm48: np.ndarray) -> None:
        self.samples += len(pcm48)
        self._busy = True

    def clear(self) -> None:
        self.cleared += 1
        self.generation += 1
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
        self.leases = 0
        self.max_leases = 0

    @contextlib.asynccontextmanager
    async def use(self):
        self.leases += 1
        self.max_leases = max(self.max_leases, self.leases)
        try:
            yield
        finally:
            self.leases -= 1

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

    def __init__(self, voice_path: str = "/stub/fr.onnx") -> None:
        self.voice_path = voice_path
        self.spoken: list[str] = []

    def stream(self, text: str, length_scale: float | None = None, *, speed: float = 1.0):
        self.spoken.append(text)
        for _ in range(2):  # two "sentences" of 0.5 s at 22.05 kHz
            yield np.full(11025, 1000, dtype=np.int16), 22050

    def warmup(self) -> float:
        return 0.01

    def synthesize(self, text: str, length_scale: float | None = None, *, speed: float = 1.0) -> Speech:
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
        self.voices = {"en": StubTTS("/stub/en.onnx")}

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


async def test_converse_in_english_uses_english_voice_and_recognition(fake, mcp_session, media) -> None:
    media.link.speech_after_s = 0.4
    languages: list[str | None] = []
    original = media.stt.transcribe

    def spy(audio, language=None):
        languages.append(language)
        return original(audio, language)

    media.stt.transcribe = spy  # type: ignore[method-assign]
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        r = await client.call_tool("converse", {"say_text": "Hello!", "language": "en", "max_wait_s": 5})
    assert not r.is_error, text_of(r)
    out = json_part(r)
    assert out["voice"] == "en" and out["heard"] is True
    assert media.voices["en"].spoken == ["Hello!"] and media.tts.spoken == []
    assert languages and set(languages) == {"en"}


async def test_speak_default_and_unknown_language_use_default_voice(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        r1 = await client.call_tool("speak", {"text": "Bonjour"})
        r2 = await client.call_tool("speak", {"text": "Hallo", "language": "de"})
    assert r1.structured_content["voice"] == "fr"
    assert r2.structured_content["voice"].startswith("fr (no 'de' voice")
    assert media.tts.spoken == ["Bonjour", "Hallo"] and media.voices["en"].spoken == []


async def test_perception_status_lists_voices(fake, mcp_session, media) -> None:
    async with mcp_session(media=media) as (client, _):
        r = await client.call_tool("get_perception_status", {})
    voices = r.structured_content["text_to_speech"]["voices"]
    assert set(voices) == {"fr", "en"} and voices["en"]["voice"] == "/stub/en.onnx"


def test_from_settings_builds_english_voice_unless_disabled() -> None:
    from minireachy_mcp.config import load_settings
    from minireachy_mcp.media.services import MediaServices

    assert set(MediaServices.from_settings(load_settings({})).voices) == {"en"}
    assert MediaServices.from_settings(load_settings({"TTS_VOICE_PATH_EN": ""})).voices == {}


async def test_warmup_loads_models_and_opens_stream_without_robot_action(fake) -> None:
    from minireachy_mcp.media.services import MediaServices

    stub = StubMedia()
    services = MediaServices(link=stub.link, stt=stub.stt, tts=stub.tts, voices=stub.voices)
    services.start_warmup()
    await services._warmup_task
    assert services.warmup_report["state"] == "done"
    assert services.warmup_report["speech_to_text_s"] == 0.01 and "media_link_s" in services.warmup_report
    assert services.warmup_report["text_to_speech_en_s"] == 0.01
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


# -- gaze: look_at_point, nudge_head, head motion and automatic look in converse -----------

BASE = {"pitch": 0.0262, "yaw": -0.0176}  # fake daemon present head pose
REAL_SPECS = {
    "name": "lite",
    "available_resolutions": [{"name": "R1920x1080at60fps", "width": 1920, "height": 1080, "fps": 60,
                               "crop_factor": 1.115},
                              {"name": "R3840x2592at30fps", "width": 3840, "height": 2592, "fps": 30,
                               "crop_factor": 1.0}],
    "default_resolution": {"name": "R1920x1080at60fps", "width": 1920, "height": 1080, "fps": 60,
                           "crop_factor": 1.115},
    "K": [[2001.8, 0.0, 1905.9], [0.0, 2003.1, 1328.3], [0.0, 0.0, 1.0]],
    "D": [0.0],
}


def head_gotos(fake) -> list[dict]:
    return [c.body for c in fake.calls("POST") if c.path == "/api/move/goto" and "head_pose" in c.body]


def answer(text: str):
    return lambda audio, language=None: Transcript(text=text, language="fr", language_probability=0.99,
                                                   no_speech_probability=0.01, seconds=0.1)


async def test_look_at_point_needs_an_image_first(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        r = await client.call_tool("look_at_point", {"x": 100, "y": 100})
    assert r.is_error and "no image yet" in text_of(r) and fake.non_get_calls() == []


async def test_look_at_point_uses_camera_intrinsics_and_pose_at_capture(fake, mcp_session, media) -> None:
    fake.overrides["GET /api/camera/specs"] = Override(body=REAL_SPECS)
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        await client.call_tool("capture_image", {})  # 1024x576 from a 1920x1080 frame
        # The head moves after the photo: the target must still be computed from the capture pose.
        fake.overrides["GET /api/state/present_head_pose"] = Override(
            body={"x": 0, "y": 0, "z": 0, "roll": 0.1, "pitch": 0.3, "yaw": 0.5})
        center = await client.call_tool("look_at_point", {"x": 512, "y": 288})
        right_low = await client.call_tool("look_at_point", {"x": 1024, "y": 576})
    assert not center.is_error, text_of(center)
    focal = 2001.8 * 1920 / 3840 * 1.115 * 1024 / 1920  # ~595 px in the returned image
    first, second = head_gotos(fake)
    assert first["head_pose"]["yaw"] == pytest.approx(BASE["yaw"], abs=1e-3)
    assert first["head_pose"]["roll"] == 0.0  # straightened
    assert second["head_pose"]["yaw"] == pytest.approx(BASE["yaw"] - math.atan(512 / focal), abs=1e-3)
    assert second["head_pose"]["pitch"] == pytest.approx(BASE["pitch"] + math.atan(288 / focal), abs=1e-3)
    assert right_low.structured_content["clamped"] is False


async def test_look_at_point_rejects_points_outside_and_clamps(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        await client.call_tool("capture_image", {"max_width": 640})
        outside = await client.call_tool("look_at_point", {"x": 700, "y": 10})
        fake.overrides["GET /api/state/present_head_pose"] = Override(
            body={"x": 0, "y": 0, "z": 0, "roll": 0, "pitch": 0, "yaw": 1.1})
        await client.call_tool("capture_image", {"max_width": 640})
        clamped = await client.call_tool("look_at_point", {"x": 0, "y": 180})
    assert outside.is_error and "outside the last image" in text_of(outside)
    assert clamped.structured_content["clamped"] is True
    assert head_gotos(fake)[-1]["head_pose"]["yaw"] == 1.2  # guard rail


async def test_nudge_head_is_relative_to_present_pose(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True) as (client, _):
        r = await client.call_tool("nudge_head", {"yaw": -0.15, "pitch": 0.1})
    assert not r.is_error, text_of(r)
    pose = head_gotos(fake)[0]["head_pose"]
    assert pose["yaw"] == pytest.approx(BASE["yaw"] - 0.15, abs=1e-3)
    assert pose["pitch"] == pytest.approx(BASE["pitch"] + 0.1, abs=1e-3)
    assert pose["roll"] == 0.0 and pose["x"] == 0.0


async def test_nudge_head_refused_without_physical(fake, mcp_session, media) -> None:
    async with mcp_session(media=media) as (client, _):
        r = await client.call_tool("nudge_head", {"yaw": 0.1})
    assert r.is_error and fake.non_get_calls() == []


async def test_converse_turns_head_then_gestures_while_speaking(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        await client.call_tool("capture_image", {})
        started = time.monotonic()
        r = await client.call_tool("converse", {"say_text": "Je regarde !", "gesture_name": "joie",
                                                "look_at_point": [800, 288], "listen_after": False})
        elapsed = time.monotonic() - started
    assert not r.is_error, text_of(r)
    out = json_part(r)
    gotos = [c.body for c in fake.calls("POST") if c.path == "/api/move/goto"]
    assert "head_pose" in gotos[0] and all("head_pose" not in g for g in gotos[1:])  # head, then antennas
    assert out["head"]["head_pose"]["yaw"] < BASE["yaw"] and out["gesture"]["steps_sent"] == 5
    assert "image" not in out  # nothing heard, nothing to look at
    assert elapsed < 4.0


async def test_converse_head_turn_and_head_gesture(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        r = await client.call_tool("converse", {"say_text": "Oui !", "gesture_name": "oui",
                                                "head_turn": [0.3, 0.0], "listen_after": False})
    assert not r.is_error, text_of(r)
    poses = [g["head_pose"] for g in head_gotos(fake)]
    target = poses[0]
    assert target["yaw"] == pytest.approx(BASE["yaw"] + 0.3, abs=1e-3)
    assert len(poses) == 1 + 4  # turn, then the 4 nod steps around the new direction
    assert all(p["yaw"] == pytest.approx(target["yaw"], abs=1e-3) for p in poses[1:])
    assert max(p["pitch"] for p in poses[1:]) == pytest.approx(target["pitch"] + 0.15, abs=1e-3)
    assert poses[-1]["pitch"] == pytest.approx(target["pitch"], abs=1e-3)


async def test_converse_rejects_both_head_targets(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        r = await client.call_tool("converse", {"say_text": "x", "look_at_point": [1, 1], "head_turn": [0.1, 0]})
    assert r.is_error and fake.non_get_calls() == []


@pytest.mark.parametrize(
    "heard,expect_image",
    [("Regarde ce que j'ai dans la main", True), ("Qu'est-ce qui est écrit dessus ?", True),
     ("Can you see this?", True), ("Bonjour Reachy", False), ("Au revoir Reachy", False)],
)
async def test_converse_auto_look_on_visual_cues(fake, mcp_session, media, heard, expect_image) -> None:
    media.link.speech_after_s = 0.3
    media.stt.transcribe = answer(heard)  # type: ignore[method-assign]
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        r = await client.call_tool("converse", {"say_text": "Vas-y", "max_wait_s": 5})
    out = json_part(r)
    assert out["text"] == heard
    assert any(c.type == "image" for c in r.content) is expect_image
    assert ("look" in out) is expect_image


async def test_converse_look_never_and_bool_compat(fake, mcp_session, media) -> None:
    media.link.speech_after_s = 0.3
    media.stt.transcribe = answer("Regarde")  # type: ignore[method-assign]
    async with mcp_session(media=media, enable_perception=True) as (client, _):
        never = await client.call_tool("converse", {"look": "never", "max_wait_s": 5})
        media.link.speech_after_s = None
        legacy = await client.call_tool("converse", {"look": True, "listen_after": False})
    assert not any(c.type == "image" for c in never.content)
    assert any(c.type == "image" for c in legacy.content) and json_part(legacy)["look"] == "requested"
