"""Robot app management tools and Hugging Face / relay status."""

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..client import seg
from ..errors import SafetyGateError
from ..models import SAFE_NAME, SourceKind
from ..safety import Risk
from ._common import Deps, accepted, register

_SPACE_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"


def _compact(app: dict[str, Any], include_extra: bool) -> dict[str, Any]:
    """Keep what an LLM needs; the raw list is several MB."""
    extra = app.get("extra") or {}
    card = extra.get("cardData") or {}
    out: dict[str, Any] = {
        "name": app.get("name"),
        "source_kind": app.get("source_kind"),
        "description": app.get("description") or card.get("short_description") or "",
        "id": extra.get("id"),
        "url": app.get("url"),
    }
    if include_extra:
        out["extra"] = extra
    return out


def register_tools(server: MCPServer, deps: Deps) -> None:
    c = deps.client
    s = deps.settings

    @register(
        server,
        deps,
        risk=Risk.READ,
        endpoints=("GET /api/apps/list-available/{source_kind}", "GET /api/apps/list-available"),
    )
    async def list_apps(
        source_kind: Annotated[
            SourceKind | None,
            Field(description="installed | hf_space | dashboard_selection | local. Omit for all sources (slow, large)."),
        ] = None,
        name_contains: Annotated[str | None, Field(max_length=100, description="Case-insensitive name filter.")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        include_extra: Annotated[bool, Field(description="Include raw Hugging Face metadata (large).")] = False,
    ) -> dict[str, Any]:
        """List robot apps (installed or available), in a compact form.

        Use source_kind="installed" to see what can be started with start_app.
        """
        path = "/api/apps/list-available" + (f"/{seg(source_kind.value)}" if source_kind else "")
        apps = await c.get(path, max_bytes=s.max_app_list_bytes)
        if name_contains:
            needle = name_contains.lower()
            apps = [a for a in apps if needle in str(a.get("name", "")).lower()]
        return {
            "total": len(apps),
            "returned": min(len(apps), limit),
            "apps": [_compact(a, include_extra) for a in apps[:limit]],
        }

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/apps/current-app-status",))
    async def get_current_app_status() -> dict[str, Any]:
        """Get the currently running app: info, state (starting|running|done|stopping|error), error.

        running=false when no app is running.
        """
        status = await c.get("/api/apps/current-app-status")
        if status is None:
            return {"running": False}
        return {"running": True, **status}

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/apps/check-updates",))
    async def check_app_updates(
        force: Annotated[bool, Field(description="Bypass the daemon's 5-minute cache (slower).")] = False,
    ) -> dict[str, Any]:
        """Check installed apps for updates on Hugging Face. Returns apps_with_updates, apps_checked, apps_skipped."""
        return await c.get("/api/apps/check-updates", params={"force": force})

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/apps/job-status/{job_id}",))
    async def get_app_job_status(
        job_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,64}$", description="job_id from an install/remove/update.")],
    ) -> dict[str, Any]:
        """Get the status (pending|in_progress|done|failed) and logs of a background app job."""
        job = await c.get(f"/api/apps/job-status/{seg(job_id)}")
        logs = job.get("logs") or []
        if len(logs) > 50:
            job = {**job, "logs": logs[-50:], "logs_truncated": True, "logs_total": len(logs)}
        return job

    @register(server, deps, risk=Risk.PHYSICAL, endpoints=("POST /api/apps/start-app/{app_name}",))
    async def start_app(
        app_name: Annotated[str, Field(pattern=SAFE_NAME, description="Name of an installed app (list_apps installed).")],
    ) -> dict[str, Any]:
        """Start an installed app. Apps drive the robot themselves (motion, audio, camera),
        outside this server's guard rails. Returns the app status as reported at start.
        """
        status = await c.post(f"/api/apps/start-app/{seg(app_name)}")
        return accepted(
            "start_app", status, next_step="Poll get_current_app_status until state is 'running' or 'error'."
        )

    @register(server, deps, risk=Risk.PHYSICAL, endpoints=("POST /api/apps/restart-current-app",))
    async def restart_current_app() -> dict[str, Any]:
        """Restart the currently running app."""
        return accepted("restart_current_app", await c.post("/api/apps/restart-current-app"))

    @register(server, deps, risk=Risk.STOP, endpoints=("POST /api/apps/stop-current-app",))
    async def stop_current_app() -> dict[str, Any]:
        """Stop the currently running app. Always allowed."""
        return {"daemon_response": await c.post("/api/apps/stop-current-app")}

    @register(
        server,
        deps,
        risk=Risk.ADMIN,
        endpoints=("GET /api/apps/list-available/{source_kind}", "POST /api/apps/install"),
    )
    async def install_app(
        app_name: Annotated[str, Field(pattern=SAFE_NAME, description="Exact app name as returned by list_apps.")],
        source_kind: Annotated[
            SourceKind, Field(description="Source the app was listed under (usually hf_space or dashboard_selection).")
        ],
    ) -> dict[str, Any]:
        """Install an app (downloads and installs code from Hugging Face on the robot host).

        The server looks the app up in list-available/{source_kind} and sends that exact record,
        so only listed apps can be installed. Runs in the background: returns a job_id for
        get_app_job_status.
        """
        if source_kind == SourceKind.installed:
            raise SafetyGateError("source_kind 'installed' lists apps that are already installed.")
        apps = await c.get(f"/api/apps/list-available/{seg(source_kind.value)}", max_bytes=s.max_app_list_bytes)
        match = next((a for a in apps if a.get("name") == app_name), None)
        if match is None:
            raise SafetyGateError(f"No app named {app_name!r} in source {source_kind.value!r}; use list_apps first.")
        return accepted("install_app", await c.post("/api/apps/install", json_body=match))

    @register(server, deps, risk=Risk.ADMIN, endpoints=("POST /api/apps/install-private-space",))
    async def install_private_space(
        space_id: Annotated[str, Field(pattern=_SPACE_ID, description="Hugging Face space id 'owner/name'.")],
    ) -> dict[str, Any]:
        """Install a private Hugging Face space as an app (requires a HF token already stored in
        the daemon by the operator). Returns a job_id for get_app_job_status.
        """
        return accepted(
            "install_private_space", await c.post("/api/apps/install-private-space", json_body={"space_id": space_id})
        )

    @register(server, deps, risk=Risk.ADMIN, endpoints=("POST /api/apps/remove/{app_name}",))
    async def remove_app(app_name: Annotated[str, Field(pattern=SAFE_NAME)]) -> dict[str, Any]:
        """Uninstall an app. Background job: returns a job_id for get_app_job_status."""
        return accepted("remove_app", await c.post(f"/api/apps/remove/{seg(app_name)}"))

    @register(server, deps, risk=Risk.ADMIN, endpoints=("POST /api/apps/update/{app_name}",))
    async def update_app(app_name: Annotated[str, Field(pattern=SAFE_NAME)]) -> dict[str, Any]:
        """Update an installed app to its latest version from Hugging Face. Returns a job_id."""
        return accepted("update_app", await c.post(f"/api/apps/update/{seg(app_name)}"))

    # -- Hugging Face account / relay (read-only + reconnect) ---------------------------

    @register(
        server,
        deps,
        risk=Risk.READ,
        endpoints=("GET /api/hf-auth/status", "GET /api/hf-auth/oauth/configured"),
    )
    async def get_hf_auth_status() -> dict[str, Any]:
        """Tell whether the daemon is logged in to Hugging Face (and as whom) and whether OAuth is configured.

        Logging in / out is done by the operator in the Reachy Mini desktop app, not through this server.
        """
        return {
            "auth": await c.get("/api/hf-auth/status"),
            "oauth": await c.get("/api/hf-auth/oauth/configured"),
        }

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/hf-auth/relay-status",))
    async def get_relay_status() -> dict[str, Any]:
        """Get the status of the central signaling relay used for remote (WebRTC) sessions."""
        return await c.get("/api/hf-auth/relay-status")

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/hf-auth/central-robot-status",))
    async def get_central_robot_status() -> dict[str, Any]:
        """Ask the central server which of the user's robots are online/busy and with which app.

        available=false means unknown (not logged in or central server unreachable).
        """
        return await c.get("/api/hf-auth/central-robot-status")

    @register(server, deps, risk=Risk.CONFIG, endpoints=("POST /api/hf-auth/refresh-relay",))
    async def refresh_relay() -> dict[str, Any]:
        """Force the central signaling relay to reconnect.

        status "requested" = reconnect started; "skipped" with a reason = nothing happened
        (do not wait for a state change in that case).
        """
        return await c.post("/api/hf-auth/refresh-relay")
