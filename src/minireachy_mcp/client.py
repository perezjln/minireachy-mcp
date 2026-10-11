"""Async HTTP client for the native MiniReachy daemon."""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings
from .errors import (
    DaemonAuthError,
    DaemonError,
    DaemonHTTPError,
    DaemonTimeoutError,
    DaemonUnreachableError,
    DaemonValidationError,
    InvalidResponseError,
    ResponseTooLargeError,
    UncertainOutcomeError,
)

logger = logging.getLogger("minireachy_mcp.client")

# Set by the tool layer so every daemon request can be tied back to one MCP tool call.
correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

_MAX_DETAIL_CHARS = 500


def seg(value: str) -> str:
    """Encode one path segment, including '/', so user input cannot alter the route."""
    return quote(value, safe="")


def _summarize_detail(detail: Any) -> str:
    """Turn a FastAPI error body into a short, readable message."""
    if isinstance(detail, list):  # FastAPI 422 format
        parts = []
        for item in detail[:5]:
            if isinstance(item, dict):
                loc = ".".join(str(x) for x in item.get("loc", []) if x != "body")
                parts.append(f"{loc}: {item.get('msg', '')}".strip(": "))
            else:
                parts.append(str(item))
        text = "; ".join(parts)
    elif isinstance(detail, str):
        text = detail
    else:
        text = json.dumps(detail, ensure_ascii=False, default=str)
    return text if len(text) <= _MAX_DETAIL_CHARS else text[:_MAX_DETAIL_CHARS] + "…"


class MiniReachyClient:
    """Thin wrapper over one shared `httpx.AsyncClient`.

    * GET requests are idempotent and retried on network failures (``get_retries``).
    * Any other method is sent exactly once; if the outcome is unknown an
      `UncertainOutcomeError` is raised instead of guessing.
    * Response bodies are streamed and aborted beyond ``max_response_bytes``.
    """

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_s,
                read=settings.read_timeout_s,
                write=settings.read_timeout_s,
                pool=settings.connect_timeout_s,
            ),
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": "minireachy-mcp", "Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> MiniReachyClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- public helpers -------------------------------------------------------------

    async def get(self, path: str, *, params: dict[str, Any] | None = None, max_bytes: int | None = None) -> Any:
        return await self.request("GET", path, params=params, max_bytes=max_bytes)

    async def post(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        return await self.request("POST", path, params=params, json_body=json_body, files=files)

    async def delete(self, path: str) -> Any:
        return await self.request("DELETE", path)

    # -- core -----------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: dict[str, Any] | None = None,
        max_bytes: int | None = None,
    ) -> Any:
        idempotent = method == "GET"
        attempts = 1 + (self.settings.get_retries if idempotent else 0)
        limit = max_bytes or self.settings.max_response_bytes
        if params:
            params = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in params.items() if v is not None}

        last_exc: DaemonError | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await self._send_once(method, path, params, json_body, files, limit, attempt)
            except (DaemonUnreachableError, DaemonTimeoutError) as exc:
                last_exc = exc
                if attempt < attempts:
                    logger.warning("retrying idempotent request cid=%s %s %s (%s)", correlation_id.get(), method, path, exc.code)
                    continue
                raise
        assert last_exc is not None  # pragma: no cover
        raise last_exc  # pragma: no cover

    async def _send_once(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        json_body: Any,
        files: dict[str, Any] | None,
        limit: int,
        attempt: int,
    ) -> Any:
        cid = correlation_id.get()
        started = time.perf_counter()
        request = self._http.build_request(
            method,
            path,
            params=params,
            json=json_body,
            files=files,
            headers={"X-Request-ID": f"{cid}-{uuid.uuid4().hex[:6]}"},
        )
        status: int | str = "-"
        try:
            response = await self._http.send(request, stream=True)
            try:
                status = response.status_code
                body = await self._read_limited(response, limit, method, path)
            finally:
                await response.aclose()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            status = "connect_error"
            raise DaemonUnreachableError(
                f"Cannot connect to the MiniReachy daemon at {self.settings.base_url} ({type(exc).__name__}). "
                "The request was not delivered. Check that the daemon is running and reachable from the "
                "container (see the diagnose_connectivity tool).",
                method=method,
                path=path,
            ) from None
        except (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError) as exc:
            status = "no_response"
            if method == "GET":
                raise DaemonTimeoutError(
                    f"No complete answer from the daemon ({type(exc).__name__}).", method=method, path=path
                ) from None
            raise UncertainOutcomeError(
                f"The request was sent but no complete answer was received ({type(exc).__name__}). "
                "The daemon MAY have executed it. It was not retried. Read the robot state "
                "(e.g. list_running_moves, get_full_state, get_daemon_status) before deciding what to do.",
                method=method,
                path=path,
            ) from None
        except httpx.HTTPError as exc:
            status = "transport_error"
            raise (DaemonUnreachableError if method == "GET" else UncertainOutcomeError)(
                f"HTTP transport error ({type(exc).__name__}).", method=method, path=path
            ) from None
        finally:
            logger.info(
                "daemon_request cid=%s method=%s path=%s status=%s attempt=%d duration_ms=%.0f",
                cid, method, path, status, attempt, (time.perf_counter() - started) * 1000,
            )
        return self._decode(response, body, method, path)

    async def _read_limited(self, response: httpx.Response, limit: int, method: str, path: str) -> bytes:
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise ResponseTooLargeError(
                f"Response of {declared} bytes exceeds the {limit}-byte limit.", method=method, path=path
            )
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > limit:
                raise ResponseTooLargeError(
                    f"Response exceeds the {limit}-byte limit and was discarded.", method=method, path=path
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _decode(self, response: httpx.Response, body: bytes, method: str, path: str) -> Any:
        payload: Any = None
        parse_error = False
        if body:
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                parse_error = True

        status = response.status_code
        if 200 <= status < 300:
            if parse_error:
                raise InvalidResponseError(
                    f"HTTP {status} but the body is not valid JSON "
                    f"(content-type {response.headers.get('content-type', 'unknown')!r}).",
                    method=method,
                    path=path,
                )
            return payload

        detail = payload.get("detail", payload) if isinstance(payload, dict) else (payload if not parse_error else None)
        summary = _summarize_detail(detail) if detail is not None else response.reason_phrase
        kwargs = {"status": status, "detail": detail, "method": method, "path": path}
        if status == 422:
            raise DaemonValidationError(f"The daemon rejected the parameters: {summary}", **kwargs)
        if status in (401, 403):
            raise DaemonAuthError(f"HTTP {status}: {summary}", **kwargs)
        if status == 404:
            raise DaemonHTTPError(f"HTTP 404 not found: {summary}", **kwargs)
        if status >= 500 and method != "GET":
            raise DaemonHTTPError(
                f"HTTP {status} from the daemon: {summary}. The operation may have partially run; "
                "check the robot state before retrying.",
                **kwargs,
            )
        raise DaemonHTTPError(f"HTTP {status}: {summary}", **kwargs)
