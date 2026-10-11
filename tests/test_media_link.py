"""WebRTC consumer against a simulated daemon media server (real WebRTC over loopback)."""

import numpy as np
import pytest

from minireachy_mcp.media.webrtc import MediaLink, MediaUnavailableError

from .fake_webrtc import FakeMediaServer

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def loopback_ice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test container has no network: let ICE use the loopback interface."""
    import aioice.ice

    monkeypatch.setattr(aioice.ice, "get_host_addresses", lambda use_ipv4, use_ipv6: ["127.0.0.1"])


async def test_receives_video_and_audio() -> None:
    async with FakeMediaServer() as media:
        link = MediaLink(media.url, connect_timeout_s=20)
        try:
            frame, age = await link.latest_frame()
            assert (frame.width, frame.height) == (640, 480)
            assert age < 2
            rgb = frame.to_ndarray(format="rgb24")
            assert rgb[..., 0].mean() > 150 and rgb[..., 1].mean() < 60  # red survived VP8 encoding
            start = link.audio.total
            import asyncio

            await asyncio.sleep(1.0)
            audio = link.audio.read(start)
            assert len(audio) > 8000  # ~1 s at 16 kHz
            assert 0.1 < float(np.sqrt(np.mean(audio**2))) < 0.4  # 0.3-amplitude tone, RMS ≈ 0.21
            status = link.status()
            assert status["state"] == "webrtc:connected" and status["video_frames"] > 0
            assert media.sessions == 1
        finally:
            await link.close()


async def test_missing_producer_is_reported() -> None:
    async with FakeMediaServer(producer_name="other") as media:
        link = MediaLink(media.url, connect_timeout_s=2)
        try:
            with pytest.raises(MediaUnavailableError) as info:
                await link.ensure_connected()
            assert "reachymini" in str(info.value) or "No media" in str(info.value)
            assert "not announced" in (link.stats.last_error or "")
        finally:
            await link.close()


async def test_unreachable_signalling(closed_port) -> None:
    link = MediaLink(f"ws://127.0.0.1:{closed_port}", connect_timeout_s=1.5)
    try:
        with pytest.raises(MediaUnavailableError, match="No media"):
            await link.ensure_connected()
    finally:
        await link.close()


def test_sdp_candidate_filter() -> None:
    import ipaddress

    from minireachy_mcp.media.webrtc import filter_sdp_candidates

    sdp = "\r\n".join([
        "v=0",
        "a=candidate:1 1 UDP 2015363327 169.254.123.25 53085 typ host",
        "a=candidate:2 1 UDP 2015363327 192.168.1.78 53080 typ host",
        "a=candidate:3 1 UDP 1679819007 88.160.102.54 25309 typ srflx raddr 0.0.0.0 rport 0",
        "a=candidate:4 1 UDP 2015363327 fe80::1 53090 typ host",
    ]) + "\r\n"
    out, dropped = filter_sdp_candidates(sdp, [ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fe80::/10")])
    assert dropped == 2 and "169.254" not in out and "fe80" not in out and "192.168.1.78" in out
    assert filter_sdp_candidates(sdp, []) == (sdp, 0)


async def test_session_without_media_is_detected_and_retried() -> None:
    """A producer that negotiates but never sends media must not leave the link 'connected' forever."""
    async with FakeMediaServer() as media:
        media.silent = True
        link = MediaLink(media.url, connect_timeout_s=4, first_media_timeout_s=1.0)
        try:
            with pytest.raises(MediaUnavailableError):
                await link.ensure_connected()
            assert "no media" in (link.stats.last_error or "")
            assert media.sessions >= 2  # it gave up on the silent session and opened a new one
        finally:
            await link.close()


async def test_voice_is_streamed_to_the_daemon() -> None:
    import asyncio

    from minireachy_mcp.media.speaker import RATE

    async with FakeMediaServer() as media:
        link = MediaLink(media.url, connect_timeout_s=20)
        try:
            await link.ensure_connected()
            speaker = link.speaker
            assert speaker is not None and link.status()["speaker_ready"] is True
            t = np.arange(int(0.6 * RATE)) / RATE
            speaker.enqueue((0.3 * np.sin(2 * np.pi * 300 * t) * 32767).astype(np.int16))
            assert speaker.busy
            await asyncio.wait_for(speaker.drained(), 3)
            await asyncio.sleep(0.5)
            loud = [r for r in media.received_rms if r > 0.1]
            assert 20 <= len(loud) <= 40  # ~0.6 s of 20 ms frames reached the "daemon"
            assert len(media.received_rms) > len(loud)  # silence before/after
        finally:
            await link.close()


async def test_no_speaker_when_daemon_refuses_audio() -> None:
    async with FakeMediaServer() as media:
        media.accept_audio = False
        link = MediaLink(media.url, connect_timeout_s=20)
        try:
            await link.ensure_connected()
            assert link.speaker is None and link.status()["speaker_ready"] is False
        finally:
            await link.close()


async def test_blocked_event_loop_is_not_mistaken_for_a_dead_stream() -> None:
    """A CPU-heavy caller blocking the loop for longer than the stall timeout must not reset the session."""
    import asyncio
    import time as _time

    async with FakeMediaServer() as media:
        link = MediaLink(media.url, connect_timeout_s=20, stall_timeout_s=2.0)
        try:
            await link.ensure_connected()
            _time.sleep(3.0)  # blocks the event loop (like a transcription run in the loop thread)
            await asyncio.sleep(3.0)
            assert media.sessions == 1, link.stats.last_error
            frame, age = await link.latest_frame()
            assert age < 1.0
        finally:
            await link.close()
