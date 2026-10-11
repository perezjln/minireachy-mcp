from __future__ import annotations

import socket
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp import Client

from minireachy_mcp.client import MiniReachyClient
from minireachy_mcp.config import Settings
from minireachy_mcp.server import build_server

from .fake_daemon import FakeDaemon


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def _daemon_server() -> Iterator[FakeDaemon]:
    daemon = FakeDaemon()
    daemon.start()
    yield daemon
    daemon.stop()


@pytest.fixture
def fake(_daemon_server: FakeDaemon) -> Iterator[FakeDaemon]:
    _daemon_server.reset()
    yield _daemon_server
    _daemon_server.reset()


@pytest.fixture
def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def make_settings(fake: FakeDaemon) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        data: dict[str, Any] = {
            "minireachy_base_url": fake.base_url,
            "connect_timeout_s": 1.0,
            "read_timeout_s": 1.0,
        }
        data.update(overrides)
        return Settings(**data)

    return factory


@pytest.fixture
def mcp_session(make_settings: Callable[..., Settings]) -> Callable[..., Any]:
    """`async with mcp_session(enable_physical_actions=True) as (client, deps): ...`"""

    @asynccontextmanager
    async def factory(media: Any = None, **overrides: Any) -> AsyncIterator[tuple[Client, Any]]:
        settings = make_settings(**overrides)
        async with MiniReachyClient(settings) as daemon_client:
            server, deps = build_server(settings, daemon_client, media)
            async with Client(server) as client:
                yield client, deps

    return factory


def text_of(result: Any) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)
