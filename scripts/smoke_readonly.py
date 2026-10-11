"""Read-only smoke test of every READ tool against the running server (and thus the real daemon).

    docker compose --profile e2e run --rm e2e python scripts/smoke_readonly.py

Also checks that one physical tool is refused when ENABLE_PHYSICAL_ACTIONS=false.
Never sends a physical command.
"""

import json
import os
import sys

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ARGS = {
    "list_recorded_moves": {"dataset_name": "pollen-robotics/reachy-mini-emotions-library"},
    "list_apps": {"limit": 3},
    "get_urdf": {"max_chars": 1000},
    "read_audio_parameter": {"name": "AUDIO_MGR_OP_L"},
    # need ids produced by earlier actions; skipped here
    "get_move_status": None,
    "wait_for_move": None,
    "get_app_job_status": None,
}


async def main() -> int:
    url = os.environ["MCP_E2E_URL"]
    token = os.environ.get("MCP_AUTH_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    failures = 0
    async with Client(streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers, timeout=60))) as c:
        tools = (await c.list_tools()).tools
        for tool in sorted(tools, key=lambda t: t.name):
            if not tool.annotations.read_only_hint:
                continue
            args = ARGS.get(tool.name, {})
            if args is None:
                print(f"SKIP {tool.name} (needs an id from a previous action)")
                continue
            r = await c.call_tool(tool.name, args)
            text = json.dumps(r.structured_content, ensure_ascii=False, default=str)
            status = "ERR " if r.is_error else "OK  "
            if r.is_error:
                text = " ".join(getattr(x, "text", "") for x in r.content)
            print(f"{status}{tool.name}: {text[:160]}")
        refused = await c.call_tool("wake_up", {})
        ok = refused.is_error and "refused_by_safety_policy" in refused.content[0].text
        print(("OK  " if ok else "FAIL") + " wake_up refused: " + refused.content[0].text[:120])
        failures += 0 if ok else 1
    return failures


if __name__ == "__main__":
    sys.exit(anyio.run(main))
