"""Generate docs/api-inventory.md from the OpenAPI snapshot and the tool registry.

    docker compose --profile test run --rm -v ./scripts:/app/scripts:ro -v ./docs:/app/docs tests \
        python scripts/gen_inventory.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from minireachy_mcp.client import MiniReachyClient  # noqa: E402
from minireachy_mcp.config import Settings  # noqa: E402
from minireachy_mcp.inventory import EXCLUDED  # noqa: E402
from minireachy_mcp.safety import Risk  # noqa: E402
from minireachy_mcp.server import build_server  # noqa: E402

# Endpoints exercised against the REAL daemon (v1.8.0) through the containerised MCP server on
# 2026-10-11: read-only tools (tests/test_e2e.py, scripts/smoke_readonly.py) and, with the
# operator's explicit approval, goto_pose (scripts/nod_test.py, hello_test.py) and speak
# (scripts/conversation_smoke.py: upload + play_sound + delete).
REAL_OK = {
    "GET /api/daemon/status", "GET /api/daemon/hardware-id", "GET /api/daemon/robot-app-lock-status",
    "GET /api/motors/status", "GET /api/move/running", "GET /api/state/full", "GET /api/state/present_head_pose",
    "GET /api/state/present_body_yaw", "GET /api/state/present_antenna_joint_positions", "GET /api/state/doa",
    "GET /api/media/status", "GET /api/media/sounds", "GET /api/volume/current", "GET /api/volume/microphone/current",
    "GET /api/camera/specs", "GET /api/kinematics/info", "GET /api/kinematics/urdf", "GET /api/apps/current-app-status",
    "GET /api/apps/check-updates", "GET /api/apps/list-available", "GET /api/hf-auth/status",
    "GET /api/hf-auth/relay-status", "GET /api/hf-auth/oauth/configured", "GET /api/hf-auth/central-robot-status",
    "GET /api/move/recorded-move-datasets/list/{dataset_name}",
    "POST /api/move/goto", "POST /api/media/sounds/upload", "POST /api/media/play_sound",
    "DELETE /api/media/sounds/{filename}",
}
REAL_NOTE = {
    "GET /api/audio/config/parameter/{name}": "Called on the real daemon: HTTP 500 \"Errno 13 Access denied\" "
    "(USB access denied on the host side, identical with a direct call). Error correctly reported.",
    "GET /api/apps/list-available/{source_kind}": "Direct call on the real daemon OK (installed); via MCP tested in simulation.",
    "GET /api/apps/job-status/{job_id}": "Direct call on the real daemon: 404 for an unknown id; nominal case tested in simulation.",
    "POST /api/move/goto": "Real robot: movements executed. The head does not exactly reach the return poses "
    "(3 to 14° gap, see README); the antennas follow faithfully.",
}
RISK_LABEL = {
    Risk.READ: "Read", Risk.CONFIG: "Configuration", Risk.STOP: "Stop (always allowed)",
    Risk.PHYSICAL: "Physical", Risk.ADMIN: "Administration", Risk.PHYSICAL_ADMIN: "Physical + administration",
    Risk.PERCEPTION: "Perception",
}
EFFECTS = {
    Risk.READ: "None",
    Risk.CONFIG: "Changes a setting or a non-physical file",
    Risk.STOP: "Interrupts an activity",
    Risk.PHYSICAL: "Movement, motor torque, sound or app driving the robot",
    Risk.ADMIN: "Lifecycle / installation / hardware",
    Risk.PHYSICAL_ADMIN: "Backend lifecycle, may make the robot move",
    Risk.PERCEPTION: "Camera or microphones",
}


def _ref(schema: dict) -> str:
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[1]
    if "anyOf" in schema:
        return " \\| ".join(_ref(s) for s in schema["anyOf"])
    if schema.get("type") == "array":
        return f"array[{_ref(schema.get('items', {}))}]" if "items" in schema else "array"
    return schema.get("type", "any") if schema else "any"


def main() -> None:
    spec = json.loads((ROOT / "docs/api/openapi.json").read_text("utf-8"))
    settings = Settings()
    _, deps = build_server(settings, MiniReachyClient(settings))
    by_endpoint: dict[str, list] = {}
    for tool in deps.registry.values():
        for ep in tool.endpoints:
            by_endpoint.setdefault(ep, []).append(tool)

    rows = []
    counts = {"real": 0, "sim": 0, "excl": 0, "blocked": 0}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            key = f"{method.upper()} {path}"
            params = ", ".join(
                f"{p['name']} ({p['in']}{', required' if p.get('required') else ''}: {_ref(p.get('schema', {}))})"
                for p in op.get("parameters", [])
            ) or "—"
            body = "—"
            if "requestBody" in op:
                content = op["requestBody"]["content"]
                ctype = next(iter(content))
                body = f"{_ref(content[ctype]['schema'])} ({ctype.split('/')[-1]})"
            resp = _ref(op.get("responses", {}).get("200", {}).get("content", {}).get("application/json", {}).get("schema", {}))
            tools = by_endpoint.get(key, [])
            if tools:
                # A daemon write is never "perception": converse speaks/moves through it as a physical action.
                risks = [Risk.PHYSICAL if t.risk == Risk.PERCEPTION else t.risk for t in tools]
                risk = Risk.READ if method == "get" else max(risks, key=list(Risk).index)
                tool_names = ", ".join(f"`{t.name}`" for t in tools)
                if key in REAL_OK:
                    status = "Implemented and tested (simulation + real daemon)"
                    counts["real"] += 1
                else:
                    status = "Implemented, tested in simulation, not tested against the real daemon"
                    counts["sim"] += 1
                if key in REAL_NOTE:
                    status += ". " + REAL_NOTE[key]
                rows.append((method.upper(), path, op.get("summary", ""), params, body, resp,
                             EFFECTS[risk], RISK_LABEL[risk], tool_names, status))
            else:
                counts["excl"] += 1
                rows.append((method.upper(), path, op.get("summary", ""), params, body, resp, "—", "—", "—",
                             "Excluded: " + EXCLUDED[key]))

    out = [
        "# MiniReachy daemon API inventory",
        "",
        "File generated by `scripts/gen_inventory.py` from `docs/api/openapi.json` "
        f"(Reachy Mini daemon v1.8.0, {sum(len(v) for v in spec['paths'].values())} operations) "
        "and the MCP tool registry. Do not edit by hand.",
        "",
        "Authentication: the schema declares no security mechanism, and none was "
        "needed during the real calls.",
        "",
        f"* Implemented and tested against the real daemon: **{counts['real']}**",
        f"* Implemented, tested in simulation only: **{counts['sim']}**",
        f"* Excluded with justification: **{counts['excl']}**",
        f"* Blocked: **{counts['blocked']}**",
        "",
        "Physical commands tested on the real robot, with the operator's approval: `goto_pose` and `speak` "
        "(upload + playback of a sound). All other commands were only tested in simulation.",
        "",
        "| Method | Path | Description | Parameters | Body | 200 response | Side effects | Risk | MCP tool(s) | Status |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") if i != 3 else str(c) for i, c in enumerate(r)) + " |")
    tools_only = [t for t in deps.registry.values() if not t.endpoints]
    out += ["", "## Tools without a direct endpoint", ""]
    local = {
        "diagnose_connectivity": "4-step network diagnostic (DNS, TCP, HTTP, daemon state).",
        "get_server_safety_config": "safety configuration of the MCP server.",
        "get_perception_status": "state of the WebRTC stream and of the local voice models.",
        "capture_image": "camera image, received through the daemon's WebRTC stream (port 8443, not described in "
        "the OpenAPI). Tested on the real robot.",
    }
    out += [f"* `{t.name}` : {local.get(t.name, 'computed locally by the MCP server.')}" for t in tools_only]
    out += [
        "",
        "## Media stream outside the OpenAPI",
        "",
        "The daemon also runs a GStreamer `webrtcsink` media server with its signalling server "
        "(`ws://<host>:8443`, producer `reachymini`). It broadcasts the camera video and the microphone audio. "
        "This stream does not appear in the OpenAPI; it was identified in the daemon code "
        "(`reachy_mini/media/media_server.py`) and validated on the real robot from the container. It feeds `capture_image` "
        "and `listen`. Speech recognition (faster-whisper) and speech synthesis (Piper) run locally "
        "in the container.",
    ]
    (ROOT / "docs/api-inventory.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(counts, len(deps.registry), "tools")


if __name__ == "__main__":
    main()
