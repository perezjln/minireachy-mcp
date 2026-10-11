"""MCP server assembly and Streamable HTTP application."""

from __future__ import annotations

import hmac
import json
import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .client import MiniReachyClient
from .config import Settings
from .errors import DaemonError
from .media.services import MediaServices
from .tools import Deps, register_all

logger = logging.getLogger("minireachy_mcp.server")


HEALTH_PATHS = ("/healthz", "/healthz/daemon")

INSTRUCTIONS = """\
Controls a Hugging Face / Pollen Robotics Reachy Mini ("MiniReachy") through its native daemon.

Conventions: translations in meters, angles in radians. Read-only tools are always available.
Perception (capture_image = what the robot sees, listen = one transcribed utterance) and voice
(speak) let you hold a spoken conversation through the robot; perception needs ENABLE_PERCEPTION.
Physical tools (motion, motor mode, sound output, starting apps) and admin tools (daemon
lifecycle, app install, media hand-over, audio DSP) may be disabled by the operator; call
get_server_safety_config first. A refused tool sent nothing to the robot: do not retry it.

Motion commands are asynchronous: the daemon returns a move UUID when the move is scheduled.
"accepted_by_daemon" never means "completed"; use wait_for_move and get_full_state to verify.
stop_move / stop_all_moves / stop_sound / stop_current_app are always allowed. There is no
hardware emergency stop in the API. If a result is "uncertain_outcome", read the robot state
before deciding anything; never blindly repeat a physical command.
"""


def build_server(
    settings: Settings, client: MiniReachyClient, media: MediaServices | None = None
) -> tuple[MCPServer, Deps]:
    server = MCPServer(
        "minireachy-mcp",
        title="MiniReachy (Reachy Mini) robot control",
        version=__version__,
        instructions=INSTRUCTIONS,
        log_level=settings.log_level,
    )
    deps = Deps(settings=settings, client=client, registry={},
                media=media or MediaServices.from_settings(settings))
    register_all(server, deps)

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> Response:
        # Liveness of the MCP server only: never depends on the daemon, so a stopped
        # daemon cannot cause a container restart loop.
        return JSONResponse({"status": "ok", "service": "minireachy-mcp", "version": __version__})

    @server.custom_route("/healthz/daemon", methods=["GET"], include_in_schema=False)
    async def healthz_daemon(_: Request) -> Response:
        try:
            status = await client.get("/api/daemon/status")
        except DaemonError as exc:
            return JSONResponse({"daemon_reachable": False, "error": exc.code}, status_code=503)
        state = status.get("state") if isinstance(status, dict) else None
        return JSONResponse(
            {"daemon_reachable": True, "daemon_state": state}, status_code=200 if state == "running" else 503
        )

    return server, deps


class BearerAuthMiddleware:
    """Optional static bearer token on every route except the health checks."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in HEALTH_PATHS:
            await self.app(scope, receive, send)
            return
        provided = dict(scope.get("headers") or []).get(b"authorization", b"")
        if not hmac.compare_digest(provided, self._expected):
            body = json.dumps({"error": "unauthorized"}).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def build_http_app(settings: Settings, server: MCPServer) -> ASGIApp:
    app: ASGIApp = server.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        host=settings.mcp_host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.mcp_allowed_hosts),
            allowed_origins=list(settings.mcp_allowed_origins),
        ),
    )
    if settings.mcp_auth_token:
        app = BearerAuthMiddleware(app, settings.mcp_auth_token)
    return app
