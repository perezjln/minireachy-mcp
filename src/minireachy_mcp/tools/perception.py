"""Perception (camera, microphone) and voice tools built on the daemon's WebRTC media stream."""

import asyncio
import io
import json
import logging
import math
import time
import uuid
from typing import Annotated, Any, Literal

import anyio
from mcp.server.mcpserver import Image, MCPServer
from pydantic import Field

from ..client import seg
from ..errors import DaemonError
from ..media.audio import capture_utterance
from ..media.speaker import resample_int16
from ..media.webrtc import MediaUnavailableError
from ..models import HeadPose
from ..safety import Risk, check_motion, require
from ._common import Deps, register

logger = logging.getLogger("minireachy_mcp.tools")

_TTS_PREFIX = "mcp_tts_"
# Margin after the computed audio duration: playback starts slightly after play_sound answers.
_PLAYBACK_MARGIN_S = 0.6
# Margin after the last voice frame left the WebRTC track (encoding + network + jitter buffer).
_WEBRTC_MARGIN_S = 0.3

# Antenna-only gestures ([left, right] in radians, duration in seconds). Antennas follow
# their targets faithfully on the tested robot, unlike the head; amplitudes stay far below
# the ±3.0 rad guard rail. Names describe the motion, not a claimed emotion.
GESTURES: dict[str, list[tuple[tuple[float, float], float]]] = {
    "salut": [((0.8, -0.8), 0.35), ((-0.8, 0.8), 0.35), ((0.8, -0.8), 0.35), ((0.0, 0.0), 0.4)],
    "joie": [((0.5, -0.5), 0.3), ((-0.5, 0.5), 0.3), ((0.5, -0.5), 0.3), ((-0.5, 0.5), 0.3), ((0.0, 0.0), 0.35)],
    "curiosite": [((0.7, 0.0), 0.5), ((0.7, 0.0), 0.4), ((0.0, 0.0), 0.5)],
}
GestureName = Literal["aucun", "salut", "joie", "curiosite"]


def register_tools(server: MCPServer, deps: Deps) -> None:
    c = deps.client
    m = deps.media
    s = deps.settings
    lim = s.limits
    last_tts_file: dict[str, str | None] = {"name": None}

    # -- shared implementations ---------------------------------------------------------

    async def voice_stream(timeout_s: float) -> Any:
        """The WebRTC speaker track if the media link is (or quickly becomes) able to carry our voice."""
        if m.link.speaker is None:
            try:
                await m.link.ensure_connected(timeout_s)
            except MediaUnavailableError:
                return None
        return m.link.speaker

    async def say(text: str, speed: float, wait: bool) -> dict[str, Any]:
        mode = s.speech_output
        if mode == "webrtc" or (mode == "auto" and s.enable_perception):
            speaker = await voice_stream(3.0 if mode == "auto" else s.media_connect_timeout_s)
            if speaker is not None:
                return await say_webrtc(speaker, text, speed, wait)
            if mode == "webrtc":
                raise MediaUnavailableError("the voice stream to the robot speaker is not available")
        return await say_daemon(text, speed, wait)

    async def say_webrtc(speaker: Any, text: str, speed: float, wait: bool) -> dict[str, Any]:
        """Stream the voice on the media link, sentence by sentence, as soon as each is synthesised."""
        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()
        chunks: asyncio.Queue[Any] = asyncio.Queue()

        def produce() -> None:  # worker thread
            try:
                for pcm, rate in m.tts.stream(text, 1.0 / speed):
                    loop.call_soon_threadsafe(chunks.put_nowait, (pcm, rate))
            except Exception as exc:
                loop.call_soon_threadsafe(chunks.put_nowait, exc)
            else:
                loop.call_soon_threadsafe(chunks.put_nowait, None)

        producer = asyncio.ensure_future(anyio.to_thread.run_sync(produce))
        first_audio_s: float | None = None
        samples = 0
        while (item := await chunks.get()) is not None:
            if isinstance(item, Exception):
                raise item
            pcm48 = resample_int16(*item)
            speaker.enqueue(pcm48)
            samples += len(pcm48)
            if first_audio_s is None:
                first_audio_s = time.perf_counter() - t0
        await producer
        tts_total_s = time.perf_counter() - t0
        playback = "started"
        if wait:
            try:  # the track is paced in real time: it drains in about the audio duration
                await asyncio.wait_for(speaker.drained(), samples / 48_000 + 5.0)
            except TimeoutError:
                speaker.clear()
                raise MediaUnavailableError("the voice stream stalled (audio not consumed by the link)")
            if m.link.speaker is not speaker:
                playback = "interrupted (media link lost)"
            else:
                await anyio.sleep(_WEBRTC_MARGIN_S)
                playback = "finished (all audio sent + network margin)"
        return {
            "said": text,
            "duration_s": round(samples / 48_000, 2),
            "output": "webrtc",
            "playback": playback,
            "timings_s": {"first_audio": round(first_audio_s or 0.0, 3), "tts_total": round(tts_total_s, 2),
                          "total": round(time.perf_counter() - t0, 2)},
        }

    async def say_daemon(text: str, speed: float, wait: bool) -> dict[str, Any]:
        t0 = time.perf_counter()
        speech = await anyio.to_thread.run_sync(m.tts.synthesize, text, 1.0 / speed)
        name = f"{_TTS_PREFIX}{uuid.uuid4().hex[:10]}.wav"
        await c.post("/api/media/sounds/upload", files={"file": (name, speech.wav, "audio/wav")})
        t_play = time.perf_counter()
        await c.post("/api/media/play_sound", json_body={"file": name})
        play_request_s = time.perf_counter() - t_play
        previous, last_tts_file["name"] = last_tts_file["name"], name
        if previous:
            try:
                await c.delete(f"/api/media/sounds/{seg(previous)}")
            except DaemonError:
                pass  # best-effort cleanup of the temp directory
        if wait:
            await anyio.sleep(speech.duration_s + _PLAYBACK_MARGIN_S)
        return {
            "said": text,
            "duration_s": speech.duration_s,
            "output": "daemon_play_sound",
            "playback": "finished (estimated from duration)" if wait else "started",
            "timings_s": {"tts": speech.seconds, "play_sound_request": round(play_request_s, 2),
                          "total": round(time.perf_counter() - t0, 2)},
        }

    async def hear(max_wait_s: float, max_utterance_s: float, silence_ms: int | None,
                   language: str | None) -> dict[str, Any]:
        await m.link.ensure_connected()
        silence = silence_ms or s.listen_silence_ms
        early = s.listen_early_silence_ms if s.listen_early_silence_ms and s.listen_early_silence_ms < silence else None

        async def transcribe(audio: Any) -> Any:
            return await anyio.to_thread.run_sync(m.stt.transcribe, audio, language)

        deadline = anyio.current_time() + max_wait_s
        ignored: list[str] = []
        while True:
            remaining = deadline - anyio.current_time()
            if remaining <= 0.2:
                break
            utt = await capture_utterance(m.link.audio, max_wait_s=remaining, max_utterance_s=max_utterance_s,
                                          silence_ms=silence, early_silence_ms=early, speculate=transcribe)
            if utt is None:
                break
            doa_task = asyncio.ensure_future(c.get("/api/state/doa"))
            speculative_hit = False
            tr = None
            if utt.speculative is not None:
                try:
                    tr = await utt.speculative
                    speculative_hit = True
                except (asyncio.CancelledError, Exception):
                    tr = None
            if tr is None:
                tr = await transcribe(utt.audio)
            try:
                doa = await doa_task
            except DaemonError:
                doa = None
            if tr.text:
                return {
                    "heard": True,
                    "text": tr.text,
                    "language": tr.language,
                    "waited_s": round(max_wait_s - (deadline - anyio.current_time()), 2),
                    "duration_s": utt.duration_s,
                    "truncated": utt.truncated,
                    "direction_of_arrival": doa,
                    "ignored_sounds": ignored,
                    "timings_s": {
                        "stt": tr.seconds,
                        "end_of_speech_to_text": round(time.monotonic() - utt.speech_ended_at, 2),
                        "speculative_hit": speculative_hit,
                        "speculations_discarded": utt.speculations_discarded,
                    },
                }
            # Noise, the tail of the robot's own voice, or a Whisper hallucination: keep listening.
            ignored.append(f"{utt.duration_s}s:{tr.rejected_reason}")
        return {"heard": False, "text": "", "reason": "no_speech_detected", "waited_s": max_wait_s,
                "ignored_sounds": ignored}

    async def snapshot(max_width: int, quality: int) -> tuple[Image, dict[str, Any]]:
        frame, age = await m.link.latest_frame()

        def encode() -> tuple[bytes, tuple[int, int]]:
            img = frame.to_image()
            img.thumbnail((max_width, max_width * 2))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=quality)
            return out.getvalue(), img.size

        data, size = await anyio.to_thread.run_sync(encode)
        meta = {"source_resolution": [frame.width, frame.height], "image_size": list(size),
                "frame_age_s": round(age, 3), "bytes": len(data)}
        return Image(data=data, format="jpeg"), meta

    async def gesture(name: str) -> dict[str, Any]:
        """Antenna-only sequence; each step is sent then awaited for its duration."""
        steps = GESTURES[name]
        for (left, right), duration in steps:
            check_motion(s, head_pose=None, antennas=(left, right), body_yaw=None,
                         duration=max(duration, lim.min_move_duration_s))
        sent = 0
        for (left, right), duration in steps:
            d = max(duration, lim.min_move_duration_s)
            await c.post("/api/move/goto", json_body={"antennas": [left, right], "duration": d,
                                                      "interpolation": "minjerk"})
            sent += 1
            await anyio.sleep(d)
        return {"gesture": name, "steps_sent": sent}

    # -- tools ----------------------------------------------------------------------------

    @register(server, deps, risk=Risk.READ)
    async def get_perception_status() -> dict[str, Any]:
        """Status of the camera/microphone stream (WebRTC from the daemon), of the local speech models
        and of the background warm-up. Does not open the stream.
        """
        return {
            "perception_enabled": s.enable_perception,
            "media_link": m.link.status(),
            "speech_to_text": {"model": s.stt_model_path, "language": s.stt_language, "loaded": m.stt.loaded},
            "text_to_speech": {"voice": s.tts_voice_path, "loaded": m.tts.loaded},
            "warmup": getattr(m, "warmup_report", {}),
        }

    @register(server, deps, risk=Risk.PERCEPTION, structured=False)
    async def capture_image(
        max_width: Annotated[int, Field(ge=160, le=1920, description="Resize so the width is at most this.")] = 1024,
        quality: Annotated[int, Field(ge=30, le=95, description="JPEG quality.")] = 80,
    ) -> list[Any]:
        """Take a photo with the robot's head camera (what Reachy currently sees).

        Returns the JPEG image plus a JSON line with the original resolution and the frame age.
        The camera is in the head: moving the head (goto_pose) changes the view.
        """
        image, meta = await snapshot(max_width, quality)
        return [image, json.dumps(meta)]

    @register(server, deps, risk=Risk.PERCEPTION, endpoints=("GET /api/state/doa",))
    async def listen(
        max_wait_s: Annotated[float, Field(ge=1, le=60, description="How long to wait for someone to start speaking.")] = 10,
        max_utterance_s: Annotated[float, Field(ge=1, le=60, description="Maximum length of what is recorded.")] = 20,
        silence_ms: Annotated[
            int | None, Field(ge=300, le=3000, description="Silence that ends the utterance (default from config).")
        ] = None,
        language: Annotated[
            str | None, Field(pattern=r"^[a-z]{2}$", description="Force a language (ISO 639-1); default from config.")
        ] = None,
    ) -> dict[str, Any]:
        """Listen through the robot's microphones for ONE utterance and transcribe it (local Whisper).

        Waits until someone starts talking (up to max_wait_s), records until a pause, then returns
        the text. heard=false means nobody spoke (or only noise). Sound captured before the call,
        e.g. the robot's own voice, is ignored. direction_of_arrival tells where the voice came
        from (angle radians: 0 = robot's left, π/2 = front, π = right). For a conversation prefer
        `converse`, which speaks and listens in one call.
        """
        return await hear(max_wait_s, max_utterance_s, silence_ms, language)

    @register(
        server,
        deps,
        risk=Risk.PHYSICAL,
        endpoints=("POST /api/media/sounds/upload", "POST /api/media/play_sound", "DELETE /api/media/sounds/{filename}"),
    )
    async def speak(
        text: Annotated[str, Field(min_length=1, max_length=800, description="What Reachy says (plain text, no markup).")],
        wait: Annotated[bool, Field(description="Return only after the estimated end of playback.")] = True,
        speed: Annotated[float, Field(ge=0.5, le=2.0, description="Speech rate, 1.0 = normal, >1 faster.")] = 1.0,
    ) -> dict[str, Any]:
        """Say a sentence out loud on the robot's speaker (local Piper voice, French by default).

        The audio is synthesised in the container and streamed to the robot over the WebRTC media
        link, sentence by sentence (playback starts ~0.1 s after the call); if that link is not
        available it is uploaded to the daemon and played as a file instead (`output` says which).
        If audio-reactive wobbling is enabled, the head moves while speaking. With wait=true the
        tool returns when playback is over; with wait=false you can start a gesture in parallel.
        Call stop_sound to interrupt.
        """
        return await say(text, speed, wait)

    @register(
        server,
        deps,
        risk=Risk.PERCEPTION,
        structured=False,
        endpoints=("POST /api/media/sounds/upload", "POST /api/media/play_sound",
                   "DELETE /api/media/sounds/{filename}", "POST /api/move/goto", "GET /api/state/doa"),
    )
    async def converse(
        say_text: Annotated[
            str | None,
            Field(max_length=800, description="What Reachy says first (plain text). Omit to only listen."),
        ] = None,
        gesture_name: Annotated[
            GestureName, Field(description="Antenna gesture played while speaking: aucun, salut, joie, curiosite.")
        ] = "aucun",
        listen_after: Annotated[bool, Field(description="Listen for the answer after speaking.")] = True,
        look: Annotated[
            bool, Field(description="Also return a camera image taken right after listening (only if useful).")
        ] = False,
        max_wait_s: Annotated[float, Field(ge=1, le=60, description="How long to wait for an answer.")] = 10,
        speed: Annotated[float, Field(ge=0.5, le=2.0)] = 1.0,
        language: Annotated[str | None, Field(pattern=r"^[a-z]{2}$")] = None,
    ) -> list[Any]:
        """ONE conversation turn in ONE call: say a sentence (with an optional antenna gesture played at
        the same time), then listen to the answer and transcribe it, and optionally look.

        Returns a JSON text with `said`, `heard`, `text` (what the person answered),
        `direction_of_arrival` and timings, plus the camera image when look=true. This is the
        fastest way to hold a conversation: use it instead of speak + listen + capture_image.
        Speaking/gestures need ENABLE_PHYSICAL_ACTIONS; listening/looking need ENABLE_PERCEPTION.
        """
        if say_text or gesture_name != "aucun":
            require(s, Risk.PHYSICAL, "converse (speech/gesture)")
        if not (say_text or listen_after or look):
            return [json.dumps({"error": "nothing to do: give say_text, listen_after or look"})]
        if say_text or gesture_name != "aucun":
            logger.info("action_requested tool=converse say=%r gesture=%s", (say_text or "")[:80], gesture_name)
        t0 = time.perf_counter()
        result: dict[str, Any] = {}
        if say_text and gesture_name != "aucun":
            spoken, moved = await asyncio.gather(say(say_text, speed, True), gesture(gesture_name))
            result.update(spoken)
            result["gesture"] = moved
        elif say_text:
            result.update(await say(say_text, speed, True))
        elif gesture_name != "aucun":
            result["gesture"] = await gesture(gesture_name)
        if listen_after:
            result["answer"] = await hear(max_wait_s, 20, None, language)
            result["heard"] = result["answer"]["heard"]
            result["text"] = result["answer"]["text"]
        content: list[Any] = []
        if look:
            image, meta = await snapshot(1024, 80)
            result["image"] = meta
            content.append(image)
        result["turn_total_s"] = round(time.perf_counter() - t0, 2)
        return [json.dumps(result, ensure_ascii=False), *content]

    @register(server, deps, risk=Risk.PHYSICAL, endpoints=("GET /api/state/doa", "POST /api/move/goto"))
    async def turn_toward_sound(
        duration: Annotated[float, Field(ge=0.3, le=5)] = 0.8,
    ) -> dict[str, Any]:
        """Turn the head (yaw) toward the direction the last sound came from (microphone-array DoA).

        Head yaw is clamped to the server guard rail. Returns the DoA used and the move UUID.
        Assumes positive head yaw turns the head to the robot's left (standard right-handed frame).
        """
        doa = await c.get("/api/state/doa")
        if not doa:
            return {"turned": False, "reason": "direction of arrival unavailable"}
        yaw = math.pi / 2 - float(doa["angle"])
        yaw = max(-lim.max_head_yaw_rad, min(lim.max_head_yaw_rad, yaw))
        pose = {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": round(yaw, 4)}
        check_motion(s, head_pose=HeadPose(**pose), antennas=None, body_yaw=None, duration=duration)
        status = await c.get("/api/daemon/status")
        if status.get("state") != "running":
            return {"turned": False, "reason": f"daemon state {status.get('state')!r}"}
        move = await c.post("/api/move/goto", json_body={"head_pose": pose, "duration": duration,
                                                          "interpolation": "minjerk"})
        return {"turned": True, "doa": doa, "target_yaw_rad": pose["yaw"], "move_uuid": move.get("uuid"),
                "completion_confirmed": False}
