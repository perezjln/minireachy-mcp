"""Static checks of the Docker / Compose configuration (the real build is run by `docker compose`)."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text("utf-8"))


def test_mcp_port_published_on_loopback_only() -> None:
    svc = _compose()["services"]["minireachy-mcp"]
    assert svc["ports"] == ["127.0.0.1:${MCP_PUBLISH_PORT:-8765}:8765"]


def test_no_privileged_no_docker_socket_no_host_network() -> None:
    for name, svc in _compose()["services"].items():
        assert not svc.get("privileged"), name
        assert svc.get("network_mode") != "host", name
        for vol in svc.get("volumes", []):
            assert "docker.sock" not in str(vol), name


def test_service_hardening_and_policies() -> None:
    svc = _compose()["services"]["minireachy-mcp"]
    assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert svc["restart"] == "unless-stopped"
    assert "host.docker.internal:host-gateway" in svc["extra_hosts"]
    assert svc["environment"]["ENABLE_PHYSICAL_ACTIONS"] == "${ENABLE_PHYSICAL_ACTIONS:-false}"
    assert svc["logging"]["driver"] == "json-file"


def test_tests_service_has_no_network() -> None:
    assert _compose()["services"]["tests"]["network_mode"] == "none"


def test_dockerfile_runs_as_non_root_with_healthcheck() -> None:
    text = (ROOT / "Dockerfile").read_text("utf-8")
    runtime = text.split("AS runtime", 1)[1]
    assert "USER mcp" in runtime
    assert "HEALTHCHECK" in runtime and "/healthz" in runtime
    assert 'CMD ["python", "-m", "minireachy_mcp"]' in runtime
    assert "reachy_mini" not in text.lower().replace("minireachy", "")  # no daemon installed in the image
