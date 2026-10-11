"""Print the structured result of one MCP tool as compact JSON: python scripts/mcp_json.py <tool> '<json>'"""
import json, os, sys
import anyio
from mcp import Client

async def main() -> None:
    async with Client(os.environ["MCP_E2E_URL"]) as c:
        r = await c.call_tool(sys.argv[1], json.loads(sys.argv[2] if len(sys.argv) > 2 else "{}"))
        print(json.dumps(r.structured_content if r.structured_content is not None
                         else [getattr(x, "text", x.type) for x in r.content], ensure_ascii=False))

anyio.run(main)
