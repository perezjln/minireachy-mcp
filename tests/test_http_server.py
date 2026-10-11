"""The real Streamable HTTP server (uvicorn on 127.0.0.1): start, discovery, auth, health, shutdown."""

import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from minireachy_mcp.client import MiniReachyClient
from minireachy_mcp.config import Settings
from minireachy_mcp.server import build_http_app, build_server

pytestmark = pytest.mark.anyio

TOKEN = "t" * 32


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def running_server(make_settings: Callable[..., Settings]) -> Callable[..., Any]:
    @asynccontextmanager
    async def factory(**overrides: Any) -> AsyncIterator[tuple[str, MiniReachyClient]]:
        settings = make_settings(mcp_host="127.0.0.1", mcp_port=_free_port(), **overrides)
        daemon_client = MiniReachyClient(settings)
        server, _ = build_server(settings, daemon_client)
        app = build_http_app(settings, server)
        uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=settings.mcp_port, log_level="warning"))
        async with anyio.create_task_group() as tg:
            tg.start_soon(uv.serve)
            with anyio.fail_after(10):
                while not uv.started:
                    await anyio.sleep(0.02)
            try:
                yield f"http://127.0.0.1:{settings.mcp_port}", daemon_client
            finally:
                uv.should_exit = True  # same path as SIGTERM handling
        await daemon_client.aclose()

    return factory


async def test_streamable_http_discovery_and_call(fake, running_server) -> None:
    async with running_server() as (base, _):
        async with Client(f"{base}/mcp") as client:
            tools = await client.list_tools()
            result = await client.call_tool("get_motor_status", {})
    assert len(tools.tools) >= 50
    assert result.structured_content == {"mode": "enabled"}


async def test_health_does_not_depend_on_daemon(closed_port, running_server) -> None:
    async with running_server(minireachy_base_url=f"http://127.0.0.1:{closed_port}", get_retries=0) as (base, _):
        async with httpx.AsyncClient() as http:
            live = await http.get(f"{base}/healthz")
            daemon = await http.get(f"{base}/healthz/daemon")
    assert live.status_code == 200 and live.json()["status"] == "ok"
    assert daemon.status_code == 503 and daemon.json()["daemon_reachable"] is False


async def test_bearer_token_required_when_configured(fake, running_server) -> None:
    async with running_server(mcp_auth_token=TOKEN) as (base, _):
        async with httpx.AsyncClient() as http:
            assert (await http.get(f"{base}/healthz")).status_code == 200
            denied = await http.post(f"{base}/mcp", json={})
            wrong = await http.post(f"{base}/mcp", json={}, headers={"Authorization": "Bearer nope"})
        assert denied.status_code == 401 and wrong.status_code == 401
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as authed:
            async with Client(streamable_http_client(f"{base}/mcp", http_client=authed)) as client:
                assert (await client.call_tool("get_motor_status", {})).structured_content == {"mode": "enabled"}


async def test_dns_rebinding_protection_rejects_foreign_host(fake, running_server) -> None:
    async with running_server() as (base, _):
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{base}/mcp",
                headers={"Host": "evil.example:8765", "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
    assert r.status_code in (400, 403, 421)


async def test_startup_and_shutdown_send_nothing_to_robot(fake, running_server) -> None:
    async with running_server() as (base, daemon_client):
        async with httpx.AsyncClient() as http:
            assert (await http.get(f"{base}/healthz")).status_code == 200
    assert fake.requests == []
    assert daemon_client._http.is_closed


def test_process_exits_cleanly_on_sigterm(fake) -> None:
    """`python -m minireachy_mcp` as Docker runs it: SIGTERM -> graceful stop, exit code 0, no robot call."""
    import os
    import signal
    import subprocess
    import sys
    import time
    import urllib.request

    port = _free_port()
    env = {**os.environ, "MINIREACHY_BASE_URL": fake.base_url, "MCP_HOST": "127.0.0.1", "MCP_PORT": str(port)}
    proc = subprocess.Popen([sys.executable, "-m", "minireachy_mcp"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                if urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1).status == 200:
                    break
            except OSError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise
                time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out
    assert "minireachy-mcp stopped" in out
    assert fake.requests == []
