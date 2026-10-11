"""A simulated daemon media server: gst-plugins-rs signalling protocol + an aiortc producer
streaming a synthetic camera (solid colour 640x480) and microphone (440 Hz tone)."""

from __future__ import annotations

import asyncio
import fractions
import json
import time
import uuid
from typing import Any

import av
import numpy as np
import websockets
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription


class ColourTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self) -> None:
        super().__init__()
        self._pts = 0

    async def recv(self) -> av.VideoFrame:
        await asyncio.sleep(1 / 30)
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        img[:, :, 0] = 200  # red-ish
        frame = av.VideoFrame.from_ndarray(img, format="rgb24")
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, 90000)
        self._pts += 3000
        return frame


class ToneTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, amplitude: float = 0.3) -> None:
        super().__init__()
        self.amplitude = amplitude
        self._n = 0
        self._start = time.monotonic()

    async def recv(self) -> av.AudioFrame:
        samples = 960  # 20 ms at 48 kHz
        target = self._start + self._n / 48000
        await asyncio.sleep(max(0, target - time.monotonic()))
        t = (np.arange(samples) + self._n) / 48000
        pcm = (self.amplitude * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
        frame.sample_rate = 48000
        frame.pts = self._n
        frame.time_base = fractions.Fraction(1, 48000)
        self._n += samples
        return frame


class FakeMediaServer:
    def __init__(self, producer_name: str = "reachymini") -> None:
        self.producer_name = producer_name
        self.producer_id = str(uuid.uuid4())
        self.port = 0
        self.sessions = 0
        self.silent = False  # negotiate normally but send no media (daemon pipeline stopped)
        self.accept_audio = True  # like the daemon: audio transceiver is sendrecv
        self.received_rms: list[float] = []  # one value per 20 ms frame received from the consumer
        self._readers: list[asyncio.Task[None]] = []
        self._server: Any = None
        self._pcs: list[RTCPeerConnection] = []

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def __aenter__(self) -> FakeMediaServer:
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        for r in self._readers:
            r.cancel()
        for pc in self._pcs:
            await pc.close()
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, ws: Any) -> None:
        await ws.send(json.dumps({"type": "welcome", "peerId": str(uuid.uuid4())}))
        pc: RTCPeerConnection | None = None
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "list":
                await ws.send(json.dumps({"type": "list", "producers": [
                    {"id": self.producer_id, "meta": {"name": self.producer_name}}]}))
            elif msg["type"] == "startSession":
                session_id = str(uuid.uuid4())
                self.sessions += 1
                await ws.send(json.dumps({"type": "sessionStarted", "peerId": msg["peerId"], "sessionId": session_id}))
                pc = RTCPeerConnection()
                self._pcs.append(pc)

                @pc.on("track")
                def on_track(track: Any) -> None:  # the consumer's voice
                    async def read() -> None:
                        while True:
                            frame = await track.recv()
                            pcm = frame.to_ndarray().astype(np.float32) / 32768
                            self.received_rms.append(float(np.sqrt(np.mean(pcm**2))))
                    self._readers.append(asyncio.create_task(read()))

                audio_direction = "sendrecv" if self.accept_audio else "sendonly"
                if self.silent:
                    pc.addTransceiver("video", direction="sendonly")
                    pc.addTransceiver("audio", direction=audio_direction)
                else:
                    pc.addTrack(ColourTrack())
                    pc.addTransceiver(ToneTrack(), direction=audio_direction)
                pc.createDataChannel("data")
                await pc.setLocalDescription(await pc.createOffer())
                await ws.send(json.dumps({"type": "peer", "sessionId": session_id,
                                          "sdp": {"type": "offer", "sdp": pc.localDescription.sdp}}))
            elif msg["type"] == "peer" and "sdp" in msg and pc is not None:
                await pc.setRemoteDescription(RTCSessionDescription(msg["sdp"]["sdp"], msg["sdp"]["type"]))
            elif msg["type"] == "endSession":
                break
