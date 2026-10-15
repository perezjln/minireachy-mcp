"""Motion tools: goto, wake up / sleep, recorded moves, stop, motor mode."""

import time
import uuid
from typing import Annotated, Any

import anyio
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..client import seg
from ..models import DATASET_NAME, SAFE_NAME, HeadPose, InterpolationTechnique, MotorControlMode
from ..safety import Risk, check_head_speed, check_motion
from ._common import Deps, accepted, register

_POLL_INTERVAL_S = 0.2


def register_tools(server: MCPServer, deps: Deps) -> None:
    c = deps.client
    mo = deps.motion
    lim = deps.settings.limits
    preflight = mo.preflight

    def move_result(action: str, response: Any, pre: dict[str, Any]) -> dict[str, Any]:
        move_uuid = response.get("uuid") if isinstance(response, dict) else None
        return accepted(
            action,
            response,
            move_uuid=move_uuid,
            preconditions=pre,
            next_step="Call wait_for_move(move_uuid) or get_move_status(move_uuid) to know when it ends; "
            "stop_move(move_uuid) interrupts it.",
        )

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/move/running",))
    async def list_running_moves() -> dict[str, Any]:
        """List the UUIDs of move tasks currently running on the daemon."""
        moves = await c.get("/api/move/running")
        return {"running_moves": [m.get("uuid") for m in moves], "count": len(moves)}

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/move/running",))
    async def get_move_status(move_uuid: uuid.UUID) -> dict[str, Any]:
        """Tell whether a move (by UUID) is still running.

        running=false means the move finished, was stopped or failed: the daemon API does not
        distinguish these cases. Check get_full_state for the resulting pose.
        """
        moves = await c.get("/api/move/running")
        running = str(move_uuid) in {m.get("uuid") for m in moves}
        return {"move_uuid": str(move_uuid), "running": running}

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/move/running",))
    async def wait_for_move(
        move_uuid: uuid.UUID,
        timeout_s: Annotated[float, Field(gt=0, le=60, description="Maximum time to wait.")] = 15.0,
    ) -> dict[str, Any]:
        """Wait until a move is no longer running, polling the daemon (read-only).

        Returns running=false once it left the running list (finished, stopped or failed —
        indistinguishable through the API), or timed_out=true if still running at timeout_s.
        """
        deadline = time.monotonic() + timeout_s
        target = str(move_uuid)
        while True:
            moves = await c.get("/api/move/running")
            if target not in {m.get("uuid") for m in moves}:
                return {"move_uuid": target, "running": False, "timed_out": False}
            if time.monotonic() >= deadline:
                return {"move_uuid": target, "running": True, "timed_out": True}
            await anyio.sleep(_POLL_INTERVAL_S)

    @register(server, deps, risk=Risk.PHYSICAL, endpoints=("POST /api/move/goto", "GET /api/state/present_head_pose"))
    async def goto_pose(
        duration: Annotated[
            float,
            Field(
                allow_inf_nan=False,
                description=f"Movement duration in seconds (server guard rail: "
                f"{lim.min_move_duration_s}–{lim.max_move_duration_s} s).",
            ),
        ],
        head_pose: Annotated[
            HeadPose | None,
            Field(
                description=f"Target head pose. Translation in meters (guard rail ±{lim.max_head_translation_m}), "
                f"roll/pitch/yaw in radians (±{lim.max_head_roll_rad}/±{lim.max_head_pitch_rad}/"
                f"±{lim.max_head_yaw_rad}). Omit to leave the head unchanged.",
            ),
        ] = None,
        antennas: Annotated[
            tuple[Annotated[float, Field(allow_inf_nan=False)], Annotated[float, Field(allow_inf_nan=False)]] | None,
            Field(description=f"Target antenna angles [left, right] in radians (guard rail ±{lim.max_antenna_rad})."),
        ] = None,
        body_yaw: Annotated[
            float | None, Field(allow_inf_nan=False, description=f"Target body yaw in radians (guard rail ±{lim.max_body_yaw_rad}).")
        ] = None,
        interpolation: InterpolationTechnique = InterpolationTechnique.minjerk,
    ) -> dict[str, Any]:
        """Move the head, antennas and/or body to a target over `duration` seconds.

        At least one of head_pose, antennas, body_yaw is required. The daemon answers as soon as
        the move is scheduled: the result contains move_uuid but completion is NOT confirmed.
        Use wait_for_move then get_full_state to verify the final position. A head move faster
        than the speed guard rail (from the present head pose) is refused with the minimum duration.
        """
        check_motion(deps.settings, head_pose=head_pose, antennas=antennas, body_yaw=body_yaw, duration=duration)
        pre = await preflight()
        if head_pose is not None and (present := await mo.present_head()) is not None:
            check_head_speed(deps.settings, start=present, target=head_pose.model_dump(), duration=duration)
        body: dict[str, Any] = {"duration": duration, "interpolation": interpolation.value}
        if head_pose is not None:
            body["head_pose"] = head_pose.model_dump()
        if antennas is not None:
            body["antennas"] = list(antennas)
        if body_yaw is not None:
            body["body_yaw"] = body_yaw
        return move_result("goto_pose", await mo.send_goto(body), pre)

    @register(server, deps, risk=Risk.PHYSICAL, endpoints=("POST /api/move/play/wake_up",))
    async def wake_up() -> dict[str, Any]:
        """Play the robot's built-in wake-up motion. Returns the move UUID (completion not confirmed)."""
        pre = await preflight()
        mo.forget_head_target()
        return move_result("wake_up", await c.post("/api/move/play/wake_up"), pre)

    @register(server, deps, risk=Risk.PHYSICAL, endpoints=("POST /api/move/play/goto_sleep",))
    async def goto_sleep() -> dict[str, Any]:
        """Play the robot's built-in go-to-sleep motion (head lowered to rest).

        Returns the move UUID (completion not confirmed).
        """
        pre = await preflight()
        mo.forget_head_target()
        return move_result("goto_sleep", await c.post("/api/move/play/goto_sleep"), pre)

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/move/recorded-move-datasets/list/{dataset_name}",))
    async def list_recorded_moves(
        dataset_name: Annotated[
            str,
            Field(
                pattern=DATASET_NAME,
                description="Hugging Face dataset id 'owner/name', e.g. "
                "'pollen-robotics/reachy-mini-emotions-library'.",
            ),
        ],
    ) -> dict[str, Any]:
        """List the recorded moves available in a Hugging Face move dataset.

        The daemon fetches the dataset from Hugging Face (needs internet; may be slow the first time).
        """
        moves = await c.get(f"/api/move/recorded-move-datasets/list/{seg(dataset_name)}")
        return {"dataset_name": dataset_name, "moves": moves, "count": len(moves)}

    @register(
        server,
        deps,
        risk=Risk.PHYSICAL,
        endpoints=("POST /api/move/play/recorded-move-dataset/{dataset_name}/{move_name}",),
    )
    async def play_recorded_move(
        dataset_name: Annotated[str, Field(pattern=DATASET_NAME, description="Dataset id 'owner/name'.")],
        move_name: Annotated[str, Field(pattern=SAFE_NAME, description="A name returned by list_recorded_moves.")],
    ) -> dict[str, Any]:
        """Play a pre-recorded move (e.g. an emotion like 'cheerful1') from a dataset.

        Recorded moves have their own amplitude and duration, not limited by this server's
        goto guard rails. Returns the move UUID (completion not confirmed).
        """
        pre = await preflight()
        mo.forget_head_target()
        path = f"/api/move/play/recorded-move-dataset/{seg(dataset_name)}/{seg(move_name)}"
        return move_result("play_recorded_move", await c.post(path), pre)

    @register(server, deps, risk=Risk.STOP, endpoints=("POST /api/move/stop",))
    async def stop_move(move_uuid: uuid.UUID) -> dict[str, Any]:
        """Stop one running move by UUID. Always allowed. The robot holds its current position."""
        mo.forget_head_target()
        response = await c.post("/api/move/stop", json_body={"uuid": str(move_uuid)})
        return {"move_uuid": str(move_uuid), "daemon_response": response}

    @register(server, deps, risk=Risk.STOP, endpoints=("GET /api/move/running", "POST /api/move/stop"))
    async def stop_all_moves() -> dict[str, Any]:
        """Stop every running move (software stop). Always allowed.

        This is NOT a hardware emergency stop: the daemon exposes none. It does not stop
        apps (use stop_current_app) nor audio-reactive wobbling (use disable_wobbling).
        Gestures in progress (converse) send no further step.
        """
        await mo.interrupt()  # first, so no gesture step is sent after the running list is read
        moves = await c.get("/api/move/running")
        results: list[dict[str, Any]] = []
        for m in moves:
            uid = m.get("uuid")
            try:
                results.append({"move_uuid": uid, "daemon_response": await c.post("/api/move/stop", json_body={"uuid": uid})})
            except Exception as exc:  # report and keep stopping the others
                results.append({"move_uuid": uid, "error": getattr(exc, "to_llm", lambda: str(exc))()})
        remaining = [m.get("uuid") for m in await c.get("/api/move/running")]
        return {"stopped": results, "still_running": remaining}

    @register(server, deps, risk=Risk.PHYSICAL, endpoints=("POST /api/motors/set_mode/{mode}", "GET /api/motors/status"))
    async def set_motor_mode(
        mode: Annotated[
            MotorControlMode,
            Field(
                description="enabled = motors hold position; disabled = no torque (the head can drop "
                "under gravity); gravity_compensation = compliant, can be moved by hand."
            ),
        ],
    ) -> dict[str, Any]:
        """Change the motor control mode, then read it back to confirm.

        WARNING: 'disabled' removes torque; place the robot in a safe pose first (goto_sleep).
        """
        mo.forget_head_target()
        response = await c.post(f"/api/motors/set_mode/{seg(mode.value)}")
        after = await c.get("/api/motors/status")
        confirmed = isinstance(after, dict) and after.get("mode") == mode.value
        return {
            "requested_mode": mode.value,
            "daemon_response": response,
            "mode_after": after.get("mode") if isinstance(after, dict) else None,
            "confirmed": confirmed,
        }
