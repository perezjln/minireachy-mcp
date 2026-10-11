"""Real local models (baked into the image): Piper French TTS -> faster-whisper STT round trip."""

import io
import os
import wave

import numpy as np
import pytest

from minireachy_mcp.config import Settings
from minireachy_mcp.media.speech import _HALLUCINATIONS, SpeechToText, TextToSpeech

S = Settings()
models_present = os.path.exists(S.stt_model_path) and os.path.exists(S.tts_voice_path)
needs_models = pytest.mark.skipif(not models_present, reason="speech models not baked into this image")


def to_16k(wav: bytes) -> np.ndarray:
    import av

    with wave.open(io.BytesIO(wav)) as wf:
        rate = wf.getframerate()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).reshape(1, -1)
    frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
    frame.sample_rate = rate
    res = av.AudioResampler(format="flt", layout="mono", rate=16000)
    return np.concatenate([f.to_ndarray().reshape(-1) for f in res.resample(frame) + res.resample(None)])


@needs_models
def test_tts_then_stt_round_trip() -> None:
    tts = TextToSpeech(S.tts_voice_path)
    stt = SpeechToText(S.stt_model_path, initial_prompt=S.stt_initial_prompt)
    speech = tts.synthesize("Bonjour, il fait beau aujourd'hui.")
    assert 1.0 < speech.duration_s < 6.0 and speech.wav[:4] == b"RIFF"
    transcript = stt.transcribe(to_16k(speech.wav))
    assert transcript.rejected_reason is None
    assert "bonjour" in transcript.text.lower() and "beau" in transcript.text.lower()
    assert transcript.language == "fr"


@needs_models
def test_speed_changes_duration() -> None:
    tts = TextToSpeech(S.tts_voice_path)
    slow = tts.synthesize("Une phrase de test.", length_scale=1.5)
    fast = tts.synthesize("Une phrase de test.", length_scale=0.7)
    assert slow.duration_s > fast.duration_s


@needs_models
def test_silence_is_not_transcribed() -> None:
    stt = SpeechToText(S.stt_model_path)
    transcript = stt.transcribe(np.zeros(16000 * 2, dtype=np.float32))
    assert transcript.text == "" and transcript.rejected_reason is not None


@pytest.mark.parametrize(
    "text", ["Sous-titres réalisés par la communauté d'Amara.org", "Merci d'avoir regardé cette vidéo !"]
)
def test_known_hallucinations_filtered(text: str) -> None:
    assert _HALLUCINATIONS.search(text)
