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
    assert f"cloud-helm-{service}:0.6.3" in compose
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


def test_container_entrypoints_are_included_in_docker_context():
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "!deploy/server-entrypoint.sh" in dockerignore
    assert "!deploy/agent-entrypoint.sh" in dockerignore


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


def test_agent_compose_mounts_host_read_only_for_system_metrics():
    compose = (ROOT / "deploy/agent.compose.yml").read_text(encoding="utf-8")
    assert "- /etc/hostname:/host/rootfs-marker:ro" in compose
    assert "- /proc/net/dev:/host/network-dev:ro" not in compose
    assert "network_mode: host" in compose
    assert "ports:" not in compose
    assert "- /proc/stat:/host/proc-stat:ro" in compose
    assert "- /proc/meminfo:/host/meminfo:ro" in compose
    assert "- /proc/loadavg:/host/loadavg:ro" in compose
    assert "- /proc/uptime:/host/uptime:ro" in compose
    assert "- /:/host:ro" not in compose
    environment = (ROOT / "deploy/agent.env.example").read_text(encoding="utf-8")
    assert "CLOUDHELM_AGENT_HOST_ROOT_PATH=/host/rootfs-marker" in environment
    assert "CLOUDHELM_AGENT_HOST_NETWORK_STATS_PATH=/proc/net/dev" in environment
    assert "CLOUDHELM_AGENT_NETWORK_INTERFACES=" in environment
    assert "CLOUDHELM_AGENT_HOST_CPU_STATS_PATH=/host/proc-stat" in environment
    assert "CLOUDHELM_AGENT_HOST_MEMORY_STATS_PATH=/host/meminfo" in environment
    assert "CLOUDHELM_AGENT_HOST_LOAD_STATS_PATH=/host/loadavg" in environment
    assert "CLOUDHELM_AGENT_HOST_UPTIME_STATS_PATH=/host/uptime" in environment
    dockerfile = (ROOT / "deploy/agent.Dockerfile").read_text(encoding="utf-8")
    assert "iproute2" in dockerfile
