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
    assert f"cloud-helm-{service}:1.0.0" in compose
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


def test_desktop_stylesheet_is_packaged_and_linked_after_module_styles():
    page = (ROOT / "cloudhelm/static/index.html").read_text(encoding="utf-8")
    desktop_asset = "/assets/desktop.css"
    assert desktop_asset in page
    assert page.index(desktop_asset) > page.index("/assets/scaling.css")

    desktop = (ROOT / "cloudhelm/static/assets/desktop.css").read_text(encoding="utf-8")
    app = (ROOT / "cloudhelm/static/assets/app.css").read_text(encoding="utf-8")
    assert "@media (min-width: 960px)" in desktop
    assert "@media (min-width: 960px)" in app
    assert "--desktop-rail" in app


def test_management_and_scaling_settings_are_split_into_tabs():
    page = (ROOT / "cloudhelm/static/index.html").read_text(encoding="utf-8")
    script = (ROOT / "cloudhelm/static/assets/app.js").read_text(encoding="utf-8")

    for tab in ("account", "users", "alerts", "scaling"):
        assert f'data-profile-tab="{tab}"' in page
        assert f'data-profile-panel="{tab}"' in page
    for tab in ("summary", "instances", "policy", "release"):
        assert f'data-scaling-tab="{tab}"' in script
    assert "gpu_names" in script
    assert "实例业务上限已保存" in script
    assert "data-delete-instance" in script
    assert "确认删除实例" in script
    assert "businessHistoryCard" in script
    assert "AI 业务服务" in script
    assert "AI 服务发布" in script
    assert "data-service-id" in script
    assert "service_id:row.dataset.serviceId" in script
    container_history = script[
        script.index("async function renderContainerHistory") : script.index(
            "function gpuAllocation"
        )
    ]
    assert "businessHistoryCard(samples)" in container_history


def test_container_logs_are_rendered_in_the_viewing_device_timezone():
    page = (ROOT / "cloudhelm/static/index.html").read_text(encoding="utf-8")
    script = (ROOT / "cloudhelm/static/assets/app.js").read_text(encoding="utf-8")
    mini_format = (ROOT / "miniprogram/utils/format.js").read_text(encoding="utf-8")
    mini_container = (ROOT / "miniprogram/pages/container/index.js").read_text(
        encoding="utf-8"
    )

    assert "app.js?v=1.0.0" in page
    assert "formatLogsForDevice(result.result)" in script
    assert "getTimezoneOffset()" in script
    assert "formatDeviceTime(c.finished_at)" in script
    assert "function logs(value)" in mini_format
    assert "getTimezoneOffset()" in mini_format
    assert "format.logs(task.result)" in mini_container


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


def test_automatic_tag_workflow_explicitly_dispatches_release():
    workflow = (ROOT / ".github/workflows/tag-release.yml").read_text(encoding="utf-8")
    assert "actions: write" in workflow
    assert "gh workflow run release.yml" in workflow
    assert '--field "release_tag=$tag"' in workflow
