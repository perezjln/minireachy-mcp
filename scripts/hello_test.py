"""Physical test: a "bonjour" gesture through the MCP server, then back to the initial pose,
then 40 s of read-only daemon monitoring.

    docker compose --profile e2e run --rm -v ./scripts:/app/scripts:ro e2e python scripts/hello_test.py

Requires ENABLE_PHYSICAL_ACTIONS=true. Head tilt 0.2 rad (~11°), antennas ±0.6 rad, all within guard rails.
"""

import os
import time

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

POSE_KEYS = ("x", "y", "z", "roll", "pitch", "yaw")


async def call(c: Client, name: str, args: dict) -> dict:
    r = await c.call_tool(name, args)
    if r.is_error:
        raise RuntimeError(f"{name}: {' '.join(getattr(x, 'text', '') for x in r.content)}")
    return r.structured_content


async def step(c: Client, label: str, **goto: object) -> None:
    r = await call(c, "goto_pose", goto)
    w = await call(c, "wait_for_move", {"move_uuid": r["move_uuid"], "timeout_s": 5})
    print(f"{label:<22} accepted={r['accepted_by_daemon']} done={not w['running']} timed_out={w['timed_out']}")


async def main() -> None:
    token = os.environ.get("MCP_AUTH_TOKEN")
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=60)
    async with Client(streamable_http_client(os.environ["MCP_E2E_URL"], http_client=http)) as c:
        state = await call(c, "get_full_state", {})
        home = {k: state["head_pose"][k] for k in POSE_KEYS}
        ant0 = state["antennas_position"]
        print("start   : pitch=%+.4f roll=%+.4f antennas=[%+.3f, %+.3f]" % (home["pitch"], home["roll"], *ant0))

        tilt = {**home, "roll": home["roll"] + 0.2, "pitch": home["pitch"] - 0.1}
        await step(c, "head tilted", duration=0.8, head_pose=tilt, antennas=[0.6, -0.6], interpolation="ease_in_out")
        for i in (1, 2):
            await step(c, f"antennes {i}/2 (a)", duration=0.4, antennas=[-0.6, 0.6], interpolation="ease_in_out")
            await step(c, f"antennes {i}/2 (b)", duration=0.4, antennas=[0.6, -0.6], interpolation="ease_in_out")
        await step(c, "retour", duration=1.0, head_pose=home, antennas=list(ant0), interpolation="minjerk")

        await anyio.sleep(0.5)
        end = await call(c, "get_full_state", {})
        p = end["head_pose"]
        print("end     : pitch=%+.4f roll=%+.4f antennas=[%+.3f, %+.3f]" % (p["pitch"], p["roll"], *end["antennas_position"]))
        print("delta   : pitch=%+.4f roll=%+.4f" % (p["pitch"] - home["pitch"], p["roll"] - home["roll"]))

        print("surveillance du daemon (40 s, lecture seule) :")
        t0 = time.monotonic()
        while time.monotonic() - t0 < 40:
            r = await c.call_tool("get_daemon_status", {})
            ok = (not r.is_error) and r.structured_content.get("state") == "running"
            pitch = ""
            if ok:
                hp = (await call(c, "get_head_pose", {}))["pitch"]
                pitch = f" pitch={hp:+.4f}"
            print(f"  t+{time.monotonic() - t0:4.0f}s daemon={'running' if ok else 'INJOIGNABLE'}{pitch}")
            await anyio.sleep(5)


anyio.run(main)
