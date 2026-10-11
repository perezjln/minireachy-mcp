"""Daemon status, diagnostics and lifecycle tools."""

# NOTE: no `from __future__ import annotations` in tool modules: the MCP SDK builds
# input schemas from the real annotation objects.

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..diagnostics import run_connectivity_checks
from ..safety import Risk, describe
from ._common import Deps, accepted, register


def register_tools(server: MCPServer, deps: Deps) -> None:
    c = deps.client

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/daemon/status",))
    async def get_daemon_status() -> dict[str, Any]:
        """Get the MiniReachy daemon status.

        Returns robot_name, state (not_initialized|starting|running|stopping|stopped|error),
        version, simulation flags, media flags and backend_status (motor_control_mode,
        control loop statistics, error). Start here to check that the robot is usable.
        """
        return await c.get("/api/daemon/status")

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/daemon/hardware-id",))
    async def get_hardware_id() -> dict[str, Any]:
        """Get the robot's unique hardware ID (USB serial of its audio device).

        Returns {"hardware_id": str | null}; null means no robot hardware is attached.
        """
        return await c.get("/api/daemon/hardware-id")

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/daemon/robot-app-lock-status",))
    async def get_robot_app_lock_status() -> dict[str, Any]:
        """Tell whether a managed app currently holds the robot.

        state is "free", "local_app" (holder_name = app name) or "remote_session".
        SDK clients talking to the daemon directly are NOT reflected by this lock.
        """
        return await c.get("/api/daemon/robot-app-lock-status")

    @register(server, deps, risk=Risk.READ)
    async def diagnose_connectivity() -> dict[str, Any]:
        """Diagnose the link between this MCP server (in Docker) and the daemon (on the host).

        Runs four separate checks: DNS resolution of the daemon host, TCP port reachability,
        any HTTP answer, then a functional call to /api/daemon/status. Each step reports ok/error
        so network problems are not confused with robot problems. Sends no command to the robot.
        """
        return await run_connectivity_checks(deps.settings, c)

    @register(server, deps, risk=Risk.READ)
    async def get_server_safety_config() -> dict[str, Any]:
        """Show which action categories this MCP server allows and the active motion guard rails.

        Call this before planning physical actions: if physical_actions_enabled is false,
        every movement/sound/motor tool will be refused.
        """
        return describe(deps.settings)

    @register(server, deps, risk=Risk.PHYSICAL_ADMIN, endpoints=("POST /api/daemon/start",))
    async def start_daemon(
        wake_up: Annotated[bool, Field(description="If true the robot also plays its wake-up motion.")] = False,
    ) -> dict[str, Any]:
        """Start the daemon's robot backend (the HTTP API stays up either way).

        With wake_up=true the robot moves. Returns the daemon's answer; poll get_daemon_status
        until state == "running" to confirm.
        """
        return accepted("start_daemon", await c.post("/api/daemon/start", params={"wake_up": wake_up}))

    @register(server, deps, risk=Risk.PHYSICAL_ADMIN, endpoints=("POST /api/daemon/stop",))
    async def stop_daemon(
        goto_sleep: Annotated[bool, Field(description="If true the robot first moves to its sleep pose.")] = True,
    ) -> dict[str, Any]:
        """Stop the daemon's robot backend. Robot control becomes unavailable until start_daemon.

        Prefer goto_sleep=true so the head is lowered to a rest position first.
        Poll get_daemon_status to confirm state == "stopped".
        """
        return accepted("stop_daemon", await c.post("/api/daemon/stop", params={"goto_sleep": goto_sleep}))

    @register(server, deps, risk=Risk.PHYSICAL_ADMIN, endpoints=("POST /api/daemon/restart",))
    async def restart_daemon() -> dict[str, Any]:
        """Restart the daemon's robot backend. Ongoing moves and apps are interrupted.

        Poll get_daemon_status until state == "running" to confirm.
        """
        return accepted("restart_daemon", await c.post("/api/daemon/restart"))
