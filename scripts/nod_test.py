"""Physical test: a small head nod through the MCP server, then back to the initial pose.

    docker compose --profile e2e run --rm -v ./scripts:/app/scripts:ro e2e python scripts/nod_test.py

Requires ENABLE_PHYSICAL_ACTIONS=true. Amplitude 0.15 rad (~8.6°), 1 s per step.
"""

import json
import os

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

NOD_PITCH = 0.15  # rad, positive pitch = head down (per XYZRPY convention, verified by read-back below)


async def call(c: Client, name: str, args: dict) -> dict:
    r = await c.call_tool(name, args)
    if r.is_error:
        raise RuntimeError(f"{name}: {' '.join(getattr(x, 'text', '') for x in r.content)}")
    return r.structured_content


def pose_str(p: dict) -> str:
    return ", ".join(f"{k}={p[k]:+.4f}" for k in ("x", "y", "z", "roll", "pitch", "yaw"))


async def main() -> None:
    token = os.environ.get("MCP_AUTH_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http = httpx2.AsyncClient(headers=headers, timeout=60)
    async with Client(streamable_http_client(os.environ["MCP_E2E_URL"], http_client=http)) as c:
        print("safety:", json.dumps({k: v for k, v in (await call(c, "get_server_safety_config", {})).items()
                                     if k.endswith("enabled")}))
        print("motors:", (await call(c, "get_motor_status", {}))["mode"])
        start = await call(c, "get_head_pose", {})
        print("pose initiale :", pose_str(start))
        home = {k: start[k] for k in ("x", "y", "z", "roll", "pitch", "yaw")}

        for label, pitch in (("bas", start["pitch"] + NOD_PITCH), ("retour", start["pitch"])):
            target = {**home, "pitch": pitch}
            r = await call(c, "goto_pose", {"duration": 1.0, "head_pose": target, "interpolation": "minjerk"})
            print(f"goto {label}: accepted={r['accepted_by_daemon']} completed_confirmed={r['completion_confirmed']} "
                  f"uuid={r['move_uuid']} pre={r['preconditions']}")
            w = await call(c, "wait_for_move", {"move_uuid": r["move_uuid"], "timeout_s": 5})
            print(f"  wait: running={w['running']} timed_out={w['timed_out']}")
            await anyio.sleep(0.3)
            now = await call(c, "get_head_pose", {})
            print(f"  pose mesurée : {pose_str(now)}  (pitch visé {pitch:+.4f}, écart {now['pitch'] - pitch:+.4f})")

        print("moves en cours:", (await call(c, "list_running_moves", {}))["count"])


anyio.run(main)
