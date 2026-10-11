"""End-to-end tests against the running Compose stack (`docker compose --profile e2e run --rm e2e`).

They use only read-only tools: they exercise the real network path
container -> host daemon, but never move the robot.
"""

import os

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.anyio,
    pytest.mark.skipif("MCP_E2E_URL" not in os.environ, reason="MCP_E2E_URL not set (run via compose e2e)"),
]

READ_ONLY_TOOLS = [
    "get_daemon_status", "get_motor_status", "get_full_state", "list_running_moves",
    "get_media_status", "get_volume", "get_camera_specs", "get_robot_app_lock_status",
    "get_current_app_status", "get_server_safety_config", "get_perception_status",
]


def _client() -> Client:
    headers = {}
    if os.environ.get("MCP_AUTH_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['MCP_AUTH_TOKEN']}"
    return Client(streamable_http_client(os.environ["MCP_E2E_URL"], http_client=httpx2.AsyncClient(headers=headers)))


async def test_health() -> None:
    async with httpx.AsyncClient() as http:
        r = await http.get(os.environ["MCP_E2E_HEALTH_URL"])
    assert r.status_code == 200


async def test_discovery() -> None:
    async with _client() as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert {"goto_pose", "stop_all_moves", "get_daemon_status"} <= names


async def test_connectivity_to_real_daemon() -> None:
    async with _client() as client:
        r = await client.call_tool("diagnose_connectivity", {})
    steps = {s["step"]: s for s in r.structured_content["steps"]}
    print(r.structured_content)
    assert steps["dns_resolution"]["ok"] and steps["tcp_connect"]["ok"] and steps["http_response"]["ok"]


@pytest.mark.parametrize("tool", READ_ONLY_TOOLS)
async def test_read_only_tools_against_real_daemon(tool: str) -> None:
    async with _client() as client:
        r = await client.call_tool(tool, {})
    assert not r.is_error, r.content
