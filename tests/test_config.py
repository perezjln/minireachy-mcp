import pytest

from minireachy_mcp.config import ConfigError, load_settings


def test_defaults_are_safe() -> None:
    s = load_settings({})
    assert s.enable_physical_actions is False
    assert s.enable_admin_actions is False
    assert s.mcp_auth_token is None
    assert s.base_url == "http://host.docker.internal:8000"
    assert s.mcp_port == 8765 and s.mcp_path == "/mcp"


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("YES", True), ("false", False), ("0", False), ("", False)])
def test_boolean_parsing(raw: str, expected: bool) -> None:
    assert load_settings({"ENABLE_PHYSICAL_ACTIONS": raw}).enable_physical_actions is expected


def test_invalid_boolean_rejected() -> None:
    with pytest.raises(ConfigError, match="ENABLE_ADMIN_ACTIONS"):
        load_settings({"ENABLE_ADMIN_ACTIONS": "maybe"})


def test_full_environment() -> None:
    s = load_settings({
        "MINIREACHY_BASE_URL": "http://192.168.1.20:8000/",
        "MINIREACHY_READ_TIMEOUT": "5",
        "MCP_PORT": "9000",
        "MCP_ALLOWED_HOSTS": "a:*, b:1",
        "LIMIT_HEAD_YAW_RAD": "0.5",
        "LIMIT_MAX_MOVE_DURATION_S": "4",
    })
    assert s.base_url == "http://192.168.1.20:8000"
    assert s.read_timeout_s == 5 and s.mcp_port == 9000
    assert s.mcp_allowed_hosts == ("a:*", "b:1")
    assert s.limits.max_head_yaw_rad == 0.5 and s.limits.max_move_duration_s == 4


@pytest.mark.parametrize(
    "env",
    [
        {"MINIREACHY_BASE_URL": "not a url"},
        {"MCP_PORT": "70000"},
        {"MINIREACHY_READ_TIMEOUT": "-1"},
        {"MCP_PATH": "mcp"},
        {"LIMIT_MIN_MOVE_DURATION_S": "5", "LIMIT_MAX_MOVE_DURATION_S": "1"},
        {"LOG_LEVEL": "LOUD"},
    ],
)
def test_invalid_values_rejected(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_settings(env)


def test_short_token_rejected_without_leaking_it() -> None:
    with pytest.raises(ConfigError) as info:
        load_settings({"MCP_AUTH_TOKEN": "s3cr3t-short"})
    assert "s3cr3t-short" not in str(info.value)


def test_empty_token_means_no_auth() -> None:
    assert load_settings({"MCP_AUTH_TOKEN": ""}).mcp_auth_token is None


def test_speed_defaults() -> None:
    s = load_settings({})
    assert s.listen_silence_ms == 700 and s.listen_early_silence_ms == 300 and s.stt_beam_size == 5
    assert s.stt_vad_filter is False and s.stt_cpu_threads == 8 and s.media_warmup is True
    assert load_settings({"MEDIA_WARMUP": "false", "LISTEN_EARLY_SILENCE_MS": "0"}).media_warmup is False
    assert s.speech_output == "auto" and load_settings({"SPEECH_OUTPUT": "daemon"}).speech_output == "daemon"
