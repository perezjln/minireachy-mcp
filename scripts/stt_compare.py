"""Compare speech-to-text settings on the REAL robot microphone, same utterances for every setting.

    # 1. download the candidate models into a cache volume (no robot needed)
    docker run --rm --user root -e HF_HOME=/cache -v minireachy-stt-bench:/cache \
        -v ./scripts:/app/scripts:ro minireachy-mcp:0.2.0-test python scripts/stt_compare.py download
    # 2. listening session: Reachy prompts "Phrase N" out loud, you say the expected phrase
    docker run --rm --user root -e HF_HOME=/cache -v minireachy-stt-bench:/cache \
        -v ./scripts:/app/scripts:ro -v ./src:/app/src:ro minireachy-mcp:0.2.0-test python scripts/stt_compare.py run

Opens its own WebRTC session with the daemon (a second consumer, the MCP server is untouched).
Audio stays in memory; nothing is written to disk.
"""

import asyncio
import re
import sys
import time
import unicodedata

import numpy as np
from faster_whisper import WhisperModel

from minireachy_mcp.config import Settings
from minireachy_mcp.media.audio import capture_utterance
from minireachy_mcp.media.speaker import resample_int16
from minireachy_mcp.media.speech import TextToSpeech
from minireachy_mcp.media.webrtc import MediaLink

S = Settings()
PHRASES = [
    "Je t'entends bien.",
    "Oui.",
    "Non merci.",
    "Vas-y, continue.",
    "Tu peux répéter s'il te plaît ?",
    "Qu'est-ce que tu vois sur ma gauche ?",
]
MODELS = {"small": S.stt_model_path, "medium": "medium", "turbo": "large-v3-turbo"}
CONFIGS = [("small", 1), ("small", 5), ("medium", 1), ("turbo", 1), ("turbo", 5)]


def norm(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.replace("'", " ")).split())


def load(name: str) -> WhisperModel:
    return WhisperModel(MODELS[name], device="cpu", compute_type="int8", cpu_threads=S.stt_cpu_threads,
                        download_root="/cache/models" if name != "small" else None)


def transcribe(model: WhisperModel, audio: np.ndarray, beam: int) -> str:
    segs, _ = model.transcribe(audio, language="fr", beam_size=beam, vad_filter=False, without_timestamps=True,
                               condition_on_previous_text=False, initial_prompt=S.stt_initial_prompt)
    return " ".join(s.text.strip() for s in segs).strip()


async def say(link: MediaLink, tts: TextToSpeech, text: str) -> None:
    for pcm, rate in tts.stream(text):
        link.speaker.enqueue(resample_int16(pcm, rate))
    await link.speaker.drained()
    await asyncio.sleep(0.4)


async def run() -> None:
    t = time.perf_counter()
    models = {name: load(name) for name in MODELS}
    warm = np.zeros(16000, dtype=np.float32)
    for name, model in models.items():
        transcribe(model, warm, 1)
    print(f"models loaded in {time.perf_counter() - t:.1f}s", flush=True)

    tts = TextToSpeech(S.tts_voice_path)
    link = MediaLink(S.media_signalling_url, connect_timeout_s=25)
    await link.ensure_connected()
    if link.speaker is None:
        raise SystemExit("voice stream is not available")
    await say(link, tts, "Test de reconnaissance vocale. Après chaque numéro, dis la phrase prévue.")

    rows = []
    for i, expected in enumerate(PHRASES, 1):
        await say(link, tts, f"Phrase {i}.")
        utt = await capture_utterance(link.audio, max_wait_s=15, max_utterance_s=10, silence_ms=800)
        if utt is None:
            print(f"#{i} rien entendu", flush=True)
            rows.append((i, expected, None))
            continue
        results = []
        for name, beam in CONFIGS:
            t = time.perf_counter()
            # in a worker thread, like the MCP server: never block the event loop (WebRTC keeps flowing)
            text = await asyncio.to_thread(transcribe, models[name], utt.audio, beam)
            results.append((f"{name}-b{beam}", text, time.perf_counter() - t, norm(text) == norm(expected)))
        rows.append((i, expected, (utt.duration_s, results)))
        print(f"#{i} ({utt.duration_s:.1f}s) attendu « {expected} »", flush=True)
        for label, text, dt, ok in results:
            print(f"     {label:9} {dt:5.2f}s {'OK ' if ok else '   '} « {text} »", flush=True)
    await say(link, tts, "Merci, le test est terminé.")
    await link.close()

    print("\nSUMMARY (exact sentences / mean time)")
    for k, (name, beam) in enumerate(CONFIGS):
        done = [r[2][1][k] for r in rows if r[2]]
        if done:
            exact = sum(1 for d in done if d[3])
            print(f"  {name}-b{beam:<3} {exact}/{len(done)} exactes, {np.mean([d[2] for d in done]):.2f}s en moyenne")


def download() -> None:
    for name in ("medium", "turbo"):
        t = time.perf_counter()
        load(name)
        print(f"{name} ready in {time.perf_counter() - t:.0f}s", flush=True)


if __name__ == "__main__":
    {"download": download, "run": lambda: asyncio.run(run())}[sys.argv[1]]()
