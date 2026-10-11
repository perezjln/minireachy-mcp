"""Read-only robot state tools."""

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..safety import Risk
from ._common import Deps, register


def register_tools(server: MCPServer, deps: Deps) -> None:
    c = deps.client

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/state/full",))
    async def get_full_state(
        with_control_mode: bool = True,
        with_head_pose: bool = True,
        with_target_head_pose: bool = False,
        with_head_joints: bool = False,
        with_target_head_joints: bool = False,
        with_body_yaw: bool = True,
        with_target_body_yaw: bool = False,
        with_antenna_positions: bool = True,
        with_target_antenna_positions: bool = False,
        with_passive_joints: bool = False,
        with_doa: bool = False,
        use_pose_matrix: Annotated[
            bool, Field(description="Return poses as a flattened 4x4 matrix instead of x,y,z,roll,pitch,yaw.")
        ] = False,
    ) -> dict[str, Any]:
        """Get a snapshot of the robot state, selecting which fields to include.

        Units: meters for translations, radians for angles. Fields not requested are null.
        antennas_position is [left, right]. doa is the sound direction of arrival.
        """
        return await c.get(
            "/api/state/full",
            params={
                "with_control_mode": with_control_mode,
                "with_head_pose": with_head_pose,
                "with_target_head_pose": with_target_head_pose,
                "with_head_joints": with_head_joints,
                "with_target_head_joints": with_target_head_joints,
                "with_body_yaw": with_body_yaw,
                "with_target_body_yaw": with_target_body_yaw,
                "with_antenna_positions": with_antenna_positions,
                "with_target_antenna_positions": with_target_antenna_positions,
                "with_passive_joints": with_passive_joints,
                "with_doa": with_doa,
                "use_pose_matrix": use_pose_matrix,
            },
        )

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/state/present_head_pose",))
    async def get_head_pose(use_pose_matrix: bool = False) -> dict[str, Any]:
        """Get the present head pose.

        Default: {x, y, z (meters), roll, pitch, yaw (radians)}. With use_pose_matrix=true:
        {"m": [16 floats]} = row-major 4x4 homogeneous transform (translation in meters).
        """
        return await c.get("/api/state/present_head_pose", params={"use_pose_matrix": use_pose_matrix})

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/state/present_body_yaw",))
    async def get_body_yaw() -> dict[str, Any]:
        """Get the present body yaw in radians. Returns {"body_yaw_rad": float}."""
        return {"body_yaw_rad": await c.get("/api/state/present_body_yaw")}

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/state/present_antenna_joint_positions",))
    async def get_antenna_positions() -> dict[str, Any]:
        """Get the present antenna joint positions in radians. Returns {"left_rad", "right_rad"}."""
        left, right = await c.get("/api/state/present_antenna_joint_positions")
        return {"left_rad": left, "right_rad": right}

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/state/doa",))
    async def get_direction_of_arrival() -> dict[str, Any]:
        """Get the sound direction of arrival from the microphone array.

        angle is in radians: 0 = robot's left, π/2 = front, π = right. speech_detected is a
        boolean. available=false means the audio device is not available.
        """
        doa = await c.get("/api/state/doa")
        if doa is None:
            return {"available": False}
        return {"available": True, **doa}

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/motors/status",))
    async def get_motor_status() -> dict[str, Any]:
        """Get the motor control mode: "enabled", "disabled" or "gravity_compensation"."""
        return await c.get("/api/motors/status")

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/camera/specs",))
    async def get_camera_specs() -> dict[str, Any]:
        """Get the detected camera: name, available/default resolutions (width, height, fps,
        crop_factor), intrinsic matrix K (3x3) and distortion coefficients D.

        This server does not stream images; the daemon's HTTP API has no frame endpoint.
        """
        return await c.get("/api/camera/specs")

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/kinematics/info",))
    async def get_kinematics_info() -> dict[str, Any]:
        """Get information about the kinematics engine (engine name, collision checking)."""
        return await c.get("/api/kinematics/info")

    @register(server, deps, risk=Risk.READ, endpoints=("GET /api/kinematics/urdf",))
    async def get_urdf(
        max_chars: Annotated[int, Field(ge=1000, le=500_000, description="Truncate the URDF to this length.")] = 20_000,
    ) -> dict[str, Any]:
        """Get the robot's URDF (XML robot description). It is large (~130 kB), so it is truncated
        to max_chars by default; the result says whether it was truncated and the full length.
        """
        data = await c.get("/api/kinematics/urdf")
        urdf = data.get("urdf", "") if isinstance(data, dict) else ""
        return {"urdf": urdf[:max_chars], "length": len(urdf), "truncated": len(urdf) > max_chars}
