"""Live smoke test of the conversation chain through the running MCP server:
camera -> greeting (speech) -> listening (STT) -> spoken echo.

    docker compose --profile e2e run --rm -v ./scripts:/app/scripts:ro -v <dir>:/out e2e python scripts/conversation_smoke.py

Requires ENABLE_PHYSICAL_ACTIONS=true and ENABLE_PERCEPTION=true. Moves only the antennas.
"""

import base64
import json
import os
import time

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def call(c: Client, name: str, args: dict | None = None) -> object:
    t = time.monotonic()
    r = await c.call_tool(name, args or {})
    took = time.monotonic() - t
    if r.is_error:
        print(f"[{took:5.2f}s] {name} ERROR: {' '.join(getattr(x, 'text', '') for x in r.content)}")
        return None
    if r.structured_content is not None:
        print(f"[{took:5.2f}s] {name}: {json.dumps(r.structured_content, ensure_ascii=False)[:300]}")
        return r.structured_content
    for item in r.content:
        if item.type == "image" and os.path.isdir("/out"):
            with open("/out/reachy_view.jpg", "wb") as f:
                f.write(base64.b64decode(item.data))
        elif item.type == "text":
            print(f"[{took:5.2f}s] {name}: {item.text}")
    return r.content


async def main() -> None:
    token = os.environ.get("MCP_AUTH_TOKEN")
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=120)
    async with Client(streamable_http_client(os.environ["MCP_E2E_URL"], http_client=http)) as c:
        await call(c, "get_server_safety_config")
        await call(c, "capture_image", {"max_width": 1024})
        await call(c, "get_perception_status")
        await call(c, "goto_pose", {"duration": 0.5, "antennas": [0.6, -0.6]})
        await call(c, "speak", {"text": "Bonjour ! Je suis Reachy. Maintenant je peux voir, écouter et parler. "
                                        "Dis-moi quelque chose !"})
        await call(c, "goto_pose", {"duration": 0.5, "antennas": [0.0, 0.0]})
        heard = await call(c, "listen", {"max_wait_s": 12, "silence_ms": 900})
        if heard and heard.get("heard"):
            await call(c, "speak", {"text": f"Tu as dit : {heard['text']}"})
        else:
            await call(c, "speak", {"text": "Je n'ai rien entendu, mais mes oreilles fonctionnent !"})
        await call(c, "get_perception_status")


anyio.run(main)
