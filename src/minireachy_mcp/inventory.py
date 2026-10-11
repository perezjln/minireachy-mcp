"""Endpoints of the daemon API that are deliberately NOT exposed as MCP tools.

Every operation of the OpenAPI schema must be either used by a registered tool
(see each tool's ``endpoints``) or listed here with a justification. The test
suite and ``python -m minireachy_mcp.diagnose --drift`` enforce this.
"""

EXCLUDED: dict[str, str] = {
    "GET /": "HTML dashboard for humans; no machine-readable data.",
    "POST /health-check": "Resets the daemon's own watchdog timer used by the desktop app; "
    "calling it from MCP would mask a dead desktop client.",
    "POST /api/move/set_target": "Sets a target with no duration or interpolation (instant jump), meant for "
    "high-frequency streaming control loops. goto_pose covers the same targets with a bounded duration.",
    "GET /api/kinematics/stl/{filename}": "Binary 3D mesh assets for visualisation; not useful to an LLM, and the "
    "path parameter accepts sub-paths.",
    "POST /api/hf-auth/save-token": "Would route a Hugging Face secret through the LLM context. The operator "
    "logs in through the Reachy Mini desktop app.",
    "DELETE /api/hf-auth/token": "Account credential management is left to the operator (desktop app).",
    "GET /api/hf-auth/oauth/start": "Interactive browser OAuth flow for a human user.",
    "GET /api/hf-auth/oauth/status/{session_id}": "Part of the interactive OAuth flow.",
    "DELETE /api/hf-auth/oauth/session/{session_id}": "Part of the interactive OAuth flow.",
    "GET /api/hf-auth/oauth/callback": "Browser redirect target of the OAuth flow; never called by clients.",
}
