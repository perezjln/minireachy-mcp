"""HTTP client against the simulated daemon (real TCP)."""

import pytest

from minireachy_mcp.client import MiniReachyClient, seg
from minireachy_mcp.config import Settings
from minireachy_mcp.errors import (
    DaemonAuthError,
    DaemonHTTPError,
    DaemonTimeoutError,
    DaemonUnreachableError,
    DaemonValidationError,
    InvalidResponseError,
    ResponseTooLargeError,
    UncertainOutcomeError,
)

from .fake_daemon import Override

pytestmark = pytest.mark.anyio


async def test_get_json(fake, make_settings) -> None:
    async with MiniReachyClient(make_settings()) as c:
        status = await c.get("/api/daemon/status")
    assert status["state"] == "running"
    assert fake.calls("GET")[0].path == "/api/daemon/status"


async def test_bool_query_params_serialized_lowercase(fake, make_settings) -> None:
    async with MiniReachyClient(make_settings()) as c:
        await c.get("/api/state/full", params={"with_doa": True, "use_pose_matrix": False, "skip": None})
    assert fake.calls()[0].query == {"with_doa": "true", "use_pose_matrix": "false"}


async def test_path_segment_encoding_keeps_slash_inside_segment(fake, make_settings) -> None:
    async with MiniReachyClient(make_settings()) as c:
        await c.get(f"/api/move/recorded-move-datasets/list/{seg('owner/name')}")
    assert fake.calls()[0].raw_path.endswith("/list/owner%2Fname")


@pytest.mark.parametrize(
    "status,body,exc,needle",
    [
        (404, {"detail": "Job ID not found"}, DaemonHTTPError, "Job ID not found"),
        (422, {"detail": [{"loc": ["path", "source_kind"], "msg": "Input should be 'hf_space'"}]},
         DaemonValidationError, "path.source_kind: Input should be 'hf_space'"),
        (401, {"detail": "no"}, DaemonAuthError, "401"),
        (403, {"detail": "no"}, DaemonAuthError, "403"),
        (500, {"detail": "read_audio_parameter failed"}, DaemonHTTPError, "500"),
    ],
)
async def test_http_errors(fake, make_settings, status, body, exc, needle) -> None:
    fake.overrides["GET /api/x"] = Override(status=status, body=body)
    async with MiniReachyClient(make_settings()) as c:
        with pytest.raises(exc) as info:
            await c.get("/api/x")
    assert needle in info.value.to_llm()


async def test_post_500_warns_about_partial_execution(fake, make_settings) -> None:
    fake.overrides["POST /api/x"] = Override(status=500, body={"detail": "boom"})
    async with MiniReachyClient(make_settings()) as c:
        with pytest.raises(DaemonHTTPError, match="partially"):
            await c.post("/api/x")


async def test_long_error_detail_truncated(fake, make_settings) -> None:
    fake.overrides["GET /api/x"] = Override(status=404, body={"detail": "y" * 5000})
    async with MiniReachyClient(make_settings()) as c:
        with pytest.raises(DaemonHTTPError) as info:
            await c.get("/api/x")
    assert len(info.value.message) < 700


async def test_invalid_json_on_success(fake, make_settings) -> None:
    fake.overrides["GET /api/x"] = Override(raw=b"<html>not json</html>", content_type="text/html")
    async with MiniReachyClient(make_settings()) as c:
        with pytest.raises(InvalidResponseError, match="not valid JSON"):
            await c.get("/api/x")


async def test_non_json_error_body(fake, make_settings) -> None:
    fake.overrides["GET /api/x"] = Override(status=502, raw=b"Bad Gateway", content_type="text/plain")
    async with MiniReachyClient(make_settings()) as c:
        with pytest.raises(DaemonHTTPError, match="502"):
            await c.get("/api/x")


async def test_response_too_large(fake, make_settings) -> None:
    fake.overrides["GET /api/x"] = Override(raw=b'"' + b"a" * 5000 + b'"')
    async with MiniReachyClient(make_settings(max_response_bytes=1024)) as c:
        with pytest.raises(ResponseTooLargeError):
            await c.get("/api/x")
        # per-call override (used for the large app catalogue)
        assert len(await c.get("/api/x", max_bytes=10_000)) == 5000


async def test_get_timeout_is_retried_then_fails(fake, make_settings) -> None:
    fake.overrides["GET /api/slow"] = Override(body={}, delay=1.5)
    async with MiniReachyClient(make_settings(read_timeout_s=0.3, get_retries=1)) as c:
        with pytest.raises(DaemonTimeoutError):
            await c.get("/api/slow")
    assert len(fake.calls("GET")) == 2  # 1 try + 1 retry


async def test_post_timeout_is_uncertain_and_never_retried(fake, make_settings) -> None:
    fake.overrides["POST /api/move/goto"] = Override(body={"uuid": "x"}, delay=1.5)
    async with MiniReachyClient(make_settings(read_timeout_s=0.3, get_retries=3)) as c:
        with pytest.raises(UncertainOutcomeError) as info:
            await c.post("/api/move/goto", json_body={"duration": 1})
    assert len(fake.calls("POST")) == 1
    assert "MAY have executed" in info.value.to_llm()


async def test_connection_refused(closed_port) -> None:
    settings = Settings(minireachy_base_url=f"http://127.0.0.1:{closed_port}", connect_timeout_s=1, get_retries=0)
    async with MiniReachyClient(settings) as c:
        with pytest.raises(DaemonUnreachableError, match="not delivered"):
            await c.get("/api/daemon/status")
        # commands too: a refused connection means nothing was delivered
        with pytest.raises(DaemonUnreachableError):
            await c.post("/api/move/play/wake_up")


async def test_client_is_closed(make_settings) -> None:
    c = MiniReachyClient(make_settings())
    async with c:
        pass
    assert c._http.is_closed
