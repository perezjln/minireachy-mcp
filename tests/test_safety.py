"""Safety policy: physical/admin gates, guard rails, preconditions. Uses a real MCP client."""

import pytest

from minireachy_mcp.safety import Risk

from .conftest import text_of
from .fake_daemon import DAEMON_STATUS, Override

pytestmark = pytest.mark.anyio

UUID = "0b3ec2a4-9a51-4bd2-8a51-0c4b0d1e2f3a"

# Minimal valid arguments for every gated tool.
GATED_CALLS = {
    "goto_pose": {"duration": 1.0, "antennas": [0.1, -0.1]},
    "wake_up": {},
    "goto_sleep": {},
    "play_recorded_move": {"dataset_name": "pollen-robotics/reachy-mini-emotions-library", "move_name": "cheerful1"},
    "set_motor_mode": {"mode": "disabled"},
    "play_sound": {"file": "wake_up.wav"},
    "enable_wobbling": {},
    "disable_wobbling": {},
    "set_volume": {"volume": 30},
    "play_test_sound": {},
    "start_app": {"app_name": "reachy_mini_conversation_app"},
    "restart_current_app": {},
    "release_media": {},
    "acquire_media": {},
    "apply_audio_config": {"config": [{"name": "AUDIO_MGR_OP_L", "values": [1]}]},
    "install_app": {"app_name": "hand_tracker", "source_kind": "hf_space"},
    "install_private_space": {"space_id": "me/space"},
    "remove_app": {"app_name": "x"},
    "update_app": {"app_name": "x"},
    "start_daemon": {"wake_up": False},
    "stop_daemon": {"goto_sleep": True},
    "restart_daemon": {},
    "speak": {"text": "Bonjour"},
    "turn_toward_sound": {},
}


async def test_every_gated_tool_has_a_test_case(mcp_session) -> None:
    async with mcp_session() as (_, deps):
        gated = {n for n, s in deps.registry.items() if s.risk in (Risk.PHYSICAL, Risk.ADMIN, Risk.PHYSICAL_ADMIN)}
    assert gated == set(GATED_CALLS)


@pytest.mark.parametrize("tool", sorted(GATED_CALLS))
async def test_gated_tools_refused_by_default_and_send_nothing(fake, mcp_session, tool) -> None:
    async with mcp_session() as (client, _):
        result = await client.call_tool(tool, GATED_CALLS[tool])
    assert result.is_error
    assert "refused_by_safety_policy" in text_of(result)
    assert "No request was sent" in text_of(result)
    assert fake.requests == []  # not even a preflight read


async def test_physical_enabled_does_not_unlock_admin(fake, mcp_session) -> None:
    async with mcp_session(enable_physical_actions=True) as (client, _):
        for tool in ("restart_daemon", "install_app", "release_media"):
            result = await client.call_tool(tool, GATED_CALLS[tool])
            assert result.is_error and "ENABLE_ADMIN_ACTIONS" in text_of(result)
    assert fake.non_get_calls() == []


async def test_admin_enabled_does_not_unlock_physical(fake, mcp_session) -> None:
    async with mcp_session(enable_admin_actions=True) as (client, _):
        for tool in ("wake_up", "start_daemon", "set_volume"):
            result = await client.call_tool(tool, GATED_CALLS[tool])
            assert result.is_error and "ENABLE_PHYSICAL_ACTIONS" in text_of(result)
    assert fake.non_get_calls() == []


@pytest.mark.parametrize(
    "tool,args",
    [
        ("stop_move", {"move_uuid": UUID}),
        ("stop_all_moves", {}),
        ("stop_sound", {}),
        ("stop_current_app", {}),
    ],
)
async def test_stop_commands_always_allowed(fake, mcp_session, tool, args) -> None:
    async with mcp_session() as (client, _):
        result = await client.call_tool(tool, args)
    assert not result.is_error, text_of(result)


async def test_read_tools_work_when_everything_disabled(fake, mcp_session) -> None:
    async with mcp_session() as (client, _):
        for tool in ("get_daemon_status", "get_full_state", "get_motor_status", "list_running_moves"):
            result = await client.call_tool(tool, {})
            assert not result.is_error, text_of(result)
    assert fake.non_get_calls() == []


@pytest.mark.parametrize(
    "args,needle",
    [
        ({"duration": 1.0, "head_pose": {"pitch": 1.0}}, "head_pose.pitch"),
        ({"duration": 1.0, "head_pose": {"z": 0.05}}, "head_pose.z"),
        ({"duration": 1.0, "body_yaw": 2.0}, "body_yaw"),
        ({"duration": 1.0, "antennas": [3.5, 0]}, "antenna left"),
        ({"duration": 0.05, "antennas": [0, 0]}, "duration"),
        ({"duration": 60, "antennas": [0, 0]}, "duration"),
        ({"duration": 1.0}, "at least one"),
    ],
)
async def test_motion_guard_rails(fake, mcp_session, args, needle) -> None:
    async with mcp_session(enable_physical_actions=True) as (client, _):
        result = await client.call_tool("goto_pose", args)
    assert result.is_error and needle in text_of(result)
    assert fake.requests == []


@pytest.mark.parametrize(
    "args",
    [
        {"duration": 1.0, "head_pose": {"pitch": float("nan")}},
        {"duration": 1.0, "head_pose": {"x": 0.0, "bogus": 1}},
        {"duration": 1.0, "antennas": [0.1]},
        {"duration": 1.0, "interpolation": "teleport"},
    ],
)
async def test_schema_validation_rejects_bad_input(fake, mcp_session, args) -> None:
    async with mcp_session(enable_physical_actions=True) as (client, _):
        result = await client.call_tool("goto_pose", args)
    assert result.is_error
    assert fake.requests == []


async def test_guard_rails_configurable(fake, mcp_session) -> None:
    from minireachy_mcp.config import MotionLimits

    async with mcp_session(enable_physical_actions=True, limits=MotionLimits(max_head_pitch_rad=1.2)) as (client, _):
        result = await client.call_tool("goto_pose", {"duration": 1.0, "head_pose": {"pitch": 1.0}})
    assert not result.is_error, text_of(result)


async def test_motion_refused_when_daemon_not_running(fake, mcp_session) -> None:
    fake.overrides["GET /api/daemon/status"] = Override(body={**DAEMON_STATUS, "state": "stopped"})
    async with mcp_session(enable_physical_actions=True) as (client, _):
        result = await client.call_tool("wake_up", {})
    assert result.is_error and "not 'running'" in text_of(result)
    assert fake.non_get_calls() == []


@pytest.mark.parametrize("bad", ["/etc/passwd", "../secret.wav", "C:\\x.wav", "a/b.wav"])
async def test_play_sound_rejects_paths(fake, mcp_session, bad) -> None:
    async with mcp_session(enable_physical_actions=True) as (client, _):
        result = await client.call_tool("play_sound", {"file": bad})
    assert result.is_error
    assert fake.requests == []


async def test_upload_sound_limits(fake, mcp_session) -> None:
    import base64

    async with mcp_session(max_sound_upload_bytes=2048) as (client, _):
        too_big = base64.b64encode(b"x" * 4096).decode()
        r1 = await client.call_tool("upload_sound", {"filename": "a.wav", "content_base64": too_big})
        r2 = await client.call_tool("upload_sound", {"filename": "a.exe", "content_base64": "AAAA"})
        r3 = await client.call_tool("upload_sound", {"filename": "a.wav", "content_base64": "@@not-base64@@"})
        ok = await client.call_tool("upload_sound", {"filename": "a.wav", "content_base64": base64.b64encode(b"RIFF").decode()})
    assert r1.is_error and r2.is_error and r3.is_error
    assert not ok.is_error, text_of(ok)
    assert len(fake.calls("POST")) == 1


async def test_install_app_only_sends_listed_record(fake, mcp_session) -> None:
    async with mcp_session(enable_admin_actions=True) as (client, _):
        missing = await client.call_tool("install_app", {"app_name": "unknown_app", "source_kind": "hf_space"})
        ok = await client.call_tool("install_app", {"app_name": "hand_tracker", "source_kind": "hf_space"})
    assert missing.is_error and "list_apps" in text_of(missing)
    assert not ok.is_error
    posts = fake.calls("POST")
    assert len(posts) == 1 and posts[0].body["name"] == "hand_tracker" and posts[0].body["source_kind"] == "hf_space"
