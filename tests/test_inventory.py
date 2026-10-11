"""Every OpenAPI operation must be either used by a tool or explicitly excluded."""

import pytest

from minireachy_mcp.diagnostics import drift_report, openapi_operations
from minireachy_mcp.inventory import EXCLUDED

from .fake_daemon import OPENAPI

pytestmark = pytest.mark.anyio


async def test_no_drift_against_snapshot(mcp_session) -> None:
    async with mcp_session() as (_, deps):
        covered = {e for spec in deps.registry.values() for e in spec.endpoints}
    report = drift_report(OPENAPI, covered)
    assert report["unclassified"] == [], "new daemon endpoints need a tool or an exclusion"
    assert report["tools_reference_missing_endpoints"] == []
    assert report["exclusions_for_missing_endpoints"] == []
    assert report["overlap_covered_and_excluded"] == []
    assert report["operations"] == 67
    assert report["covered_by_tools"] + report["excluded"] == report["operations"]


def test_drift_detects_new_and_removed_endpoints() -> None:
    schema = {"paths": {"/api/new": {"get": {}}, "/api/daemon/status": {"get": {}}}}
    report = drift_report(schema, {"GET /api/daemon/status", "POST /api/gone"})
    assert report["unclassified"] == ["GET /api/new"]
    assert report["tools_reference_missing_endpoints"] == ["POST /api/gone"]


def test_exclusions_are_justified() -> None:
    ops = openapi_operations(OPENAPI)
    for op, reason in EXCLUDED.items():
        assert op in ops
        assert len(reason) > 20
