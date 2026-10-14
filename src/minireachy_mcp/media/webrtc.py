"""WebRTC consumer for the daemon's GStreamer `webrtcsink` media server.

The daemon streams the robot camera (video) and microphone array (audio) through
`webrtcsink` with its built-in signalling server (ws://<host>:8443). This module
joins that stream as a regular consumer — the same channel used by the official
remote SDK and browsers — so the container never touches USB devices.

Signalling protocol (gst-plugins-rs, JSON, field names camelCase):
    <- {"type": "welcome", "peerId": ...}
    -> {"type": "list"}                         <- {"type": "list", "producers": [{"id", "meta"}]}
    -> {"type": "startSession", "peerId": producer}
    <- {"type": "sessionStarted", "peerId", "sessionId"}
    <- {"type": "peer", "sessionId", "sdp": {"type": "offer", "sdp"}}
    -> {"type": "peer", "sessionId", "sdp": {"type": "answer", "sdp"}}
    <> {"type": "peer", "sessionId", "ice": {"candidate", "sdpMLineIndex"}}
    <- {"type": "endSession", "sessionId"}
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .audio import AudioRing
from .speaker import create_speaker_track

logger = logging.getLogger("minireachy_mcp.media.webrtc")


class MediaUnavailableError(Exception):
    """The robot media stream could not be obtained."""


class _IdleDisconnect(Exception):
    """Internal: the stream was unused for `idle_disconnect_s` and is closed until needed again."""


@dataclass
class LinkStats:
    state: str = "disconnected"
    producer_id: str | None = None
    session_id: str | None = None
    connected_at: float | None = None
    video_frames: int = 0
    last_video_at: float | None = None
    video_size: tuple[int, int] | None = None
    audio_frames: int = 0
    last_audio_at: float | None = None
    last_error: str | None = None
    connects: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


# Optional ICE candidate filtering (e.g. ("169.254.0.0/16",)). Empty by default: on Docker
# Desktop for Windows the only working path from the container to the daemon goes through
# the host's link-local virtual adapter (measured 2026-10-11: filtering it prevents ICE).
DEFAULT_IGNORED_NETWORKS: tuple[str, ...] = ()


def _candidate_address(candidate_line: str) -> str | None:
    # "candidate:<foundation> <component> <transport> <priority> <address> <port> typ <type> ..."
    parts = candidate_line.split()
    return parts[4] if len(parts) > 5 else None


def _is_ignored(address: str | None, networks: list[Any]) -> bool:
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False  # mDNS hostnames etc.: keep
    return any(ip.version == net.version and ip in net for net in networks)


def filter_sdp_candidates(sdp: str, networks: list[Any]) -> tuple[str, int]:
    """Drop `a=candidate` lines whose address is in one of `networks`."""
    kept, dropped = [], 0
    for line in sdp.splitlines():
        if line.startswith("a=candidate:") and _is_ignored(_candidate_address(line[2:]), networks):
            dropped += 1
            continue
        kept.append(line)
    return "\r\n".join(kept) + "\r\n", dropped


def _selected_pair(pc: Any) -> str | None:
    """Best-effort description of the ICE candidate pair carrying media (aioice internals)."""
    try:
        for transceiver in pc.getTransceivers():
            conn = transceiver.receiver.transport.transport._connection  # RTCIceTransport -> aioice
            pair = conn._nominated.get(1)
            if pair is not None:
                return f"{pair.local_candidate.host}:{pair.local_candidate.port} -> "                        f"{pair.remote_candidate.host}:{pair.remote_candidate.port} ({pair.remote_candidate.type})"
    except Exception:
        return None
    return None


class MediaLink:
    """Maintains one WebRTC session with the daemon, on demand, with reconnection."""

    def __init__(
        self,
        signalling_url: str,
        *,
        producer_name: str = "reachymini",
        ring_seconds: float = 60.0,
        connect_timeout_s: float = 15.0,
        first_media_timeout_s: float = 8.0,
        stall_timeout_s: float = 5.0,
        ignored_networks: tuple[str, ...] = DEFAULT_IGNORED_NETWORKS,
        idle_disconnect_s: float = 0.0,
    ) -> None:
        self.signalling_url = signalling_url
        # 0 = never: otherwise the camera and microphones are only streamed (and decoded, and
        # buffered in memory) while something uses them, plus this grace period.
        self.idle_disconnect_s = idle_disconnect_s
        self.producer_name = producer_name
        self.connect_timeout_s = connect_timeout_s
        self.first_media_timeout_s = first_media_timeout_s
        self.stall_timeout_s = stall_timeout_s
        self.ignored_networks = [ipaddress.ip_network(n) for n in ignored_networks]
        self.audio = AudioRing(seconds=ring_seconds)
        self.stats = LinkStats()
        self._latest_frame: Any = None  # av.VideoFrame
        # Outgoing voice track of the current session, set only once the daemon accepted
        # to receive audio from us (bidirectional audio transceiver) and media flows.
        self.speaker: Any = None
        self._pending_speaker: Any = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._closing = False
        self._wanted = asyncio.Event()  # set by callers; cleared when the link goes idle
        self._leases = 0  # callers currently relying on the stream (listening, speaking, looking)
        self._last_used = time.monotonic()

    # -- public API ---------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def use(self) -> AsyncIterator[None]:
        """Keep the stream open for the duration of the block (no idle disconnection)."""
        self._leases += 1
        self._last_used = time.monotonic()
        try:
            yield
        finally:
            self._leases -= 1
            self._last_used = time.monotonic()

    def _idle(self) -> bool:
        if not self.idle_disconnect_s or self._leases or (self.speaker is not None and self.speaker.busy):
            return False
        return time.monotonic() - self._last_used > self.idle_disconnect_s

    async def ensure_connected(self, timeout_s: float | None = None) -> None:
        """Start the session if needed and wait until media is flowing."""
        self._last_used = time.monotonic()
        async with self._lock:
            if self._task is None or self._task.done():
                self._ready.clear()
                self._task = asyncio.create_task(self._run(), name="minireachy-webrtc")
            self._wanted.set()  # wakes up a link that went idle
        try:
            await asyncio.wait_for(self._ready.wait(), timeout_s or self.connect_timeout_s)
        except TimeoutError:
            raise MediaUnavailableError(
                f"No media from the robot after {timeout_s or self.connect_timeout_s:.0f}s "
                f"(signalling {self.signalling_url}, state={self.stats.state}, "
                f"last_error={self.stats.last_error}). Check that the daemon runs and media is not released."
            ) from None

    async def latest_frame(self, max_age_s: float = 2.0) -> tuple[Any, float]:
        """Return (av.VideoFrame, age_seconds) of the most recent camera frame."""
        await self.ensure_connected()
        deadline = time.monotonic() + self.connect_timeout_s
        while True:
            frame, at = self._latest_frame, self.stats.last_video_at
            if frame is not None and at is not None and time.monotonic() - at <= max_age_s:
                return frame, time.monotonic() - at
            if time.monotonic() > deadline:
                raise MediaUnavailableError("No recent camera frame (the video track may be missing or stalled).")
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self.stats.state = "closed"

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        s = self.stats
        return {
            "state": s.state,
            "signalling_url": self.signalling_url,
            "producer_id": s.producer_id,
            "video_frames": s.video_frames,
            "video_size": list(s.video_size) if s.video_size else None,
            "last_video_age_s": round(now - s.last_video_at, 2) if s.last_video_at else None,
            "audio_frames": s.audio_frames,
            "last_audio_age_s": round(now - s.last_audio_at, 2) if s.last_audio_at else None,
            "audio_level_rms_1s": round(float(self.audio.rms_last(1.0)), 4),
            "connects": s.connects,
            "sessions": s.extra.get("sessions", 0),
            "speaker_ready": self.speaker is not None,
            "speaker_frames_sent": getattr(self.speaker, "frames_sent", 0),
            "idle_disconnect_s": self.idle_disconnect_s or None,
            "unused_for_s": round(now - self._last_used, 1),
            "in_use": self._leases,
            "selected_ice_pair": s.extra.get("ice_pair"),
            "ignored_candidates": s.extra.get("ignored_candidates", 0),
            "last_error": s.last_error,
        }

    # -- session loop -------------------------------------------------------------------

    async def _run(self) -> None:
        backoff = 1.0
        while not self._closing:
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except _IdleDisconnect:
                self.stats.state = "idle_disconnected"
                self._ready.clear()
                self.audio.wipe()  # do not keep the last minute of microphone audio while unused
                logger.info("webrtc stream closed after %.0fs without use", self.idle_disconnect_s)
                # _wanted was cleared when idleness was detected: a caller arriving since then
                # has set it again, so this returns at once instead of losing the wake-up.
                await self._wanted.wait()
                backoff = 1.0
                continue
            except Exception as exc:  # keep the link alive across daemon restarts
                self.stats.last_error = f"{type(exc).__name__}: {exc}"[:300]
                logger.warning("webrtc session ended: %s", self.stats.last_error)
            self.stats.state = "reconnecting"
            self._ready.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)

    async def _session(self) -> None:
        import websockets
        from aiortc import RTCPeerConnection, RTCSessionDescription
        from aiortc.sdp import candidate_from_sdp

        self.stats.state = "signalling"
        async with websockets.connect(self.signalling_url, open_timeout=5, max_size=2**22) as ws:
            welcome = json.loads(await ws.recv())
            if welcome.get("type") != "welcome":
                raise MediaUnavailableError(f"unexpected signalling greeting: {welcome.get('type')}")
            await ws.send(json.dumps({"type": "list"}))
            producer_id = None
            while producer_id is None:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if msg.get("type") == "list":
                    for p in msg.get("producers", []):
                        if (p.get("meta") or {}).get("name") == self.producer_name:
                            producer_id = p["id"]
                    if producer_id is None:
                        raise MediaUnavailableError(f"producer {self.producer_name!r} not announced by the daemon")
            self.stats.producer_id = producer_id
            await ws.send(json.dumps({"type": "startSession", "peerId": producer_id}))

            pc = RTCPeerConnection()
            session_id: str | None = None
            pending_ice: list[dict[str, Any]] = []
            pending_speaker: list[Any] = []
            remote_set = False
            failed = asyncio.Event()
            readers: list[asyncio.Task[None]] = []

            @pc.on("track")
            def on_track(track: Any) -> None:
                logger.info("webrtc track received: %s", track.kind)
                reader = self._read_video(track) if track.kind == "video" else self._read_audio(track)
                readers.append(asyncio.create_task(reader))

            @pc.on("connectionstatechange")
            async def on_state() -> None:
                self.stats.state = f"webrtc:{pc.connectionState}"
                if pc.connectionState in ("failed", "closed"):
                    failed.set()

            async def add_ice(ice: dict[str, Any]) -> None:
                cand = ice.get("candidate") or ""
                if not cand:
                    return
                line = cand if cand.startswith("candidate:") else f"candidate:{cand}"
                if _is_ignored(_candidate_address(line), self.ignored_networks):
                    self.stats.extra["ignored_candidates"] = self.stats.extra.get("ignored_candidates", 0) + 1
                    return
                c = candidate_from_sdp(cand.split(":", 1)[1] if cand.startswith("candidate:") else cand)
                idx = int(ice.get("sdpMLineIndex") or 0)
                c.sdpMLineIndex = idx
                transceivers = pc.getTransceivers()
                c.sdpMid = transceivers[idx].mid if idx < len(transceivers) else str(idx)
                await pc.addIceCandidate(c)

            try:
                async def pump() -> None:
                    nonlocal session_id, remote_set
                    async for raw in ws:
                        msg = json.loads(raw)
                        kind = msg.get("type")
                        if kind == "sessionStarted":
                            session_id = msg.get("sessionId")
                            self.stats.session_id = session_id
                        elif kind == "peer":
                            if "sdp" in msg and msg["sdp"].get("type") == "offer":
                                offer, dropped = filter_sdp_candidates(msg["sdp"]["sdp"], self.ignored_networks)
                                self.stats.extra["ignored_candidates"] = dropped
                                await pc.setRemoteDescription(RTCSessionDescription(offer, "offer"))
                                for transceiver in pc.getTransceivers():
                                    # aiortc stores the offered direction from OUR point of view:
                                    # "sendrecv"/"sendonly" means the daemon is willing to receive.
                                    if transceiver.kind == "audio" and transceiver._offerDirection in (
                                        "sendrecv", "sendonly"
                                    ):
                                        # The daemon accepts audio from consumers: send our voice on it.
                                        speaker_track = create_speaker_track()
                                        transceiver.sender.replaceTrack(speaker_track)
                                        transceiver.direction = "sendrecv"
                                        pending_speaker.append(speaker_track)
                                        self._pending_speaker = speaker_track
                                        break
                                remote_set = True
                                for ice in pending_ice:
                                    await add_ice(ice)
                                pending_ice.clear()
                                answer = await pc.createAnswer()
                                await pc.setLocalDescription(answer)
                                await ws.send(json.dumps({
                                    "type": "peer", "sessionId": session_id,
                                    "sdp": {"type": "answer", "sdp": pc.localDescription.sdp},
                                }))
                            elif "ice" in msg:
                                if remote_set:
                                    await add_ice(msg["ice"])
                                else:
                                    pending_ice.append(msg["ice"])
                        elif kind == "endSession":
                            raise MediaUnavailableError("the daemon ended the media session")
                        elif kind == "error":
                            raise MediaUnavailableError(f"signalling error: {msg.get('details')}")
                    raise MediaUnavailableError("signalling connection closed")

                async def watchdog() -> None:
                    """A session can be ICE-'connected' yet carry no media (bad candidate pair):
                    give up on it so _run() opens a fresh one."""
                    session_start = time.monotonic()
                    first_media = False
                    last_tick = time.monotonic()
                    while True:
                        await asyncio.sleep(0.5)
                        now = time.monotonic()
                        if now - last_tick > 1.5:
                            # The event loop itself was blocked (CPU-heavy caller): frames could not be
                            # processed either. Not a media failure; restart the measurement window.
                            session_start = max(session_start, now - 0.5)
                            last_tick = now
                            await asyncio.sleep(1.0)  # let the backlog of packets be processed
                            continue
                        last_tick = now
                        if self._idle():
                            self._wanted.clear()
                            raise _IdleDisconnect
                        last = max(self.stats.last_video_at or 0.0, self.stats.last_audio_at or 0.0)
                        if not first_media:
                            if last >= session_start:
                                first_media = True
                                if pending_speaker:
                                    self.speaker = pending_speaker[0]
                                self.stats.extra["ice_pair"] = _selected_pair(pc)
                                logger.info("webrtc media flowing, ice pair %s", self.stats.extra["ice_pair"])
                            elif time.monotonic() - session_start > self.first_media_timeout_s:
                                raise MediaUnavailableError(
                                    f"session connected but no media after {self.first_media_timeout_s:.0f}s "
                                    f"(ice pair {_selected_pair(pc)}). If this persists, the daemon's media "
                                    "pipeline has probably stopped (its log then shows 'Internal data stream "
                                    "error'): restart Reachy Mini Control."
                                )
                        elif time.monotonic() - last > self.stall_timeout_s:
                            raise MediaUnavailableError(f"media stalled for more than {self.stall_timeout_s:.0f}s")

                self.stats.extra["sessions"] = self.stats.extra.get("sessions", 0) + 1
                pump_task = asyncio.create_task(pump())
                failed_task = asyncio.create_task(failed.wait())
                watchdog_task = asyncio.create_task(watchdog())
                tasks = (pump_task, watchdog_task, failed_task)
                try:
                    await asyncio.wait(set(tasks), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for t in tasks:
                        t.cancel()
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                for error in results[:2]:
                    if isinstance(error, BaseException) and not isinstance(error, asyncio.CancelledError):
                        raise error
                raise MediaUnavailableError(f"webrtc connection {pc.connectionState}")
            finally:
                if self.speaker is not None:
                    self.speaker.clear()  # wakes up any speech waiting for playback
                self.speaker = None
                self._pending_speaker = None
                for r in readers:
                    r.cancel()
                if session_id:
                    with contextlib.suppress(Exception):
                        await ws.send(json.dumps({"type": "endSession", "sessionId": session_id}))
                await pc.close()
                self._latest_frame = None

    def _media_started(self) -> None:
        if self.speaker is None and self._pending_speaker is not None:
            self.speaker = self._pending_speaker
        self._ready.set()

    async def _read_video(self, track: Any) -> None:
        self.stats.connects += 1
        self.stats.connected_at = time.monotonic()
        while True:
            frame = await track.recv()
            self._latest_frame = frame
            self.stats.video_frames += 1
            self.stats.last_video_at = time.monotonic()
            self.stats.video_size = (frame.width, frame.height)
            self._media_started()

    async def _read_audio(self, track: Any) -> None:
        import av

        resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
        while True:
            frame = await track.recv()
            for out in resampler.resample(frame):
                self.audio.append(np.asarray(out.to_ndarray(), dtype=np.float32).reshape(-1))
            self.stats.audio_frames += 1
            self.stats.last_audio_at = time.monotonic()
            self._media_started()
