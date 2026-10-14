"""Shared plumbing for tool modules: registration, gating, error translation, audit log."""

from __future__ import annotations

import functools
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..client import MiniReachyClient, correlation_id
from ..config import Settings
from ..errors import DaemonError, SafetyGateError
from ..media.webrtc import MediaUnavailableError
from ..safety import Risk, require

logger = logging.getLogger("minireachy_mcp.tools")

# Arguments never written to logs (large or potentially sensitive).
_REDACTED_ARGS = {"content_base64"}


@dataclass(frozen=True)
class ToolSpec:
    """Registry entry used by the inventory/drift tests."""

    name: str
    risk: Risk
    endpoints: tuple[str, ...]  # "METHOD /path" as written in the OpenAPI schema


@dataclass
class Deps:
    settings: Settings
    client: MiniReachyClient
    registry: dict[str, ToolSpec]
    media: Any = None  # minireachy_mcp.media.services.MediaServices


_ANNOTATIONS = {
    Risk.READ: ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
    Risk.CONFIG: ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
    Risk.STOP: ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
    Risk.PHYSICAL: ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True),
    Risk.ADMIN: ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True),
    Risk.PHYSICAL_ADMIN: ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True),
    Risk.PERCEPTION: ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=True),
}

_GATE_NOTE = {
    Risk.PHYSICAL: "\n\nSAFETY: physical action — refused unless the operator set ENABLE_PHYSICAL_ACTIONS=true.",
    Risk.ADMIN: "\n\nSAFETY: administrative action — refused unless the operator set ENABLE_ADMIN_ACTIONS=true.",
    Risk.PHYSICAL_ADMIN: "\n\nSAFETY: requires both ENABLE_PHYSICAL_ACTIONS=true and ENABLE_ADMIN_ACTIONS=true.",
    Risk.STOP: "\n\nSAFETY: stop command — always allowed.",
}


def register(
    server: MCPServer,
    deps: Deps,
    *,
    risk: Risk,
    endpoints: tuple[str, ...] = (),
    title: str | None = None,
    structured: bool = True,
    annotations: ToolAnnotations | None = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Register an async tool with gating, correlation id, logging and error mapping.

    The wrapped function keeps its signature so the SDK builds the input schema from it.
    `annotations` overrides the hints derived from `risk` (for tools whose effects span classes).
    """

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        name = fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            cid = uuid.uuid4().hex[:12]
            token = correlation_id.set(cid)
            started = time.perf_counter()
            outcome = "ok"
            try:
                require(deps.settings, risk, name)
                if risk not in (Risk.READ, Risk.PERCEPTION):
                    safe_args = {k: ("<redacted>" if k in _REDACTED_ARGS else v) for k, v in kwargs.items()}
                    logger.info("action_requested cid=%s tool=%s risk=%s args=%s", cid, name, risk.value, safe_args)
                return await fn(*args, **kwargs)
            except SafetyGateError as exc:
                outcome = "refused"
                raise ToolError(exc.to_llm()) from None
            except DaemonError as exc:
                outcome = exc.code
                raise ToolError(exc.to_llm()) from None
            except MediaUnavailableError as exc:
                outcome = "media_unavailable"
                raise ToolError(f"[media_unavailable] {exc}") from None
            except ToolError:
                outcome = "tool_error"
                raise
            except anyio.get_cancelled_exc_class():
                outcome = "cancelled"
                raise
            except Exception:
                # Unexpected payload shape or bug: keep details in the server log, not in the LLM context.
                outcome = "internal_error"
                logger.exception("tool_error cid=%s tool=%s", cid, name)
                raise ToolError(
                    f"[internal_error] Unexpected server error (cid={cid}); details are in the server log. "
                    "If the tool sends commands, read the robot state before retrying."
                ) from None
            finally:
                logger.info(
                    "tool_call cid=%s tool=%s risk=%s outcome=%s duration_ms=%.0f",
                    cid, name, risk.value, outcome, (time.perf_counter() - started) * 1000,
                )
                correlation_id.reset(token)

        wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
        description = inspect.cleandoc(fn.__doc__ or "") + _GATE_NOTE.get(risk, "")
        server.add_tool(
            wrapper,
            name=name,
            title=title,
            description=description,
            annotations=annotations or _ANNOTATIONS[risk],
            structured_output=structured,
        )
        deps.registry[name] = ToolSpec(name=name, risk=risk, endpoints=endpoints)
        return wrapper

    return decorator


def accepted(action: str, response: Any, **extra: Any) -> dict[str, Any]:
    """Standard shape for commands whose completion the API does not confirm."""
    return {
        "accepted_by_daemon": True,
        "completion_confirmed": False,
        "action": action,
        "daemon_response": response,
        **extra,
    }
