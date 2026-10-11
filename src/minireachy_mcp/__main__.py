"""Entry point: ``python -m minireachy_mcp`` starts the Streamable HTTP MCP server."""

from __future__ import annotations

import contextlib
import logging
import signal
import sys
import threading
from collections.abc import Generator

import anyio
import uvicorn

from .client import MiniReachyClient
from .config import ConfigError, Settings, load_settings
from .server import build_http_app, build_server


class _GracefulServer(uvicorn.Server):
    """uvicorn re-raises SIGTERM/SIGINT after its graceful shutdown, which would kill the
    process before our own cleanup (closing the daemon HTTP client). Catch the signal,
    shut down, and let `serve()` return normally instead."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        stream=sys.stdout,
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    # httpx logs full URLs at INFO; our client already logs each request with a correlation id.
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def serve(settings: Settings) -> None:
    log = logging.getLogger("minireachy_mcp")
    async with MiniReachyClient(settings) as client:
        server, deps = build_server(settings, client)
        app = build_http_app(settings, server)
        if settings.enable_perception and settings.media_warmup:
            deps.media.start_warmup()  # background: models + media stream, no robot action
        log.info(
            "starting minireachy-mcp on %s:%d%s daemon=%s physical=%s admin=%s auth=%s tools=%d perception=%s",
            settings.mcp_host, settings.mcp_port, settings.mcp_path, settings.base_url,
            settings.enable_physical_actions, settings.enable_admin_actions,
            "bearer" if settings.mcp_auth_token else "none", len(deps.registry),
            settings.enable_perception,
        )
        config = uvicorn.Config(
            app,
            host=settings.mcp_host,
            port=settings.mcp_port,
            log_level=settings.log_level.lower(),
            log_config=None,  # keep our logging configuration
            proxy_headers=False,
            server_header=False,
            timeout_graceful_shutdown=5,
        )
        # uvicorn installs SIGTERM/SIGINT handlers and shuts down gracefully;
        # the daemon HTTP client is closed when this block exits. No robot action
        # is performed at startup or shutdown.
        try:
            await _GracefulServer(config).serve()
        finally:
            await deps.media.aclose()
    log.info("minireachy-mcp stopped")


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"minireachy-mcp: {exc}", file=sys.stderr)
        sys.exit(2)
    configure_logging(settings.log_level)
    anyio.run(serve, settings)


if __name__ == "__main__":
    main()
