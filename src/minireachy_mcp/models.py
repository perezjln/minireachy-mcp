"""Input models and enums mirroring the daemon's OpenAPI schemas (daemon 1.8.0)."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Hard sanity bounds on the schema itself. The tighter, configurable guard rails
# (MotionLimits) are checked at call time in safety.py.
_ANGLE = Annotated[float, Field(ge=-math.tau, le=math.tau, allow_inf_nan=False)]
_METERS = Annotated[float, Field(ge=-0.2, le=0.2, allow_inf_nan=False)]

# Names accepted for sound files / app names / dataset names. Restricting the
# character set prevents absolute paths and traversal (play_sound accepts absolute
# daemon paths, which we deliberately do not expose).
SAFE_FILENAME = r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$"
SAFE_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
DATASET_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"


class MotorControlMode(StrEnum):
    enabled = "enabled"
    disabled = "disabled"
    gravity_compensation = "gravity_compensation"


class InterpolationTechnique(StrEnum):
    linear = "linear"
    minjerk = "minjerk"
    ease_in_out = "ease_in_out"
    cartoon = "cartoon"


class SourceKind(StrEnum):
    hf_space = "hf_space"
    dashboard_selection = "dashboard_selection"
    local = "local"
    installed = "installed"


class HeadPose(BaseModel):
    """Head pose: translation in meters, orientation (roll, pitch, yaw) in radians.

    Mirrors the daemon's `XYZRPYPose`. All fields default to 0 (neutral pose).
    """

    model_config = ConfigDict(extra="forbid")

    x: _METERS = 0.0
    y: _METERS = 0.0
    z: _METERS = 0.0
    roll: _ANGLE = 0.0
    pitch: _ANGLE = 0.0
    yaw: _ANGLE = 0.0


class AudioParam(BaseModel):
    """One XVF3800 parameter write (mirrors `AudioParamPair`)."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(pattern=r"^[A-Z0-9_]{1,64}$", description="XVF3800 parameter name, e.g. AUDIO_MGR_OP_L")]
    values: Annotated[list[float], Field(min_length=1, max_length=64)]

    @field_validator("values")
    @classmethod
    def _finite(cls, v: list[float]) -> list[float]:
        if any(not math.isfinite(x) for x in v):
            raise ValueError("values must be finite numbers")
        return v
