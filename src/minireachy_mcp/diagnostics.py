"""Connectivity diagnostics (container -> host daemon) and OpenAPI drift detection."""

from __future__ import annotations

import socket
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import anyio
import httpx

from .config import Settings
from .errors import DaemonError
from .inventory import EXCLUDED

if TYPE_CHECKING:
    from .client import MiniReachyClient

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def _step(name: str, ok: bool, started: float, **info: Any) -> dict[str, Any]:
    return {"step": name, "ok": ok, "duration_ms": round((time.perf_counter() - started) * 1000), **info}


async def run_connectivity_checks(settings: Settings, client: MiniReachyClient) -> dict[str, Any]:
    """Four independent steps: DNS, TCP, HTTP, functional daemon status."""
    url = urlsplit(settings.base_url)
    host = url.hostname or ""
    port = url.port or (443 if url.scheme == "https" else 80)
    steps: list[dict[str, Any]] = []

    # 1. name resolution
    t = time.perf_counter()
    try:
        infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = sorted({str(i[4][0]) for i in infos})
        steps.append(_step("dns_resolution", True, t, host=host, addresses=addresses))
    except OSError as exc:
        steps.append(_step("dns_resolution", False, t, host=host, error=str(exc),
                           hint="On Linux add extra_hosts: host.docker.internal:host-gateway, or set MINIREACHY_BASE_URL."))
        return _summary(settings, steps)

    # 2. TCP port
    t = time.perf_counter()
    try:
        with anyio.fail_after(settings.connect_timeout_s):
            stream = await anyio.connect_tcp(host, port)
        await stream.aclose()
        steps.append(_step("tcp_connect", True, t, port=port))
    except (OSError, TimeoutError) as exc:
        steps.append(_step("tcp_connect", False, t, port=port, error=type(exc).__name__ + ": " + str(exc),
                           hint="Daemon stopped, listening on 127.0.0.1 only, or blocked by a host firewall."))
        return _summary(settings, steps)

    # 3. any HTTP answer (OpenAPI document, read-only)
    t = time.perf_counter()
    try:
        async with httpx.AsyncClient(base_url=settings.base_url, timeout=settings.read_timeout_s) as http:
            r = await http.get("/openapi.json")
        steps.append(_step("http_response", True, t, status=r.status_code,
                           api_title=(r.json().get("info", {}).get("title") if r.status_code == 200 else None)))
    except (httpx.HTTPError, ValueError) as exc:
        steps.append(_step("http_response", False, t, error=type(exc).__name__))
        return _summary(settings, steps)

    # 4. functional: daemon status
    t = time.perf_counter()
    try:
        status = await client.get("/api/daemon/status")
        steps.append(_step("daemon_status", status.get("state") == "running", t,
                           daemon_state=status.get("state"), daemon_version=status.get("version"),
                           backend_error=(status.get("backend_status") or {}).get("error"),
                           note="Network OK. 'ok' here only means the daemon reports state=running; "
                                "it does not prove the robot hardware is healthy."))
    except DaemonError as exc:
        steps.append(_step("daemon_status", False, t, error=exc.to_llm()))
    return _summary(settings, steps)


def _summary(settings: Settings, steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {"base_url": settings.base_url, "all_ok": all(s["ok"] for s in steps) and len(steps) == 4, "steps": steps}


def openapi_operations(schema: dict[str, Any]) -> set[str]:
    return {
        f"{method.upper()} {path}"
        for path, ops in schema.get("paths", {}).items()
        for method in ops
        if method in _HTTP_METHODS
    }


def drift_report(schema: dict[str, Any], covered: set[str]) -> dict[str, Any]:
    """Compare an OpenAPI schema with the endpoints used by tools plus the exclusion list."""
    ops = openapi_operations(schema)
    excluded = set(EXCLUDED)
    return {
        "daemon_api_version": schema.get("info", {}).get("version"),
        "operations": len(ops),
        "covered_by_tools": len(ops & covered),
        "excluded": len(ops & excluded),
        "unclassified": sorted(ops - covered - excluded),
        "tools_reference_missing_endpoints": sorted(covered - ops),
        "exclusions_for_missing_endpoints": sorted(excluded - ops),
        "overlap_covered_and_excluded": sorted(covered & excluded),
    }
