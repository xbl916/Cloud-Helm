import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "relative_path,service",
    [
        ("docker-compose.yml", "server"),
        ("deploy/postgres.compose.yml", "server"),
        ("deploy/agent.compose.yml", "agent"),
    ],
)
def test_compose_uses_main_container_initialization(relative_path, service):
    compose = (ROOT / relative_path).read_text(encoding="utf-8")
    assert "\n  data-init:" not in compose
    assert "condition: service_completed_successfully" not in compose
    assert f"cloud-helm-{service}:0.5.1" in compose
    assert "- CHOWN" in compose
    assert "- FOWNER" in compose
    assert "- DAC_OVERRIDE" in compose
    assert "- SETPCAP" in compose


@pytest.mark.parametrize(
    "script", ["deploy/server-entrypoint.sh", "deploy/agent-entrypoint.sh"]
)
def test_container_entrypoints_are_valid_shell(script):
    completed = subprocess.run(
        ["sh", "-n", str(ROOT / script)], check=False, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr


def test_server_entrypoint_drops_identity_and_all_capabilities():
    script = (ROOT / "deploy/server-entrypoint.sh").read_text(encoding="utf-8")
    assert "chown -R 10001:10001 /data" in script
    assert "--reuid=10001" in script
    assert "--regid=10001" in script
    assert "--bounding-set=-all" in script


def test_agent_entrypoint_drops_all_capabilities():
    script = (ROOT / "deploy/agent-entrypoint.sh").read_text(encoding="utf-8")
    assert "chown -R 0:0 /data" in script
    assert "--bounding-set=-all" in script
