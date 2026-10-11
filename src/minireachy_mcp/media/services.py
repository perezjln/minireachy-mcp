"""Bundle of media services, created lazily (nothing connects or loads at server start
unless the optional background warm-up is enabled)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import anyio

from ..config import Settings
from .speech import SpeechToText, TextToSpeech
from .webrtc import MediaLink

logger = logging.getLogger("minireachy_mcp.media")


@dataclass
class MediaServices:
    link: MediaLink
    stt: SpeechToText
    tts: TextToSpeech
    # Other voices by language (ISO 639-1); `tts` is the default one.
    voices: dict[str, TextToSpeech] = field(default_factory=dict)
    warmup_report: dict[str, Any] = field(default_factory=dict)
    _warmup_task: asyncio.Task[None] | None = None

    @classmethod
    def from_settings(cls, s: Settings) -> MediaServices:
        voices = {}
        if s.tts_voice_path_en and s.tts_language != "en":
            voices["en"] = TextToSpeech(s.tts_voice_path_en, length_scale=s.tts_length_scale, language="en")
        return cls(
            link=MediaLink(s.media_signalling_url, producer_name=s.media_producer_name,
                           connect_timeout_s=s.media_connect_timeout_s),
            stt=SpeechToText(s.stt_model_path, language=s.stt_language, compute_type=s.stt_compute_type,
                             beam_size=s.stt_beam_size, cpu_threads=s.stt_cpu_threads,
                             initial_prompt=s.stt_initial_prompt, vad_filter=s.stt_vad_filter),
            tts=TextToSpeech(s.tts_voice_path, length_scale=s.tts_length_scale, language=s.tts_language),
            voices=voices,
        )

    def start_warmup(self) -> None:
        """Background: load both speech models and open the media stream.

        Never blocks the server start, never acts on the robot (no sound, no motion):
        it only loads models and subscribes to the camera/microphone stream.
        """
        if self._warmup_task is None:
            self._warmup_task = asyncio.create_task(self._warmup(), name="minireachy-warmup")

    async def _warmup(self) -> None:
        self.warmup_report["state"] = "running"
        for name, fn in (("text_to_speech_s", self.tts.warmup), ("speech_to_text_s", self.stt.warmup)):
            try:
                self.warmup_report[name] = await anyio.to_thread.run_sync(fn)
            except Exception as exc:  # report, keep the server usable
                self.warmup_report[name] = f"error: {type(exc).__name__}: {exc}"[:200]
        try:
            started = anyio.current_time()
            await self.link.ensure_connected()
            self.warmup_report["media_link_s"] = round(anyio.current_time() - started, 2)
        except Exception as exc:
            self.warmup_report["media_link_s"] = f"error: {exc}"[:200]
        # Extra voices last, so the default conversation path is ready first; loading them here
        # keeps the first sentence in another language from paying the model load (~1 s).
        for language, voice in self.voices.items():
            try:
                self.warmup_report[f"text_to_speech_{language}_s"] = await anyio.to_thread.run_sync(voice.warmup)
            except Exception as exc:
                self.warmup_report[f"text_to_speech_{language}_s"] = f"error: {type(exc).__name__}: {exc}"[:200]
        self.warmup_report["state"] = "done"
        logger.info("media warm-up %s", self.warmup_report)

    async def aclose(self) -> None:
        if self._warmup_task is not None:
            self._warmup_task.cancel()
            try:
                await self._warmup_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.link.close()
