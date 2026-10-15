"""Shared motion control: stop interrupts gestures, head target tracking, speed guard rail, durations."""

import asyncio
import json

import pytest

from .conftest import text_of
from .fake_daemon import DAEMON_STATUS, HEAD_POSE, Override
from .test_perception_tools import StubMedia

pytestmark = pytest.mark.anyio


@pytest.fixture
def media() -> StubMedia:
    return StubMedia()


def gotos(fake) -> list[dict]:
    return [c.body for c in fake.calls("POST") if c.path == "/api/move/goto"]


def head_gotos(fake) -> list[dict]:
    return [g for g in gotos(fake) if "head_pose" in g]


def present(fake, **pose: float) -> None:
    fake.overrides["GET /api/state/present_head_pose"] = Override(body={**HEAD_POSE, **pose})


def json_part(result) -> dict:
    return json.loads(next(c.text for c in result.content if c.type == "text"))


# -- 1. stop_all_moves interrupts converse gestures ----------------------------------------


async def test_stop_all_moves_interrupts_a_converse_gesture(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        turn = asyncio.create_task(client.call_tool(
            "converse", {"gesture_name": "salut", "listen_after": False, "look": "never"}))
        await asyncio.sleep(0.5)  # the "salut" gesture has 4 steps of ~0.35 s
        stop = await client.call_tool("stop_all_moves", {})
        sent_before_stop_returned = len(gotos(fake))
        r = await turn
    assert not r.is_error, text_of(r)
    gesture = json_part(r)["gesture"]
    assert gesture["interrupted"] is True and 1 <= gesture["steps_sent"] < 4
    assert len(gotos(fake)) == sent_before_stop_returned  # nothing sent after the stop
    assert stop.structured_content["still_running"] == []


async def test_stop_all_moves_interrupts_a_head_gesture(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        turn = asyncio.create_task(client.call_tool(
            "converse", {"gesture_name": "non", "listen_after": False, "look": "never"}))
        await asyncio.sleep(0.4)
        await client.call_tool("stop_all_moves", {})
        sent = len(gotos(fake))
        r = await turn
    assert json_part(r)["gesture"]["interrupted"] is True
    assert len(gotos(fake)) == sent


async def test_gesture_completes_without_stop(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        r = await client.call_tool("converse", {"gesture_name": "salut", "listen_after": False, "look": "never"})
    gesture = json_part(r)["gesture"]
    assert gesture == {"gesture": "salut", "steps_sent": 4}


# -- 2. relative head moves start from the commanded target --------------------------------


async def test_nudge_starts_from_commanded_target_when_the_head_sags(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        first = await client.call_tool("nudge_head", {"pitch": -0.15})
        target = first.structured_content["head_pose"]
        present(fake, pitch=target["pitch"] + 0.1, yaw=target["yaw"])  # the head sagged 0.1 rad
        second = await client.call_tool("nudge_head", {"pitch": -0.15})
    assert first.structured_content["reference"] == "present"
    assert second.structured_content["reference"] == "commanded"
    assert second.structured_content["head_pose"]["pitch"] == pytest.approx(target["pitch"] - 0.15, abs=1e-3)


async def test_external_motion_makes_the_commanded_target_stale(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        await client.call_tool("nudge_head", {"yaw": 0.1})
        present(fake, yaw=0.9)  # moved by something else (an app, a hand) by far more than any sag
        r = await client.call_tool("nudge_head", {"yaw": 0.1})
    assert r.structured_content["reference"] == "present"
    assert r.structured_content["head_pose"]["yaw"] == pytest.approx(1.0, abs=1e-3)


@pytest.mark.parametrize(
    "tool,args",
    [("stop_all_moves", {}), ("stop_move", {"move_uuid": "0b3ec2a4-9a51-4bd2-8a51-0c4b0d1e2f3a"}),
     ("wake_up", {}), ("goto_sleep", {}),
     ("play_recorded_move", {"dataset_name": "pollen-robotics/reachy-mini-emotions-library",
                             "move_name": "cheerful1"})],
)
async def test_other_moves_forget_the_commanded_target(fake, mcp_session, media, tool, args) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        await client.call_tool("nudge_head", {"pitch": -0.15})
        await client.call_tool(tool, args)
        r = await client.call_tool("nudge_head", {"pitch": -0.15})
    assert r.structured_content["reference"] == "present"


async def test_goto_pose_head_target_is_the_new_reference(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        await client.call_tool("goto_pose", {"duration": 1.0, "head_pose": {"yaw": 0.2}})
        present(fake, yaw=0.15, pitch=0.08)
        r = await client.call_tool("nudge_head", {"yaw": 0.1})
    assert r.structured_content["reference"] == "commanded"
    assert r.structured_content["head_pose"]["yaw"] == pytest.approx(0.3, abs=1e-3)
    assert r.structured_content["head_pose"]["pitch"] == 0.0


async def test_converse_head_turn_starts_from_commanded_target(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        await client.call_tool("nudge_head", {"pitch": -0.15})
        target = head_gotos(fake)[-1]["head_pose"]
        present(fake, pitch=target["pitch"] + 0.1)
        r = await client.call_tool("converse", {"head_turn": [0.0, -0.15], "listen_after": False, "look": "never"})
    head = json_part(r)["head"]
    assert head["from"] == "commanded"
    assert head["head_pose"]["pitch"] == pytest.approx(target["pitch"] - 0.15, abs=1e-3)


async def test_look_at_point_builds_on_the_target_commanded_at_capture(fake, mcp_session, media) -> None:
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        await client.call_tool("nudge_head", {"pitch": -0.15})
        target = head_gotos(fake)[-1]["head_pose"]
        present(fake, pitch=target["pitch"] + 0.1)
        await client.call_tool("capture_image", {})
        r = await client.call_tool("look_at_point", {"x": 512, "y": 288})  # image center: no turn
    assert r.structured_content["head_pose"]["pitch"] == pytest.approx(target["pitch"], abs=1e-3)


# -- 3. head speed guard rail -------------------------------------------------------------


async def test_goto_pose_refuses_a_too_fast_head_move(fake, mcp_session) -> None:
    async with mcp_session(enable_physical_actions=True) as (client, _):
        fast = await client.call_tool("goto_pose", {"duration": 0.3, "head_pose": {"yaw": 1.2}})
        assert fake.non_get_calls() == []
        ok = await client.call_tool("goto_pose", {"duration": 0.7, "head_pose": {"yaw": 1.2}})
    assert fast.is_error and "rad/s" in text_of(fast) and "duration >= 0.7" in text_of(fast)
    assert not ok.is_error, text_of(ok)


async def test_head_speed_limit_is_configurable(fake, mcp_session) -> None:
    from minireachy_mcp.config import MotionLimits

    async with mcp_session(enable_physical_actions=True, limits=MotionLimits(max_head_speed_rad_s=5.0)) as (client, _):
        r = await client.call_tool("goto_pose", {"duration": 0.3, "head_pose": {"yaw": 1.2}})
    assert not r.is_error, text_of(r)


async def test_explicit_short_duration_is_raised_to_the_speed_limit(fake, mcp_session, media) -> None:
    present(fake, roll=0.0, pitch=0.0, yaw=0.0)
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        r = await client.call_tool("nudge_head", {"yaw": 1.0, "duration": 0.3})
    assert r.structured_content["duration_s"] == pytest.approx(0.5)  # 1.0 rad at 2 rad/s
    assert head_gotos(fake)[-1]["duration"] == pytest.approx(0.5)


# -- 4. durations grow with the turn, not with the absolute angle ---------------------------


async def test_converse_head_duration_depends_on_the_turn(fake, mcp_session, media) -> None:
    present(fake, roll=0.0, pitch=0.0, yaw=1.0)
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        small = await client.call_tool("converse", {"head_turn": [0.1, 0.0], "listen_after": False, "look": "never"})
        present(fake, roll=0.0, pitch=0.0, yaw=0.0)
        await client.call_tool("stop_all_moves", {})  # forget the target: start from the present pose
        large = await client.call_tool("converse", {"head_turn": [1.0, 0.0], "listen_after": False, "look": "never"})
    assert json_part(small)["head"]["duration_s"] == pytest.approx(0.6)  # was 1.5 (from |yaw| = 1.1)
    assert json_part(large)["head"]["duration_s"] == pytest.approx(1.4)


async def test_nudge_and_turn_toward_sound_default_durations(fake, mcp_session, media) -> None:
    present(fake, roll=0.0, pitch=0.0, yaw=0.0)
    fake.overrides["GET /api/state/doa"] = Override(body={"angle": 0.0, "speech_detected": True})
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        nudge = await client.call_tool("nudge_head", {"yaw": 0.1})
        sound = await client.call_tool("turn_toward_sound", {})
    assert nudge.structured_content["duration_s"] == pytest.approx(0.6)
    assert sound.structured_content["duration_s"] == pytest.approx(1.5)  # 1.2 rad turn


# -- 5. one preflight for every motion path ------------------------------------------------


async def test_converse_motion_checks_the_daemon_first(fake, mcp_session, media) -> None:
    fake.overrides["GET /api/daemon/status"] = Override(body={**DAEMON_STATUS, "state": "stopped"})
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        r = await client.call_tool("converse", {"gesture_name": "joie", "listen_after": False, "look": "never"})
    assert r.is_error and "not 'running'" in text_of(r)
    assert fake.non_get_calls() == []


async def test_converse_reports_motor_mode_warning(fake, mcp_session, media) -> None:
    status = {**DAEMON_STATUS, "backend_status": {**DAEMON_STATUS["backend_status"], "motor_control_mode": "disabled"}}
    fake.overrides["GET /api/daemon/status"] = Override(body=status)
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        r = await client.call_tool("converse", {"gesture_name": "oui", "listen_after": False, "look": "never"})
    assert "warning" in json_part(r)["preconditions"]


@pytest.mark.parametrize("tool,args", [("nudge_head", {"yaw": 0.1}), ("turn_toward_sound", {})])
async def test_head_tools_refused_when_daemon_not_running(fake, mcp_session, media, tool, args) -> None:
    fake.overrides["GET /api/daemon/status"] = Override(body={**DAEMON_STATUS, "state": "stopped"})
    async with mcp_session(media=media, enable_physical_actions=True, enable_perception=True) as (client, _):
        r = await client.call_tool(tool, args)
    assert r.is_error and "not 'running'" in text_of(r)
    assert fake.non_get_calls() == []
