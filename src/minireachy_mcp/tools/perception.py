"""Perception (camera, microphone) and voice tools built on the daemon's WebRTC media stream."""

import asyncio
import io
import json
import logging
import math
import re
import time
import uuid
from typing import Annotated, Any, Literal

import anyio
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
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
# Head-only gestures: (pitch, yaw) offsets in radians from the current head direction, duration.
HEAD_GESTURES: dict[str, list[tuple[tuple[float, float], float]]] = {
    "oui": [((0.15, 0.0), 0.3), ((-0.05, 0.0), 0.3), ((0.12, 0.0), 0.3), ((0.0, 0.0), 0.35)],
    "non": [((0.0, 0.2), 0.3), ((0.0, -0.2), 0.4), ((0.0, 0.15), 0.35), ((0.0, 0.0), 0.35)],
}
_Finite = Annotated[float, Field(allow_inf_nan=False)]
GestureName = Literal["aucun", "salut", "joie", "curiosite", "oui", "non"]
LookMode = Literal["auto", "after", "never"]

# Words in the person's answer that mean "look at this": with look="auto", converse returns
# an image only when one of them is heard (the image is taken right after the sentence).
_VISUAL_CUES = re.compile(
    r"\b(regard\w*|vois|voit|voir|vu|vue|montr\w*|[ée]crit\w*|lis|lire|lisible|couleur\w*|photo\w*|"
    r"cam[ée]ra|image|devant (?:toi|moi)|qu'est-ce que c'est|c'est quoi (?:ça|ca)|"
    r"look\w*|see|seeing|watch\w*|show\w*|read|written|colou?r\w*|what(?: i|')s (?:this|that))\b",
    re.IGNORECASE,
)
# Horizontal field of view used when the daemon's camera specs cannot be interpreted.
_FALLBACK_HFOV_RAD = math.radians(80)


def register_tools(server: MCPServer, deps: Deps) -> None:
    c = deps.client
    m = deps.media
    s = deps.settings
    lim = s.limits
    last_tts_file: dict[str, str | None] = {"name": None}
    # Last image returned to the client, with the head pose read at the same instant: a point
    # chosen in that image stays valid even if the head moved since.
    last_view: dict[str, Any] = {}
    camera: dict[int, float] = {}
    voice_lock = anyio.Lock()

    # -- head direction helpers -----------------------------------------------------------

    async def present_head() -> dict[str, float] | None:
        try:
            pose = await c.get("/api/state/present_head_pose")
        except DaemonError:
            return None
        return pose if isinstance(pose, dict) and "yaw" in pose else None

    async def focal_px(source_width: int) -> float:
        """Focal length in pixels of the streamed image (pinhole approximation, center = image center).

        The daemon gives K for the full sensor (largest resolution); the stream is that sensor
        cropped by `crop_factor` then scaled to `source_width`.
        """
        if source_width not in camera:
            fallback = source_width / (2 * math.tan(_FALLBACK_HFOV_RAD / 2))
            focal = fallback
            try:
                specs = await c.get("/api/camera/specs")
                full_w = max(r["width"] for r in specs["available_resolutions"])
                mode = next((r for r in specs["available_resolutions"] if r["width"] == source_width),
                            specs["default_resolution"])
                focal = float(specs["K"][0][0]) * source_width / full_w * float(mode.get("crop_factor") or 1.0)
            except (DaemonError, KeyError, TypeError, ValueError, IndexError):
                focal = fallback
            if not 0.3 * source_width < focal < 3 * source_width:  # implausible specs
                focal = fallback
            camera[source_width] = focal
        return camera[source_width]

    def clamp_head(pitch: float, yaw: float) -> tuple[dict[str, float], bool]:
        """Straight head (roll 0, centered) toward pitch/yaw, clamped to the guard rails."""
        if not (math.isfinite(pitch) and math.isfinite(yaw)):
            # min/max would turn NaN into the largest allowed angle.
            raise ToolError(f"invalid head target (pitch={pitch}, yaw={yaw}): values must be finite numbers")
        p = max(-lim.max_head_pitch_rad, min(lim.max_head_pitch_rad, pitch))
        y = max(-lim.max_head_yaw_rad, min(lim.max_head_yaw_rad, yaw))
        pose = {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": round(p, 4), "yaw": round(y, 4)}
        return pose, (p, y) != (pitch, yaw)

    async def head_toward_point(x: float, y: float) -> dict[str, Any]:
        """Target head pose that puts pixel (x, y) of the last image at the image center."""
        if not last_view:
            raise ToolError("no image yet: take one with capture_image or converse(look=...) first")
        width, height = last_view["image_size"]
        if not (0 <= x <= width and 0 <= y <= height):
            raise ToolError(f"point ({x}, {y}) is outside the last image ({width}x{height})")
        base = last_view["head_pose"]
        if base is None:
            raise ToolError("the head pose at the time of the last image is unknown (daemon unreachable)")
        source_w = last_view["source_resolution"][0]
        focal = await focal_px(source_w) * width / source_w
        d_yaw = -math.atan((x - width / 2) / focal)  # right of center -> turn right (yaw decreases)
        d_pitch = math.atan((y - height / 2) / focal)  # below center -> look down (pitch increases)
        pose, clamped = clamp_head(base["pitch"] + d_pitch, base["yaw"] + d_yaw)
        return {"head_pose": pose, "clamped": clamped, "offset_rad": {"yaw": round(d_yaw, 3),
                                                                      "pitch": round(d_pitch, 3)}}

    async def send_head(pose: dict[str, float], duration: float) -> str | None:
        check_motion(s, head_pose=HeadPose(**pose), antennas=None, body_yaw=None, duration=duration)
        move = await c.post("/api/move/goto", json_body={"head_pose": pose, "duration": duration,
                                                         "interpolation": "minjerk"})
        return move.get("uuid") if isinstance(move, dict) else None

    # -- shared implementations ---------------------------------------------------------

    async def voice_stream(timeout_s: float) -> Any:
        """The WebRTC speaker track if the media link is (or quickly becomes) able to carry our voice."""
        if m.link.speaker is None:
            try:
                await m.link.ensure_connected(timeout_s)
            except MediaUnavailableError:
                return None
        return m.link.speaker

    def pick_voice(language: str | None) -> tuple[Any, str]:
        """The voice for `language` (a plain dict lookup: no loading here, the warm-up did it)."""
        if language is None or language == s.tts_language:
            return m.tts, s.tts_language
        voice = getattr(m, "voices", {}).get(language)
        if voice is None:
            return m.tts, f"{s.tts_language} (no '{language}' voice installed)"
        return voice, language

    async def say(text: str, speed: float, wait: bool, language: str | None = None) -> dict[str, Any]:
        # One voice at a time: concurrent calls would interleave their sentences on the speaker.
        async with voice_lock, m.link.use():
            return await say_unlocked(text, speed, wait, language)

    async def say_unlocked(text: str, speed: float, wait: bool, language: str | None) -> dict[str, Any]:
        tts, voice = pick_voice(language)
        mode = s.speech_output
        if mode == "webrtc" or (mode == "auto" and s.enable_perception):
            speaker = await voice_stream(3.0 if mode == "auto" else s.media_connect_timeout_s)
            if speaker is not None:
                return {**await say_webrtc(speaker, tts, text, speed, wait), "voice": voice}
            if mode == "webrtc":
                raise MediaUnavailableError("the voice stream to the robot speaker is not available")
        return {**await say_daemon(tts, text, speed, wait), "voice": voice}

    async def say_webrtc(speaker: Any, tts: Any, text: str, speed: float, wait: bool) -> dict[str, Any]:
        """Stream the voice on the media link, sentence by sentence, as soon as each is synthesised."""
        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()
        chunks: asyncio.Queue[Any] = asyncio.Queue()

        def produce() -> None:  # worker thread
            try:
                for pcm, rate in tts.stream(text, 1.0 / speed):
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

    async def say_daemon(tts: Any, text: str, speed: float, wait: bool) -> dict[str, Any]:
        t0 = time.perf_counter()
        speech = await anyio.to_thread.run_sync(tts.synthesize, text, 1.0 / speed)
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
        async with m.link.use():  # the stream must not go idle while we are listening
            return await listen_for_utterance(max_wait_s, max_utterance_s, silence_ms, language)

    async def listen_for_utterance(max_wait_s: float, max_utterance_s: float, silence_ms: int | None,
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
            try:
                if utt.speculative is not None:
                    try:
                        tr = await utt.speculative
                        speculative_hit = True
                    except Exception:  # not CancelledError: a cancelled tool call must stop here
                        tr = None
                if tr is None:
                    tr = await transcribe(utt.audio)
            except BaseException:
                doa_task.cancel()  # never leave an orphan request task behind
                raise
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
        async with m.link.use():
            (frame, age), head = await asyncio.gather(m.link.latest_frame(), present_head())

        def encode() -> tuple[bytes, tuple[int, int]]:
            img = frame.to_image()
            img.thumbnail((max_width, max_width * 2))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=quality)
            return out.getvalue(), img.size

        data, size = await anyio.to_thread.run_sync(encode)
        meta = {"source_resolution": [frame.width, frame.height], "image_size": list(size),
                "frame_age_s": round(age, 3), "bytes": len(data)}
        last_view.clear()
        last_view.update(meta, head_pose=head)
        return Image(data=data, format="jpeg"), meta

    async def gesture(name: str, base: dict[str, float] | None = None) -> dict[str, Any]:
        """Antenna or head sequence; each step is sent then awaited for its duration.

        Head gestures move around `base` (the head target just commanded) or the present head pose.
        """
        if name in HEAD_GESTURES:
            base = base or await present_head() or {"pitch": 0.0, "yaw": 0.0}
            sent = 0
            for (d_pitch, d_yaw), duration in HEAD_GESTURES[name]:
                pose, _ = clamp_head(base["pitch"] + d_pitch, base["yaw"] + d_yaw)
                await send_head(pose, max(duration, lim.min_move_duration_s))
                sent += 1
                await anyio.sleep(max(duration, lim.min_move_duration_s))
            return {"gesture": name, "steps_sent": sent}
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
            "text_to_speech": {
                "voice": s.tts_voice_path,
                "loaded": m.tts.loaded,
                "voices": {
                    s.tts_language: {"voice": s.tts_voice_path, "loaded": m.tts.loaded},
                    **{lang: {"voice": v.voice_path, "loaded": v.loaded}
                       for lang, v in getattr(m, "voices", {}).items()},
                },
            },
            "warmup": getattr(m, "warmup_report", {}),
        }

    @register(server, deps, risk=Risk.PERCEPTION, structured=False, endpoints=("GET /api/state/present_head_pose",))
    async def capture_image(
        max_width: Annotated[int, Field(ge=160, le=1920, description="Resize so the width is at most this.")] = 1024,
        quality: Annotated[int, Field(ge=30, le=95, description="JPEG quality.")] = 80,
    ) -> list[Any]:
        """Take a photo with the robot's head camera (what Reachy currently sees).

        Returns the JPEG image plus a JSON line with the original resolution and the frame age.
        The camera is in the head: moving the head changes the view. To center something seen
        in this image, call look_at_point with its pixel coordinates.
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
        language: Annotated[
            str | None,
            Field(pattern=r"^[a-z]{2}$", description="Language of `text` (ISO 639-1): picks the voice (fr, en)."),
        ] = None,
    ) -> dict[str, Any]:
        """Say a sentence out loud on the robot's speaker (local Piper voice, French by default,
        English with language="en"; `voice` in the result says which one spoke).

        The audio is synthesised in the container and streamed to the robot over the WebRTC media
        link, sentence by sentence (playback starts ~0.1 s after the call); if that link is not
        available it is uploaded to the daemon and played as a file instead (`output` says which).
        If audio-reactive wobbling is enabled, the head moves while speaking. With wait=true the
        tool returns when playback is over; with wait=false you can start a gesture in parallel.
        Call stop_sound to interrupt.
        """
        return await say(text, speed, wait, language)

    @register(
        server,
        deps,
        risk=Risk.PERCEPTION,
        structured=False,
        # Gated on perception, but it may also speak and move the head: never advertise it as read-only.
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
                                    open_world_hint=True),
        endpoints=("POST /api/media/sounds/upload", "POST /api/media/play_sound",
                   "DELETE /api/media/sounds/{filename}", "POST /api/move/goto", "GET /api/state/doa",
                   "GET /api/state/present_head_pose", "GET /api/camera/specs"),
    )
    async def converse(
        say_text: Annotated[
            str | None,
            Field(max_length=800, description="What Reachy says first (plain text). Omit to only listen."),
        ] = None,
        gesture_name: Annotated[
            GestureName,
            Field(description="Gesture played while speaking: antennas (salut, joie, curiosite) or head "
                  "(oui = nod, non = shake), or aucun."),
        ] = "aucun",
        listen_after: Annotated[bool, Field(description="Listen for the answer after speaking.")] = True,
        look: Annotated[
            LookMode | bool,
            Field(description="Camera image taken right after listening: 'auto' = only when the answer "
                  "asks to look (regarde, tu vois, montre, écrit, couleur, look, see...), 'after' (or true) "
                  "= always, 'never' (or false) = no image."),
        ] = "auto",
        look_at_point: Annotated[
            tuple[_Finite, _Finite] | None,
            Field(description="Turn the head while speaking so that this pixel [x, y] of the LAST image "
                  "returned (by converse or capture_image) comes to the center of the view."),
        ] = None,
        head_turn: Annotated[
            tuple[_Finite, _Finite] | None,
            Field(description="Turn the head while speaking by [yaw, pitch] radians relative to where it "
                  "points now (yaw > 0 = Reachy's left, pitch > 0 = down). The head is also straightened."),
        ] = None,
        max_wait_s: Annotated[float, Field(ge=1, le=60, description="How long to wait for an answer.")] = 10,
        speed: Annotated[float, Field(ge=0.5, le=2.0)] = 1.0,
        language: Annotated[
            str | None,
            Field(pattern=r"^[a-z]{2}$", description="Language of the turn (ISO 639-1): picks the voice "
                  "(fr, en) and the speech recognition language. Default from config (French)."),
        ] = None,
    ) -> list[Any]:
        """ONE conversation turn in ONE call: say a sentence while moving (antenna or head gesture, head
        turned toward a point of the last image), then listen to the answer, and look when useful.

        For a conversation in English pass language="en" on every turn (English voice and English
        recognition, no extra latency); omit it to go back to French.

        Returns a JSON text with `said`, `heard`, `text` (what the person answered),
        `direction_of_arrival` and timings, plus the camera image when one was taken (`look` says
        why). This is the fastest way to hold a conversation: use it instead of speak + listen +
        capture_image + goto_pose. Speaking/moving need ENABLE_PHYSICAL_ACTIONS; listening/looking
        need ENABLE_PERCEPTION.
        """
        look_mode = "after" if look is True else "never" if look is False else look
        if look_at_point is not None and head_turn is not None:
            raise ToolError("give look_at_point or head_turn, not both")
        moving = gesture_name != "aucun" or look_at_point is not None or head_turn is not None
        if say_text or moving:
            require(s, Risk.PHYSICAL, "converse (speech/motion)")
        if not (say_text or moving or listen_after or look_mode == "after"):
            return [json.dumps({"error": "nothing to do: give say_text, a motion, listen_after or look"})]
        # Resolve the head target before acting, so an invalid request does nothing at all.
        head: dict[str, Any] | None = None
        if look_at_point is not None:
            head = await head_toward_point(*look_at_point)
        elif head_turn is not None:
            now = await present_head() or {"pitch": 0.0, "yaw": 0.0}
            pose, clamped = clamp_head(now["pitch"] + head_turn[1], now["yaw"] + head_turn[0])
            head = {"head_pose": pose, "clamped": clamped}
        if say_text or moving:
            logger.info("action_requested tool=converse say=%r gesture=%s head=%s",
                        (say_text or "")[:80], gesture_name, head and head["head_pose"])
        t0 = time.perf_counter()
        result: dict[str, Any] = {}

        async def motion() -> None:
            # Head first, then the gesture (two concurrent goto moves would fight each other).
            if head is not None:
                pose = head["head_pose"]
                duration = min(1.5, max(0.6, 0.4 + max(abs(pose["yaw"]), abs(pose["pitch"]))))
                head["move_uuid"] = await send_head(pose, duration)
                result["head"] = head
                await anyio.sleep(duration)
            if gesture_name != "aucun":
                result["gesture"] = await gesture(gesture_name, base=head and head["head_pose"])

        if say_text and moving:
            spoken, _ = await asyncio.gather(say(say_text, speed, True, language), motion())
            result.update(spoken)
        elif say_text:
            result.update(await say(say_text, speed, True, language))
        elif moving:
            await motion()
        if listen_after:
            result["answer"] = await hear(max_wait_s, 20, None, language)
            result["heard"] = result["answer"]["heard"]
            result["text"] = result["answer"]["text"]
        reason = None
        if look_mode == "after":
            reason = "requested"
        elif look_mode == "auto" and (cue := _VISUAL_CUES.search(result.get("text") or "")):
            reason = f"auto: heard '{cue.group(0)}'"
        content: list[Any] = []
        if reason:
            image, meta = await snapshot(1024, 80)
            result["look"] = reason
            result["image"] = meta
            content.append(image)
        result["turn_total_s"] = round(time.perf_counter() - t0, 2)
        return [json.dumps(result, ensure_ascii=False), *content]

    @register(
        server,
        deps,
        risk=Risk.PHYSICAL,
        endpoints=("GET /api/daemon/status", "GET /api/camera/specs", "POST /api/move/goto"),
    )
    async def look_at_point(
        x: Annotated[float, Field(ge=0, description="Pixel column in the last image (0 = left edge).")],
        y: Annotated[float, Field(ge=0, description="Pixel row in the last image (0 = top edge).")],
        duration: Annotated[float, Field(ge=0.3, le=5)] = 0.8,
    ) -> dict[str, Any]:
        """Turn the head so that the pixel (x, y) of the LAST image returned (capture_image or
        converse) comes to the center of the view, e.g. to center an object someone shows.

        Works from the head pose recorded when that image was taken, so it stays correct even if
        the head moved since. The head is also straightened (roll 0). Angles are clamped to the
        guard rails (`clamped` says so). Pinhole approximation: precise near the center, a few
        degrees off near the edges; take a new image to check and refine.
        """
        head = await head_toward_point(x, y)
        status = await c.get("/api/daemon/status")
        if status.get("state") != "running":
            return {"turned": False, "reason": f"daemon state {status.get('state')!r}"}
        move_uuid = await send_head(head["head_pose"], duration)
        return {"turned": True, **head, "move_uuid": move_uuid, "completion_confirmed": False}

    @register(
        server,
        deps,
        risk=Risk.PHYSICAL,
        endpoints=("GET /api/daemon/status", "GET /api/state/present_head_pose", "POST /api/move/goto"),
    )
    async def nudge_head(
        yaw: Annotated[float, Field(ge=-1.0, le=1.0, description="Relative yaw in radians (> 0 = Reachy's left).")] = 0.0,
        pitch: Annotated[float, Field(ge=-0.6, le=0.6, description="Relative pitch in radians (> 0 = down).")] = 0.0,
        duration: Annotated[float, Field(ge=0.3, le=5)] = 0.8,
    ) -> dict[str, Any]:
        """Turn the head by a small amount relative to where it points now ("a bit to the right":
        yaw=-0.15). The head is also straightened (roll 0). Clamped to the guard rails.
        """
        status = await c.get("/api/daemon/status")
        if status.get("state") != "running":
            return {"turned": False, "reason": f"daemon state {status.get('state')!r}"}
        now = await present_head()
        if now is None:
            return {"turned": False, "reason": "present head pose unavailable"}
        pose, clamped = clamp_head(now["pitch"] + pitch, now["yaw"] + yaw)
        move_uuid = await send_head(pose, duration)
        return {"turned": True, "from": {"pitch": round(now["pitch"], 4), "yaw": round(now["yaw"], 4)},
                "head_pose": pose, "clamped": clamped, "move_uuid": move_uuid, "completion_confirmed": False}

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
        if not math.isfinite(yaw):  # min/max below would turn NaN into the full guard-rail angle
            return {"turned": False, "reason": f"invalid direction of arrival {doa.get('angle')!r}"}
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
