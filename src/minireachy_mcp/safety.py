"""Safety gates and motion guard rails.

Two independent switches, both off by default:

* ``ENABLE_PHYSICAL_ACTIONS`` — anything that can move the robot, change motor
  torque, emit sound through the speaker or start an app that drives the robot.
* ``ENABLE_ADMIN_ACTIONS`` — daemon lifecycle, app install/remove/update,
  hardware media hand-over and low-level audio DSP writes.
* ``ENABLE_PERCEPTION`` — camera images and microphone listening (privacy).

Stopping things (stop a move, stop a sound, stop the current app) is always
allowed because it only reduces activity.
"""

from __future__ import annotations

from enum import StrEnum

from .config import Settings
from .errors import SafetyGateError
from .models import HeadPose


class Risk(StrEnum):
    READ = "read"  # no side effect
    CONFIG = "config"  # non-physical, reversible state change
    STOP = "stop"  # reduces robot activity; always allowed
    PHYSICAL = "physical"  # motion, torque, sound output, robot-driving apps
    ADMIN = "admin"  # lifecycle / installation / hardware hand-over
    PHYSICAL_ADMIN = "physical+admin"  # needs both switches
    PERCEPTION = "perception"  # camera images, microphone listening


def require(settings: Settings, risk: Risk, action: str) -> None:
    """Raise `SafetyGateError` when ``risk`` is not allowed by the configuration."""
    needs_physical = risk in (Risk.PHYSICAL, Risk.PHYSICAL_ADMIN)
    needs_admin = risk in (Risk.ADMIN, Risk.PHYSICAL_ADMIN)
    missing = []
    if needs_physical and not settings.enable_physical_actions:
        missing.append("ENABLE_PHYSICAL_ACTIONS=true")
    if needs_admin and not settings.enable_admin_actions:
        missing.append("ENABLE_ADMIN_ACTIONS=true")
    if risk == Risk.PERCEPTION and not settings.enable_perception:
        missing.append("ENABLE_PERCEPTION=true")
    if missing:
        raise SafetyGateError(
            f"'{action}' is disabled by the server configuration (requires {' and '.join(missing)}). "
            "Only the operator can change this setting; do not try to work around it."
        )


def check_motion(
    settings: Settings,
    *,
    head_pose: HeadPose | None,
    antennas: tuple[float, float] | None,
    body_yaw: float | None,
    duration: float,
) -> None:
    """Validate a goto request against the configured `MotionLimits`."""
    lim = settings.limits
    errors: list[str] = []
    if not lim.min_move_duration_s <= duration <= lim.max_move_duration_s:
        errors.append(f"duration={duration}s outside [{lim.min_move_duration_s}, {lim.max_move_duration_s}] s")
    if head_pose is not None:
        for axis in ("x", "y", "z"):
            v = getattr(head_pose, axis)
            if abs(v) > lim.max_head_translation_m:
                errors.append(f"head_pose.{axis}={v} m exceeds ±{lim.max_head_translation_m} m")
        for axis, bound in (
            ("roll", lim.max_head_roll_rad),
            ("pitch", lim.max_head_pitch_rad),
            ("yaw", lim.max_head_yaw_rad),
        ):
            v = getattr(head_pose, axis)
            if abs(v) > bound:
                errors.append(f"head_pose.{axis}={v} rad exceeds ±{bound} rad")
    if antennas is not None:
        for side, v in zip(("left", "right"), antennas, strict=True):
            if abs(v) > lim.max_antenna_rad:
                errors.append(f"antenna {side}={v} rad exceeds ±{lim.max_antenna_rad} rad")
    if body_yaw is not None and abs(body_yaw) > lim.max_body_yaw_rad:
        errors.append(f"body_yaw={body_yaw} rad exceeds ±{lim.max_body_yaw_rad} rad")
    if head_pose is None and antennas is None and body_yaw is None:
        errors.append("at least one of head_pose, antennas or body_yaw is required")
    if errors:
        raise SafetyGateError("Motion request rejected by the server guard rails: " + "; ".join(errors) + ".")


def describe(settings: Settings) -> dict[str, object]:
    return {
        "physical_actions_enabled": settings.enable_physical_actions,
        "admin_actions_enabled": settings.enable_admin_actions,
        "perception_enabled": settings.enable_perception,
        "motion_limits": settings.limits.model_dump(),
        "notes": [
            "Motion limits are conservative MCP-side guard rails, not robot hardware specifications.",
            "stop_move, stop_all_moves, stop_sound and stop_current_app are always allowed.",
            "The daemon exposes no hardware emergency stop; stop_all_moves is a software stop only.",
        ],
    }
