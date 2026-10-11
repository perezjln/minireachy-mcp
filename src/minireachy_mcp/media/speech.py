"""Local speech-to-text (faster-whisper) and text-to-speech (Piper). Models are loaded lazily."""

from __future__ import annotations

import io
import re
import threading
import time
import wave
from dataclasses import dataclass
from typing import Any

import numpy as np

# Phrases Whisper is known to hallucinate on silence/noise (mostly subtitle credits).
_HALLUCINATIONS = re.compile(
    r"(sous-titr|amara\.org|merci d'avoir regard|abonnez-vous|thanks for watching|transcrit par|"
    r"subtitles by|♪)",
    re.IGNORECASE,
)


@dataclass
class Transcript:
    text: str
    language: str
    language_probability: float
    no_speech_probability: float
    seconds: float
    rejected_reason: str | None = None


class SpeechToText:
    def __init__(self, model_path: str, *, language: str = "fr", compute_type: str = "int8",
                 beam_size: int = 1, cpu_threads: int = 0, initial_prompt: str | None = None,
                 vad_filter: bool = False) -> None:
        self.model_path = model_path
        self.language = language
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.cpu_threads = cpu_threads
        self.initial_prompt = initial_prompt
        self.vad_filter = vad_filter
        self._model: Any = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(self.model_path, device="cpu", compute_type=self.compute_type,
                                           cpu_threads=self.cpu_threads)
        return self._model

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> Transcript:
        """Blocking; run it in a worker thread."""
        started = time.perf_counter()
        model = self._load()
        segments, info = model.transcribe(
            audio.astype(np.float32),
            language=language or self.language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
            condition_on_previous_text=False,
            initial_prompt=self.initial_prompt,
            without_timestamps=True,
        )
        segs = list(segments)
        text = " ".join(s.text.strip() for s in segs).strip()
        no_speech = max((s.no_speech_prob for s in segs), default=1.0)
        reason = None
        if not text:
            reason = "empty"
        elif _HALLUCINATIONS.search(text):
            reason = "known_whisper_hallucination"
        elif no_speech > 0.8 and len(text) < 40:
            reason = "probably_not_speech"
        return Transcript(
            text="" if reason else text,
            language=info.language,
            language_probability=round(float(info.language_probability), 3),
            no_speech_probability=round(float(no_speech), 3),
            seconds=round(time.perf_counter() - started, 2),
            rejected_reason=reason,
        )

    def warmup(self) -> float:
        """Load the model and run one inference so the first real call is not slowed down."""
        started = time.perf_counter()
        model = self._load()
        list(model.transcribe(np.zeros(16000, dtype=np.float32), language=self.language, beam_size=1)[0])
        return round(time.perf_counter() - started, 2)


@dataclass
class Speech:
    wav: bytes
    duration_s: float
    seconds: float


class TextToSpeech:
    def __init__(self, voice_path: str, *, length_scale: float = 1.0) -> None:
        self.voice_path = voice_path
        self.length_scale = length_scale
        self._voice: Any = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._voice is not None

    def _load(self) -> Any:
        with self._lock:
            if self._voice is None:
                from piper import PiperVoice

                self._voice = PiperVoice.load(self.voice_path)
        return self._voice

    def synthesize(self, text: str, length_scale: float | None = None) -> Speech:
        """Blocking; run it in a worker thread. Returns a mono 16-bit WAV."""
        from piper.config import SynthesisConfig

        started = time.perf_counter()
        voice = self._load()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            voice.synthesize_wav(text, wf, syn_config=SynthesisConfig(length_scale=length_scale or self.length_scale))
        data = buf.getvalue()
        with wave.open(io.BytesIO(data)) as wf:
            duration = wf.getnframes() / wf.getframerate()
        return Speech(wav=data, duration_s=round(duration, 2), seconds=round(time.perf_counter() - started, 2))

    def stream(self, text: str, length_scale: float | None = None) -> Any:
        """Blocking generator: yields (int16 mono samples, sample_rate) per sentence, as soon as
        each sentence is synthesised (the first one arrives after ~0.05 s)."""
        from piper.config import SynthesisConfig

        voice = self._load()
        for chunk in voice.synthesize(text, syn_config=SynthesisConfig(length_scale=length_scale or self.length_scale)):
            yield chunk.audio_int16_array, chunk.sample_rate

    def warmup(self) -> float:
        """Load the voice and synthesise once so the first real call is not slowed down."""
        started = time.perf_counter()
        self.synthesize("Bonjour.")
        return round(time.perf_counter() - started, 2)
