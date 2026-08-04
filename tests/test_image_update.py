from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from cloudhelm.image_reference import parse_tagged_image, validate_tag_change
from cloudhelm_agent.docker_runtime import DockerRuntime


def test_image_reference_requires_the_same_repository_and_a_new_tag():
    current, target = validate_tag_change(
        "nginx:1.25", "docker.io/library/nginx:1.26"
    )
    assert current.canonical_repository == "docker.io/library/nginx"
    assert target.canonical == "docker.io/library/nginx:1.26"

    with pytest.raises(ValueError, match="同一镜像仓库"):
        validate_tag_change("nginx:1.25", "example.com/other/nginx:1.26")
    with pytest.raises(ValueError, match="不能相同"):
        validate_tag_change("registry.example:5000/team/api:v1", "registry.example:5000/team/api:v1")
    with pytest.raises(ValueError, match="digest"):
        parse_tagged_image("nginx@sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="仓库名称无效"):
        parse_tagged_image("nginx;touch-pwned:latest")


def test_agent_recreates_container_and_reuses_runtime_configuration(monkeypatch):
    monkeypatch.setattr("cloudhelm_agent.docker_runtime.time.sleep", lambda _: None)
    runtime = DockerRuntime.__new__(DockerRuntime)
    runtime.client = MagicMock()

    old = MagicMock()
    old.id = "1234567890abcdef"
    old.name = "api"
    old.attrs = {
        "Id": old.id,
        "Name": "/api",
        "Config": {
            "Image": "example/api:1.0",
            "Env": ["MODE=production"],
            "Labels": {
                "com.docker.compose.service": "api",
                "com.docker.compose.image": "sha256:old",
            },
            "Volumes": {"/data": {}},
        },
        "HostConfig": {
            "AutoRemove": False,
            "Binds": ["api-data:/data:rw"],
            "NetworkMode": "project_default",
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "DeviceRequests": [{"Driver": "nvidia", "Count": -1}],
        },
        "Mounts": [
            {
                "Type": "volume",
                "Name": "api-data",
                "Source": "/var/lib/docker/volumes/api-data/_data",
                "Destination": "/data",
                "Mode": "rw",
                "RW": True,
            }
        ],
        "NetworkSettings": {
            "Networks": {
                "project_default": {
                    "IPAMConfig": None,
                    "Aliases": ["api"],
                }
            }
        },
        "State": {"Running": True},
    }
    pulled = MagicMock(id="sha256:new")
    replacement = MagicMock()
    replacement.id = "fedcba0987654321"
    replacement.attrs = {"State": {"Running": True}}
    runtime.client.containers.get.side_effect = [old, replacement]
    runtime.client.images.pull.return_value = pulled
    runtime.client.api.create_container_from_config.return_value = {
        "Id": replacement.id
    }

    result = runtime.execute(
        old.id,
        "update_image",
        {
            "expected_image": "example/api:1.0",
            "target_image": "example/api:1.1",
        },
    )

    assert result == {
        "message": "container image updated to docker.io/example/api:1.1",
        "docker_id": replacement.id,
    }
    runtime.client.images.pull.assert_called_once_with("docker.io/example/api:1.1")
    create_config = runtime.client.api.create_container_from_config.call_args.args[0]
    assert create_config["Image"] == "docker.io/example/api:1.1"
    assert create_config["HostConfig"]["Binds"] == ["api-data:/data:rw"]
    assert create_config["HostConfig"]["DeviceRequests"][0]["Driver"] == "nvidia"
    assert create_config["Labels"]["com.docker.compose.image"] == "sha256:new"
    assert create_config["NetworkingConfig"]["EndpointsConfig"][
        "project_default"
    ]["Aliases"] == ["api"]
    old.stop.assert_called_once_with(timeout=30)
    replacement.start.assert_called_once_with()
    old.remove.assert_called_once_with(force=True, v=False)


def test_agent_rolls_back_old_container_if_replacement_creation_fails(monkeypatch):
    runtime = DockerRuntime.__new__(DockerRuntime)
    runtime.client = MagicMock()
    old = MagicMock()
    old.id = "1234567890abcdef"
    old.name = "api"
    old.attrs = {
        "Name": "/api",
        "Config": {"Image": "example/api:1.0", "Labels": {}},
        "HostConfig": {"AutoRemove": False},
        "NetworkSettings": {"Networks": {}},
        "State": {"Running": True},
    }
    runtime.client.containers.get.return_value = old
    runtime.client.images.pull.return_value = MagicMock(id="sha256:new")
    runtime.client.api.create_container_from_config.side_effect = RuntimeError(
        "create failed"
    )

    with pytest.raises(RuntimeError, match="create failed"):
        runtime.execute(
            old.id,
            "update_image",
            {
                "expected_image": "example/api:1.0",
                "target_image": "example/api:1.1",
            },
        )

    assert old.rename.call_count == 2
    assert old.rename.call_args_list[-1].args == ("api",)
    old.start.assert_called_once_with()


def test_only_admin_can_queue_same_repository_image_update(
    client: TestClient, admin_headers: dict[str, str], session_for
):
    enroll = client.post(
        "/api/v1/agent/enroll",
        json={
            "enrollment_token": "test-agent-enrollment-token",
            "agent_key": "image-update-agent-key",
            "name": "镜像更新节点",
            "hostname": "update-node",
            "environment": "test",
            "agent_version": "0.5.1",
        },
    )
    credentials = enroll.json()
    agent_headers = {
        "X-Node-Id": credentials["node_id"],
        "X-Agent-Token": credentials["node_token"],
    }
    heartbeat = client.post(
        "/api/v1/agent/heartbeat",
        headers=agent_headers,
        json={
            "hostname": "update-node",
            "agent_version": "0.5.1",
            "docker_version": "28.0.0",
            "os": "Linux / x86_64",
            "containers": [
                {
                    "docker_id": "imageupdate123456",
                    "name": "gateway",
                    "image": "nginx:1.25",
                    "status": "running",
                }
            ],
        },
    )
    assert heartbeat.status_code == 204
    container = client.get(
        f"/api/v1/nodes/{credentials['node_id']}/containers",
        headers=admin_headers,
    ).json()[0]
    detail = client.get(
        f"/api/v1/containers/{container['id']}", headers=admin_headers
    ).json()
    assert detail["permissions"]["update_image"] is True

    same_tag = client.post(
        f"/api/v1/containers/{container['id']}/actions",
        headers=admin_headers,
        json={"action": "update_image", "target_image": "nginx:1.25"},
    )
    assert same_tag.status_code == 400
    other_repository = client.post(
        f"/api/v1/containers/{container['id']}/actions",
        headers=admin_headers,
        json={"action": "update_image", "target_image": "caddy:2.9"},
    )
    assert other_repository.status_code == 400

    created_operator = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "wecom_userid": "image.operator",
            "display_name": "镜像运维员",
            "role": "operator",
        },
    ).json()
    client.put(
        f"/api/v1/users/{created_operator['id']}/access",
        headers=admin_headers,
        json={
            "restricted": True,
            "rules": [
                {
                    "scope_type": "container",
                    "node_id": credentials["node_id"],
                    "container_id": container["id"],
                    "can_view": True,
                    "can_logs": True,
                    "can_operate": True,
                }
            ],
        },
    )
    operator_headers = session_for("image.operator")
    forbidden = client.post(
        f"/api/v1/containers/{container['id']}/actions",
        headers=operator_headers,
        json={"action": "update_image", "target_image": "nginx:1.26"},
    )
    assert forbidden.status_code == 403

    admin_headers = session_for("admin-wecom-id")
    queued = client.post(
        f"/api/v1/containers/{container['id']}/actions",
        headers=admin_headers,
        json={"action": "update_image", "target_image": "nginx:1.26"},
    )
    assert queued.status_code == 202
    task = client.get("/api/v1/agent/tasks/next", headers=agent_headers).json()
    assert task["action"] == "update_image"
    assert task["arguments"] == {
        "expected_image": "nginx:1.25",
        "target_image": "docker.io/library/nginx:1.26",
    }

    completed = client.post(
        f"/api/v1/agent/tasks/{task['id']}/result",
        headers=agent_headers,
        json={
            "success": True,
            "result": "container image updated",
            "docker_id": "replacement123456789",
        },
    )
    assert completed.status_code == 204
    updated = client.get(
        f"/api/v1/containers/{container['id']}", headers=admin_headers
    ).json()
    assert updated["image"] == "docker.io/library/nginx:1.26"
    assert updated["docker_id"] == "replacement123456789"
