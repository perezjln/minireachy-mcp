"""Robustness regressions: non-finite inputs, tool annotations, unexpected errors, concurrent speech."""

import asyncio
import logging
import math

import anyio
import numpy as np
import pytest

from minireachy_mcp.client import MiniReachyClient
from minireachy_mcp.config import Settings
from minireachy_mcp.errors import SafetyGateError
from minireachy_mcp.models import HeadPose
from minireachy_mcp.safety import check_motion
from minireachy_mcp.server import build_server

from .conftest import text_of
from .fake_daemon import Override
from .test_perception_tools import StubMedia, StubTTS

pytestmark = pytest.mark.anyio

NAN = float("nan")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"head_pose": None, "antennas": (NAN, 0.0), "body_yaw": None, "duration": 1.0},
        {"head_pose": None, "antennas": None, "body_yaw": NAN, "duration": 1.0},
        {"head_pose": None, "antennas": None, "body_yaw": math.inf, "duration": 1.0},
        {"head_pose": HeadPose(), "antennas": None, "body_yaw": None, "duration": NAN},
    ],
)
def test_check_motion_rejects_non_finite(kwargs) -> None:
    with pytest.raises(SafetyGateError, match="finite"):
        check_motion(Settings(), **kwargs)


async def test_non_finite_arguments_never_reach_the_daemon(fake, make_settings) -> None:
    """NaN is a valid JSON-RPC value for the SDK parser; it must not become a motion command.

    Calls the server directly: an MCP client would serialise NaN as null before sending it.
    """
    settings = make_settings(enable_physical_actions=True, enable_perception=True)
    async with MiniReachyClient(settings) as daemon_client:
        server, _ = build_server(settings, daemon_client, StubMedia())
        for name, args in (
            ("goto_pose", {"duration": 1.0, "body_yaw": NAN}),
            ("goto_pose", {"duration": 1.0, "antennas": [NAN, 0.0]}),
            ("goto_pose", {"duration": NAN, "antennas": [0.1, 0.1]}),
            ("converse", {"head_turn": [NAN, NAN], "listen_after": False, "look": "never"}),
            ("converse", {"head_turn": [math.inf, 0.0], "listen_after": False, "look": "never"}),
        ):
            with pytest.raises(Exception):  # noqa: B017 - SDK validation error type is not public API
                await server.call_tool(name, args)
    assert fake.non_get_calls() == []


async def test_turn_toward_sound_ignores_non_finite_doa(fake, mcp_session) -> None:
    fake.overrides["GET /api/state/doa"] = Override(raw=b'{"angle": NaN, "speech_detected": true}')
    async with mcp_session(enable_physical_actions=True) as (client, _):
        r = await client.call_tool("turn_toward_sound", {})
    assert not r.is_error
    assert r.structured_content["turned"] is False
    assert fake.non_get_calls() == []


async def test_converse_is_not_advertised_as_read_only(mcp_session) -> None:
    async with mcp_session() as (client, _):
        tools = {t.name: t for t in (await client.list_tools()).tools}
    hints = tools["converse"].annotations
    assert hints.read_only_hint is False
    assert hints.destructive_hint is True


async def test_unexpected_error_is_reported_and_logged(fake, mcp_session, caplog) -> None:
    caplog.set_level(logging.INFO, logger="minireachy_mcp")
    fake.overrides["GET /api/move/running"] = Override(body={"unexpected": True})
    async with mcp_session() as (client, _):
        r = await client.call_tool("list_running_moves", {})
    assert r.is_error
    assert "[internal_error]" in text_of(r)
    assert "AttributeError" not in text_of(r)  # internals stay in the server log
    assert any("tool=list_running_moves" in m and "outcome=internal_error" in m for m in caplog.messages)


class _TaggedTTS(StubTTS):
    """Three sentences per call, each filled with a tag identifying the call, produced slowly."""

    def stream(self, text, length_scale=None):
        import time

        for _ in range(3):
            time.sleep(0.03)
            yield np.full(960, 1 if text.startswith("A") else 2, dtype=np.int16), 48_000


class _OrderSpeaker:
    busy = False

    def __init__(self) -> None:
        self.order: list[int] = []

    def enqueue(self, pcm48) -> None:
        self.order.append(int(pcm48[0]))

    def clear(self) -> None:
        pass

    async def drained(self) -> None:
        await asyncio.sleep(0.01)


async def test_concurrent_speech_is_not_interleaved(fake, mcp_session) -> None:
    media = StubMedia()
    media.tts = _TaggedTTS()
    media.link.speaker = _OrderSpeaker()
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        async with anyio.create_task_group() as tg:
            tg.start_soon(client.call_tool, "speak", {"text": "AAA"})
            tg.start_soon(client.call_tool, "speak", {"text": "BBB"})
    assert media.link.speaker.order in ([1, 1, 1, 2, 2, 2], [2, 2, 2, 1, 1, 1])


async def test_perception_tools_hold_the_media_link_while_working(fake, mcp_session) -> None:
    """listen / speak / capture_image keep the stream from going idle, and release it afterwards."""
    media = StubMedia()
    media.link.speech_after_s = 0.2
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        for name, args in (("speak", {"text": "Bonjour"}), ("capture_image", {}), ("listen", {"max_wait_s": 3})):
            r = await client.call_tool(name, args)
            assert not r.is_error, (name, text_of(r))
            assert media.link.max_leases >= 1 and media.link.leases == 0, name
            media.link.max_leases = 0
