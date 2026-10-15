"""Real-robot check: a converse gesture is interrupted by stop_all_moves sent 0.5 s after it starts.

python scripts/stop_gesture_test.py [gesture] [delay_s]"""

import json
import os
import sys
import time

import anyio
from mcp import Client


async def main() -> None:
    gesture = sys.argv[1] if len(sys.argv) > 1 else "joie"
    delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    async with Client(os.environ["MCP_E2E_URL"]) as c:
        results: dict[str, object] = {}
        t0 = time.perf_counter()

        async def turn() -> None:
            r = await c.call_tool("converse", {"gesture_name": gesture, "listen_after": False, "look": "never"})
            results["converse"] = (round(time.perf_counter() - t0, 2), r.content[0].text)

        async with anyio.create_task_group() as tg:
            tg.start_soon(turn)
            await anyio.sleep(delay)
            r = await c.call_tool("stop_all_moves", {})
            results["stop_all_moves"] = (round(time.perf_counter() - t0, 2), r.content[0].text)
        for name, (at, text) in results.items():
            print(f"{name} returned at {at}s: {json.dumps(json.loads(text), ensure_ascii=False)[:500]}")


anyio.run(main)
