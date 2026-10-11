"""One MCP tool call for a live session: prints a compact JSON result, saves an image to /out/view.jpg."""
import base64, json, os, sys
import anyio
from mcp import Client

async def main() -> None:
    async with Client(os.environ["MCP_E2E_URL"]) as c:
        r = await c.call_tool(sys.argv[1], json.loads(sys.argv[2] if len(sys.argv) > 2 else "{}"))
        out = []
        for item in r.content:
            if item.type == "image":
                with open("/out/view.jpg", "wb") as f:
                    f.write(base64.b64decode(item.data))
                out.append("[image -> view.jpg]")
            elif item.type == "text":
                out.append(item.text)
        if r.structured_content is not None and not r.is_error:
            out = [json.dumps(r.structured_content, ensure_ascii=False)]
        print(("ERREUR " if r.is_error else "") + " | ".join(out))

anyio.run(main)
