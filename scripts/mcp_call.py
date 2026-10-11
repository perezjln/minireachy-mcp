"""Call one MCP tool on the running server: python scripts/mcp_call.py <tool> '<json args>'
Images are written to /out/<tool>.jpg when /out is mounted."""

import base64
import json
import os
import sys

import anyio
from mcp import Client


async def main() -> None:
    tool, args = sys.argv[1], json.loads(sys.argv[2] if len(sys.argv) > 2 else "{}")
    async with Client(os.environ["MCP_E2E_URL"]) as c:
        r = await c.call_tool(tool, args)
        for item in r.content:
            if item.type == "image" and os.path.isdir("/out"):
                with open(f"/out/{tool}.jpg", "wb") as f:
                    f.write(base64.b64decode(item.data))
                print("image saved")
            elif item.type == "text":
                print(("ERROR " if r.is_error else "") + item.text[:600])


anyio.run(main)
