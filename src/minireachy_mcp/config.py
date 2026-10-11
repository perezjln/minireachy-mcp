"""Configuration loaded from environment variables.

Every value has a safe default: physical and administrative actions are disabled
unless explicitly enabled.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator, model_validator

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(ValueError):
    """Raised when the environment contains an invalid configuration."""


class MotionLimits(BaseModel):
    """Conservative guard rails applied by the MCP server *before* calling the daemon.

    These are NOT hardware specifications of the robot. The daemon's OpenAPI schema
    does not document joint or pose limits, so these defaults are deliberately small
    and can be tuned through environment variables.
    """

    model_config = ConfigDict(frozen=True)

    max_head_translation_m: float = Field(0.03, gt=0, le=0.2)
    max_head_roll_rad: float = Field(0.6, gt=0, le=3.2)
    max_head_pitch_rad: float = Field(0.6, gt=0, le=3.2)
    max_head_yaw_rad: float = Field(1.2, gt=0, le=3.2)
    max_body_yaw_rad: float = Field(1.5, gt=0, le=3.2)
    max_antenna_rad: float = Field(3.0, gt=0, le=6.3)
    min_move_duration_s: float = Field(0.3, gt=0, le=10)
    max_move_duration_s: float = Field(10.0, gt=0, le=120)

    @model_validator(mode="after")
    def _check_durations(self) -> MotionLimits:
        if self.min_move_duration_s > self.max_move_duration_s:
            raise ValueError("min_move_duration_s must be <= max_move_duration_s")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Daemon (runs natively on the host, never in this container)
    minireachy_base_url: HttpUrl = HttpUrl("http://host.docker.internal:8000")
    connect_timeout_s: float = Field(3.0, gt=0, le=60)
    read_timeout_s: float = Field(15.0, gt=0, le=600)
    max_response_bytes: int = Field(2_000_000, ge=1024, le=50_000_000)
    max_app_list_bytes: int = Field(10_000_000, ge=1024, le=100_000_000)
    get_retries: int = Field(1, ge=0, le=5)

    # MCP server (runs in the container)
    mcp_host: str = "0.0.0.0"
    mcp_port: int = Field(8765, ge=1, le=65535)
    mcp_path: str = "/mcp"
    mcp_auth_token: str | None = None
    mcp_allowed_hosts: tuple[str, ...] = ("127.0.0.1:*", "localhost:*", "[::1]:*")
    mcp_allowed_origins: tuple[str, ...] = ("http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Safety gates
    enable_physical_actions: bool = False
    enable_admin_actions: bool = False
    # Camera images and microphone listening (privacy). Default: refused.
    enable_perception: bool = False
    max_sound_upload_bytes: int = Field(5_000_000, ge=1024, le=50_000_000)
    limits: MotionLimits = MotionLimits()

    # Media (WebRTC stream of the daemon's GStreamer media server) and local speech models
    media_signalling_url: str = "ws://host.docker.internal:8443"
    media_producer_name: str = "reachymini"
    media_connect_timeout_s: float = Field(25.0, gt=0, le=120)  # room for one automatic retry
    stt_model_path: str = "/opt/models/whisper"
    stt_language: str = Field("fr", pattern=r"^[a-z]{2}$")
    stt_compute_type: Literal["int8", "int8_float32", "float32"] = "int8"
    stt_beam_size: int = Field(5, ge=1, le=10)  # measured on the robot mic: beam 5 fixes short answers for +0.02 s
    stt_cpu_threads: int = Field(8, ge=0, le=64)  # 0 = library default; 8 measured ~10% faster
    # Whisper's own Silero VAD: redundant (listen already segments speech) and costs time.
    stt_vad_filter: bool = False
    stt_initial_prompt: str | None = "Conversation en français avec Reachy, un petit robot."
    tts_voice_path: str = "/opt/models/piper/fr_FR-siwis-medium.onnx"
    tts_length_scale: float = Field(1.0, gt=0.3, le=3.0)
    # Listening defaults (overridable per call)
    listen_silence_ms: int = Field(700, ge=300, le=3000)
    listen_early_silence_ms: int = Field(300, ge=0, le=2000)  # 0 disables speculative STT
    # Load speech models and open the media stream in the background at startup
    media_warmup: bool = True
    # Where speak/converse send the voice: "webrtc" = streamed on the media link (fast),
    # "daemon" = upload + /api/media/play_sound, "auto" = webrtc when perception is
    # enabled and the link accepts audio, else daemon.
    speech_output: Literal["auto", "webrtc", "daemon"] = "auto"

    @field_validator("media_signalling_url")
    @classmethod
    def _ws(cls, v: str) -> str:
        if not v.startswith(("ws://", "wss://")):
            raise ValueError("MEDIA_SIGNALLING_URL must start with ws:// or wss://")
        return v

    @field_validator("mcp_path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not v.startswith("/") or v in ("/healthz",):
            raise ValueError("MCP_PATH must start with '/' and must not collide with /healthz")
        return v.rstrip("/") or "/"

    @field_validator("mcp_auth_token")
    @classmethod
    def _token(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if len(v) < 24:
            raise ValueError("MCP_AUTH_TOKEN must be at least 24 characters long")
        return v

    @property
    def base_url(self) -> str:
        return str(self.minireachy_base_url).rstrip("/")


def _parse_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}")


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


_SCALARS = {
    "MINIREACHY_BASE_URL": "minireachy_base_url",
    "MINIREACHY_CONNECT_TIMEOUT": "connect_timeout_s",
    "MINIREACHY_READ_TIMEOUT": "read_timeout_s",
    "MINIREACHY_MAX_RESPONSE_BYTES": "max_response_bytes",
    "MINIREACHY_MAX_APP_LIST_BYTES": "max_app_list_bytes",
    "MINIREACHY_GET_RETRIES": "get_retries",
    "MCP_HOST": "mcp_host",
    "MCP_PORT": "mcp_port",
    "MCP_PATH": "mcp_path",
    "MCP_AUTH_TOKEN": "mcp_auth_token",
    "LOG_LEVEL": "log_level",
    "MAX_SOUND_UPLOAD_BYTES": "max_sound_upload_bytes",
    "MEDIA_SIGNALLING_URL": "media_signalling_url",
    "MEDIA_PRODUCER_NAME": "media_producer_name",
    "MEDIA_CONNECT_TIMEOUT": "media_connect_timeout_s",
    "STT_MODEL_PATH": "stt_model_path",
    "STT_LANGUAGE": "stt_language",
    "STT_COMPUTE_TYPE": "stt_compute_type",
    "STT_BEAM_SIZE": "stt_beam_size",
    "STT_CPU_THREADS": "stt_cpu_threads",
    "STT_INITIAL_PROMPT": "stt_initial_prompt",
    "TTS_VOICE_PATH": "tts_voice_path",
    "TTS_LENGTH_SCALE": "tts_length_scale",
    "LISTEN_SILENCE_MS": "listen_silence_ms",
    "LISTEN_EARLY_SILENCE_MS": "listen_early_silence_ms",
    "SPEECH_OUTPUT": "speech_output",
}
_BOOLS = {
    "ENABLE_PHYSICAL_ACTIONS": "enable_physical_actions",
    "ENABLE_ADMIN_ACTIONS": "enable_admin_actions",
    "ENABLE_PERCEPTION": "enable_perception",
    "STT_VAD_FILTER": "stt_vad_filter",
    "MEDIA_WARMUP": "media_warmup",
}
_LISTS = {
    "MCP_ALLOWED_HOSTS": "mcp_allowed_hosts",
    "MCP_ALLOWED_ORIGINS": "mcp_allowed_origins",
}
_LIMITS = {
    "LIMIT_HEAD_TRANSLATION_M": "max_head_translation_m",
    "LIMIT_HEAD_ROLL_RAD": "max_head_roll_rad",
    "LIMIT_HEAD_PITCH_RAD": "max_head_pitch_rad",
    "LIMIT_HEAD_YAW_RAD": "max_head_yaw_rad",
    "LIMIT_BODY_YAW_RAD": "max_body_yaw_rad",
    "LIMIT_ANTENNA_RAD": "max_antenna_rad",
    "LIMIT_MIN_MOVE_DURATION_S": "min_move_duration_s",
    "LIMIT_MAX_MOVE_DURATION_S": "max_move_duration_s",
}


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build `Settings` from environment variables (defaults to `os.environ`)."""
    env = os.environ if env is None else env
    data: dict[str, object] = {}
    for key, field in _SCALARS.items():
        if key in env:
            data[field] = env[key]
    for key, field in _BOOLS.items():
        if key in env:
            data[field] = _parse_bool(key, env[key])
    for key, field in _LISTS.items():
        if key in env:
            data[field] = _csv(env[key])
    limits = {field: env[key] for key, field in _LIMITS.items() if key in env}
    try:
        if limits:
            data["limits"] = MotionLimits(**limits)  # type: ignore[arg-type]
        return Settings(**data)  # type: ignore[arg-type]
    except ValidationError as exc:
        # Never echo input values: MCP_AUTH_TOKEN could be among them.
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ConfigError(f"Invalid configuration: {problems}") from None
