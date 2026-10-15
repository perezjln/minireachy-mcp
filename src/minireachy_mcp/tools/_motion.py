"""Shared motion plumbing: preflight, head target tracking, speed-aware durations, stoppable sends.

Every head/antenna `goto` sent by a tool goes through `Motion.send_goto`, so that:

* `stop_all_moves` can interrupt multi-step sequences (gestures): it bumps an epoch under the
  same lock as the sends, and a sequence started under an older epoch sends nothing more;
* relative head moves start from the last *commanded* head target rather than the measured
  pose: the head of the tested robot sags a few degrees below its setpoint, and adding a
  relative turn to the measured pose would compound that error turn after turn.
"""

from __future__ import annotations

import math
from typing import Any

import anyio
from mcp.server.mcpserver.exceptions import ToolError

from ..client import MiniReachyClient
from ..config import Settings
from ..errors import DaemonError, SafetyGateError
from ..safety import head_delta

# Above this gap between the commanded target and the measured head, something else moved the
# head (an app, a recorded move, someone's hand): the measured pose is then the better reference.
STALE_TARGET_RAD = 0.3
_AXES = ("roll", "pitch", "yaw")


class MotionInterrupted(Exception):
    """A stop command arrived while a motion sequence was running."""


class Motion:
    def __init__(self, settings: Settings, client: MiniReachyClient) -> None:
        self.settings = settings
        self.limits = settings.limits
        self.client = client
        self._lock = anyio.Lock()
        self._epoch = 0
        self._head_target: dict[str, float] | None = None

    # -- state -------------------------------------------------------------------------------

    @property
    def epoch(self) -> int:
        """Current stop generation: capture it before a sequence and pass it to `send_goto`."""
        return self._epoch

    @property
    def head_target(self) -> dict[str, float] | None:
        return dict(self._head_target) if self._head_target is not None else None

    def forget_head_target(self) -> None:
        """Something other than a head goto drives the head now (stop, built-in or recorded move)."""
        self._head_target = None

    async def interrupt(self) -> None:
        """Make running sequences stop sending; waits for a send in flight so it can be stopped too."""
        async with self._lock:
            self._epoch += 1
            self._head_target = None

    # -- reads -------------------------------------------------------------------------------

    async def preflight(self) -> dict[str, Any]:
        """Read-only checks before a motion command. Refuses if the daemon is not running."""
        status = await self.client.get("/api/daemon/status")
        state = status.get("state") if isinstance(status, dict) else None
        if state != "running":
            raise SafetyGateError(f"Daemon state is {state!r}, not 'running'; motion commands are not sent.")
        backend = status.get("backend_status") or {}
        mode = backend.get("motor_control_mode")
        info: dict[str, Any] = {"daemon_state": state, "motor_control_mode": mode}
        if mode != "enabled":
            info["warning"] = (
                f"Motors are in mode {mode!r}: the robot may not follow the commanded motion "
                "(use set_motor_mode('enabled') if appropriate)."
            )
        return info

    async def present_head(self) -> dict[str, float] | None:
        try:
            pose = await self.client.get("/api/state/present_head_pose")
        except DaemonError:
            return None
        return pose if isinstance(pose, dict) and "yaw" in pose else None

    def reference(self, present: dict[str, float] | None) -> tuple[dict[str, float] | None, str]:
        """Pose relative head moves start from, and where it comes from (commanded/present/unknown)."""
        target = self._head_target
        if target is not None and (
            present is None or all(abs(present.get(a, 0.0) - target[a]) <= STALE_TARGET_RAD for a in _AXES)
        ):
            return dict(target), "commanded"
        if present is not None:
            return present, "present"
        return None, "unknown"

    async def head_reference(self) -> tuple[dict[str, float] | None, str, dict[str, float] | None]:
        """(reference pose, its source, measured pose) in one daemon read."""
        present = await self.present_head()
        base, source = self.reference(present)
        return base, source, present

    # -- targets and durations ---------------------------------------------------------------

    def clamp_head(self, pitch: float, yaw: float) -> tuple[dict[str, float], bool]:
        """Straight head (roll 0, centered) toward pitch/yaw, clamped to the guard rails."""
        if not (math.isfinite(pitch) and math.isfinite(yaw)):
            # min/max would turn NaN into the largest allowed angle.
            raise ToolError(f"invalid head target (pitch={pitch}, yaw={yaw}): values must be finite numbers")
        lim = self.limits
        p = max(-lim.max_head_pitch_rad, min(lim.max_head_pitch_rad, pitch))
        y = max(-lim.max_head_yaw_rad, min(lim.max_head_yaw_rad, yaw))
        pose = {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": round(p, 4), "yaw": round(y, 4)}
        return pose, (p, y) != (pitch, yaw)

    def head_duration(
        self, start: dict[str, float] | None, target: dict[str, float], requested: float | None = None
    ) -> float:
        """Duration of a head move: `requested`, or one that grows with the turn (0.6–1.5 s), and
        never shorter than what the speed guard rail allows from `start` (the measured pose)."""
        delta = head_delta(start, target) if start is not None else 0.0
        duration = requested if requested is not None else min(1.5, max(0.6, 0.4 + delta))
        duration = max(duration, delta / self.limits.max_head_speed_rad_s, self.limits.min_move_duration_s)
        return round(min(duration, self.limits.max_move_duration_s), 3)

    # -- commands ----------------------------------------------------------------------------

    async def send_goto(self, body: dict[str, Any], *, epoch: int | None = None) -> Any:
        """POST /api/move/goto and return the daemon response.

        With `epoch`, raise `MotionInterrupted` instead of sending if a stop came since.
        """
        async with self._lock:
            if epoch is not None and epoch != self._epoch:
                raise MotionInterrupted
            move = await self.client.post("/api/move/goto", json_body=body)
            if body.get("head_pose") is not None:
                self._head_target = {a: float(body["head_pose"].get(a, 0.0)) for a in _AXES}
        return move


def move_uuid(response: Any) -> str | None:
    return response.get("uuid") if isinstance(response, dict) else None
