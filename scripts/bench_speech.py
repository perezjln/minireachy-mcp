"""Benchmark local STT/TTS settings inside the image (no robot, no network).

    docker compose --profile test run --rm -v ./scripts:/app/scripts:ro tests python scripts/bench_speech.py
"""

import io
import os
import time
import wave

import av
import numpy as np
from faster_whisper import WhisperModel

from minireachy_mcp.config import Settings
from minireachy_mcp.media.speech import TextToSpeech

S = Settings()
tts = TextToSpeech(S.tts_voice_path)

PHRASES = {
    "short (~2s)": "Bonjour Reachy, comment ça va ?",
    "medium (~5s)": "Je voudrais que tu me dises quelle est la couleur de mon t-shirt, s'il te plaît.",
    "long (~9s)": "Hier soir, je suis allé au cinéma avec des amis pour voir un film de science-fiction, "
    "et j'ai trouvé l'histoire vraiment passionnante.",
}


def to_16k(wav: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav)) as wf:
        rate = wf.getframerate()
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).reshape(1, -1)
    frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
    frame.sample_rate = rate
    res = av.AudioResampler(format="flt", layout="mono", rate=16000)
    return np.concatenate([f.to_ndarray().reshape(-1) for f in res.resample(frame) + res.resample(None)])


t = time.perf_counter()
tts.synthesize("Chauffe.")
print(f"TTS chargement + 1re phrase : {time.perf_counter() - t:.2f}s")
audios = {}
for name, text in PHRASES.items():
    sp = tts.synthesize(text)
    audios[name] = to_16k(sp.wav)
    print(f"TTS {name}: synthesis {sp.seconds:.2f}s for {sp.duration_s:.2f}s of audio")

print(f"\nCPU visibles : {os.cpu_count()}")
configs = [
    ("small int8 threads=auto vad=on ", dict(cpu_threads=0), dict(vad_filter=True)),
    ("small int8 threads=8   vad=on ", dict(cpu_threads=8), dict(vad_filter=True)),
    ("small int8 threads=8   vad=off", dict(cpu_threads=8), dict(vad_filter=False)),
    ("small int8 threads=16  vad=off notimestamps", dict(cpu_threads=16),
     dict(vad_filter=False, without_timestamps=True)),
]
for label, load_kw, kw in configs:
    t = time.perf_counter()
    model = WhisperModel(S.stt_model_path, device="cpu", compute_type="int8", **load_kw)
    load = time.perf_counter() - t
    first = True
    line = []
    for name, audio in audios.items():
        t = time.perf_counter()
        segs, _ = model.transcribe(audio, language="fr", beam_size=1, condition_on_previous_text=False, **kw)
        text = " ".join(s.text.strip() for s in segs)
        dt = time.perf_counter() - t
        if first:  # second run on the same clip, warm
            t = time.perf_counter()
            segs, _ = model.transcribe(audio, language="fr", beam_size=1, condition_on_previous_text=False, **kw)
            list(segs)
            line.append(f"{name}: {dt:.2f}s (cold) / {time.perf_counter() - t:.2f}s")
            first = False
        else:
            line.append(f"{name}: {dt:.2f}s")
    print(f"{label} | chargement {load:.2f}s | " + " | ".join(line))
    print(f"   -> « {text} »")
    del model
