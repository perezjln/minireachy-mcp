"""CLI diagnostics, meant to run inside the container:

    docker compose exec minireachy-mcp python -m minireachy_mcp.diagnose
    docker compose exec minireachy-mcp python -m minireachy_mcp.diagnose --drift

Read-only: sends no command to the robot.
"""

from __future__ import annotations

import argparse
import json
import sys

import anyio

from .client import MiniReachyClient
from .config import ConfigError, load_settings
from .diagnostics import drift_report, run_connectivity_checks
from .server import build_server


async def _run(drift: bool) -> int:
    settings = load_settings()
    async with MiniReachyClient(settings) as client:
        report = await run_connectivity_checks(settings, client)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        code = 0 if report["all_ok"] else 1
        if drift:
            schema = await client.get("/openapi.json")
            _, deps = build_server(settings, client)
            covered = {e for spec in deps.registry.values() for e in spec.endpoints}
            result = drift_report(schema, covered)
            print(json.dumps({"openapi_drift": result}, indent=2))
            if result["unclassified"] or result["tools_reference_missing_endpoints"]:
                code = 1
        return code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--drift", action="store_true", help="also compare the live OpenAPI with the tool inventory")
    args = parser.parse_args()
    try:
        sys.exit(anyio.run(_run, args.drift))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
