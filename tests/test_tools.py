"""Tool behaviour through a real MCP client (in-process transport) against the simulated daemon."""

import base64

import pytest

from minireachy_mcp.safety import Risk

from .conftest import text_of
from .fake_daemon import Override

pytestmark = pytest.mark.anyio

DATASET = "pollen-robotics/reachy-mini-emotions-library"


async def test_tool_discovery_and_annotations(mcp_session) -> None:
    async with mcp_session() as (client, deps):
        listed = {t.name: t for t in (await client.list_tools()).tools}
    assert set(listed) == set(deps.registry)
    assert len(listed) >= 50
    for name, tool in listed.items():
        risk = deps.registry[name].risk
        assert tool.description and len(tool.description) > 30, name
        assert tool.input_schema["type"] == "object"
        assert tool.annotations.read_only_hint is (risk in (Risk.READ, Risk.PERCEPTION)), name
        if risk in (Risk.PHYSICAL, Risk.ADMIN, Risk.PHYSICAL_ADMIN):
            assert tool.annotations.destructive_hint is True and "SAFETY" in tool.description
    goto = listed["goto_pose"].input_schema
    assert goto["required"] == ["duration"]
    assert set(goto["properties"]) == {"duration", "head_pose", "antennas", "body_yaw", "interpolation"}


async def test_read_tools_structured_output(fake, mcp_session) -> None:
    async with mcp_session() as (client, _):
        status = await client.call_tool("get_daemon_status", {})
        yaw = await client.call_tool("get_body_yaw", {})
        ant = await client.call_tool("get_antenna_positions", {})
        doa = await client.call_tool("get_direction_of_arrival", {})
        app = await client.call_tool("get_current_app_status", {})
        state = await client.call_tool("get_full_state", {"with_doa": True, "use_pose_matrix": True})
    assert status.structured_content["version"] == "1.8.0"
    assert yaw.structured_content == {"body_yaw_rad": -0.0015339807878858025}
    assert set(ant.structured_content) == {"left_rad", "right_rad"}
    assert doa.structured_content["available"] is True
    assert app.structured_content == {"running": False}
    assert fake.calls()[-1].query["with_doa"] == "true" and fake.calls()[-1].query["use_pose_matrix"] == "true"


async def test_doa_unavailable(fake, mcp_session) -> None:
    fake.overrides["GET /api/state/doa"] = Override(body=None)
    async with mcp_session() as (client, _):
        doa = await client.call_tool("get_direction_of_arrival", {})
    assert doa.structured_content == {"available": False}


async def test_urdf_truncated(fake, mcp_session) -> None:
    async with mcp_session() as (client, _):
        r = await client.call_tool("get_urdf", {"max_chars": 1000})
    assert r.structured_content["truncated"] is True
    assert len(r.structured_content["urdf"]) == 1000 and r.structured_content["length"] > 50_000


async def test_goto_sends_documented_body_and_reports_acceptance_not_completion(fake, mcp_session) -> None:
    args = {"duration": 2.0, "head_pose": {"pitch": 0.2, "z": 0.01}, "antennas": [0.5, -0.5], "body_yaw": 0.3,
            "interpolation": "cartoon"}
    async with mcp_session(enable_physical_actions=True) as (client, _):
        r = await client.call_tool("goto_pose", args)
    assert not r.is_error, text_of(r)
    out = r.structured_content
    assert out["accepted_by_daemon"] is True and out["completion_confirmed"] is False
    assert out["move_uuid"] in fake.running_moves
    post = fake.calls("POST")[0]
    assert post.path == "/api/move/goto"
    assert post.body == {
        "duration": 2.0, "interpolation": "cartoon", "antennas": [0.5, -0.5], "body_yaw": 0.3,
        "head_pose": {"x": 0.0, "y": 0.0, "z": 0.01, "roll": 0.0, "pitch": 0.2, "yaw": 0.0},
    }
    # preflight read happened before the command
    assert fake.requests[0].path == "/api/daemon/status"


async def test_goto_warns_when_motors_not_enabled(fake, mcp_session) -> None:
    from .fake_daemon import DAEMON_STATUS

    status = {**DAEMON_STATUS, "backend_status": {**DAEMON_STATUS["backend_status"], "motor_control_mode": "disabled"}}
    fake.overrides["GET /api/daemon/status"] = Override(body=status)
    async with mcp_session(enable_physical_actions=True) as (client, _):
        r = await client.call_tool("goto_pose", {"duration": 1.0, "antennas": [0, 0]})
    assert "warning" in r.structured_content["preconditions"]


async def test_move_lifecycle_status_wait_and_stop(fake, mcp_session) -> None:
    async with mcp_session(enable_physical_actions=True) as (client, _):
        uid = (await client.call_tool("wake_up", {})).structured_content["move_uuid"]
        assert (await client.call_tool("get_move_status", {"move_uuid": uid})).structured_content["running"] is True
        waited = await client.call_tool("wait_for_move", {"move_uuid": uid, "timeout_s": 0.3})
        assert waited.structured_content == {"move_uuid": uid, "running": True, "timed_out": True}
        await client.call_tool("stop_move", {"move_uuid": uid})
        waited = await client.call_tool("wait_for_move", {"move_uuid": uid, "timeout_s": 1})
        assert waited.structured_content["running"] is False and waited.structured_content["timed_out"] is False


async def test_stop_all_moves(fake, mcp_session) -> None:
    fake.running_moves.extend(["0b3ec2a4-9a51-4bd2-8a51-0c4b0d1e2f3a", "1b3ec2a4-9a51-4bd2-8a51-0c4b0d1e2f3a"])
    async with mcp_session() as (client, _):
        r = await client.call_tool("stop_all_moves", {})
    assert len(r.structured_content["stopped"]) == 2 and r.structured_content["still_running"] == []


async def test_uncertain_outcome_is_reported_and_not_retried(fake, mcp_session) -> None:
    fake.overrides["POST /api/move/play/wake_up"] = Override(body={"uuid": "x"}, delay=1.5)
    async with mcp_session(enable_physical_actions=True, read_timeout_s=0.3) as (client, _):
        r = await client.call_tool("wake_up", {})
    assert r.is_error and "uncertain_outcome" in text_of(r) and "MAY have executed" in text_of(r)
    assert len(fake.calls("POST")) == 1


async def test_daemon_unreachable_reported(closed_port, mcp_session) -> None:
    async with mcp_session(minireachy_base_url=f"http://127.0.0.1:{closed_port}", get_retries=0) as (client, _):
        r = await client.call_tool("get_daemon_status", {})
    assert r.is_error and "daemon_unreachable" in text_of(r)


async def test_set_motor_mode_reads_back(fake, mcp_session) -> None:
    async with mcp_session(enable_physical_actions=True) as (client, _):
        r = await client.call_tool("set_motor_mode", {"mode": "gravity_compensation"})
    assert r.structured_content["confirmed"] is True
    assert r.structured_content["mode_after"] == "gravity_compensation"


async def test_set_motor_mode_unconfirmed(fake, mcp_session) -> None:
    fake.overrides["GET /api/motors/status"] = Override(body={"mode": "enabled"})
    async with mcp_session(enable_physical_actions=True) as (client, _):
        r = await client.call_tool("set_motor_mode", {"mode": "disabled"})
    assert r.structured_content["confirmed"] is False


async def test_volume_confirmation(fake, mcp_session) -> None:
    async with mcp_session(enable_physical_actions=True) as (client, _):
        r = await client.call_tool("set_volume", {"volume": 40})
        bad = await client.call_tool("set_volume", {"volume": 140})
        mic = await client.call_tool("set_microphone_volume", {"volume": 70})
    assert r.structured_content["confirmed"] is True and fake.calls("POST")[0].body == {"volume": 40}
    assert bad.is_error
    assert mic.structured_content["confirmed"] is True


async def test_recorded_moves_dataset_encoding(fake, mcp_session) -> None:
    async with mcp_session(enable_physical_actions=True) as (client, _):
        listed = await client.call_tool("list_recorded_moves", {"dataset_name": DATASET})
        played = await client.call_tool("play_recorded_move", {"dataset_name": DATASET, "move_name": "cheerful1"})
        bad = await client.call_tool("list_recorded_moves", {"dataset_name": "../../etc"})
    assert listed.structured_content["count"] == 3
    assert not played.is_error
    assert any(r.raw_path.endswith("pollen-robotics%2Freachy-mini-emotions-library/cheerful1") for r in fake.calls("POST"))
    assert bad.is_error


async def test_list_apps_compact_and_filter(fake, mcp_session) -> None:
    async with mcp_session() as (client, _):
        r = await client.call_tool("list_apps", {"name_contains": "CONVERSATION"})
        installed = await client.call_tool("list_apps", {"source_kind": "installed"})
    apps = r.structured_content["apps"]
    assert r.structured_content["total"] == 1 and "extra" not in apps[0]
    assert apps[0]["description"] == "Talk with Reachy Mini!"
    assert fake.calls()[-1].path == "/api/apps/list-available/installed"
    assert installed.structured_content["total"] == 1


async def test_job_logs_truncated(fake, mcp_session) -> None:
    async with mcp_session() as (client, _):
        r = await client.call_tool("get_app_job_status", {"job_id": "11111111-2222-3333-4444-555555555555"})
    assert r.structured_content["logs_truncated"] is True and len(r.structured_content["logs"]) == 50


async def test_audio_parameter_errors_are_readable(fake, mcp_session) -> None:
    async with mcp_session() as (client, _):
        ok = await client.call_tool("read_audio_parameter", {"name": "AUDIO_MGR_OP_L"})
        missing = await client.call_tool("read_audio_parameter", {"name": "NOPE"})
    assert ok.structured_content == {"name": "AUDIO_MGR_OP_L", "values": [1.0]}
    assert missing.is_error and "could not be read" in text_of(missing)


async def test_upload_sound_multipart(fake, mcp_session) -> None:
    async with mcp_session() as (client, _):
        r = await client.call_tool(
            "upload_sound", {"filename": "hello.wav", "content_base64": base64.b64encode(b"RIFF0000").decode()}
        )
    assert r.structured_content["size_bytes"] == 8
    post = fake.calls("POST")[0]
    assert post.path == "/api/media/sounds/upload" and "multipart/form-data" in post.body["_content_type"]


async def test_hf_status_combines_two_reads(fake, mcp_session) -> None:
    async with mcp_session() as (client, _):
        r = await client.call_tool("get_hf_auth_status", {})
    assert r.structured_content["oauth"] == {"configured": True}


async def test_diagnose_connectivity_all_steps(fake, mcp_session) -> None:
    async with mcp_session() as (client, _):
        r = await client.call_tool("diagnose_connectivity", {})
    steps = [s["step"] for s in r.structured_content["steps"]]
    assert steps == ["dns_resolution", "tcp_connect", "http_response", "daemon_status"]
    assert r.structured_content["all_ok"] is True


async def test_diagnose_connectivity_stops_at_tcp(closed_port, mcp_session) -> None:
    async with mcp_session(minireachy_base_url=f"http://127.0.0.1:{closed_port}") as (client, _):
        r = await client.call_tool("diagnose_connectivity", {})
    steps = r.structured_content["steps"]
    assert [s["step"] for s in steps] == ["dns_resolution", "tcp_connect"] and steps[1]["ok"] is False


async def test_safety_config_tool(mcp_session) -> None:
    async with mcp_session(enable_physical_actions=True) as (client, _):
        r = await client.call_tool("get_server_safety_config", {})
    assert r.structured_content["physical_actions_enabled"] is True
    assert r.structured_content["admin_actions_enabled"] is False
