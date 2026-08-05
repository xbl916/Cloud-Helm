from fastapi.testclient import TestClient


def test_agent_inventory_and_task_flow(
    client: TestClient, admin_headers: dict[str, str], session_for
):
    enroll = client.post(
        "/api/v1/agent/enroll",
        json={
            "enrollment_token": "test-agent-enrollment-token",
            "agent_key": "stable-agent-key-0001",
            "name": "生产节点一",
            "hostname": "prod-01",
            "environment": "production",
            "agent_version": "0.5.2",
        },
    )
    assert enroll.status_code == 200
    credentials = enroll.json()
    agent_headers = {
        "X-Node-Id": credentials["node_id"],
        "X-Agent-Token": credentials["node_token"],
    }

    heartbeat = client.post(
        "/api/v1/agent/heartbeat",
        headers=agent_headers,
        json={
            "hostname": "prod-01",
            "agent_version": "0.5.2",
            "docker_version": "28.0.0",
            "os": "Linux / x86_64",
            "gpu_status": "ok",
            "gpus": [
                {
                    "index": 0,
                    "uuid": "GPU-12345678",
                    "name": "NVIDIA RTX 6000 Ada Generation",
                    "driver_version": "570.124.06",
                    "cuda_version": "12.8",
                    "utilization_gpu": 40.0,
                    "utilization_memory": 10.0,
                    "memory_used_mib": 2048,
                    "memory_total_mib": 49140,
                    "temperature_c": 54.0,
                    "power_draw_w": 112.45,
                    "power_limit_w": 300.0,
                },
                {
                    "index": 1,
                    "uuid": "GPU-hidden",
                    "name": "NVIDIA L4",
                    "driver_version": "570.124.06",
                    "cuda_version": "12.8",
                    "utilization_gpu": 20.0,
                    "memory_used_mib": 1024,
                    "memory_total_mib": 23034,
                },
            ],
            "containers": [
                {
                    "docker_id": "1234567890abcdef",
                    "name": "web-api",
                    "image": "example/api:1.0",
                    "status": "running",
                    "compose_project": "web",
                    "compose_service": "api",
                    "cpu_percent": 2.5,
                    "memory_usage": 104857600,
                    "memory_limit": 536870912,
                    "memory_percent": 19.53,
                    "gpu_devices": ["0"],
                },
                {
                    "docker_id": "abcdef1234567890",
                    "name": "internal-db",
                    "image": "postgres:17",
                    "status": "running",
                    "compose_project": "database",
                    "compose_service": "postgres",
                    "cpu_percent": 1.2,
                    "memory_usage": 209715200,
                    "memory_limit": 1073741824,
                    "memory_percent": 19.53,
                    "gpu_devices": ["1"],
                },
            ],
        },
    )
    assert heartbeat.status_code == 204

    nodes = client.get("/api/v1/nodes", headers=admin_headers)
    assert nodes.status_code == 200
    assert nodes.json()[0]["online"] is True
    assert nodes.json()[0]["container_count"] == 2
    assert nodes.json()[0]["gpu_status"] == "ok"
    assert nodes.json()[0]["gpus"][0]["uuid"] == "GPU-12345678"

    dashboard = client.get("/api/v1/dashboard", headers=admin_headers)
    assert dashboard.status_code == 200
    assert dashboard.json()["gpus"] == {
        "total": 2,
        "active": 2,
        "average_utilization": 30.0,
        "memory_used_mib": 3072,
        "memory_total_mib": 72174,
    }

    containers = client.get(
        f"/api/v1/nodes/{credentials['node_id']}/containers", headers=admin_headers
    )
    assert containers.status_code == 200
    assert len(containers.json()) == 2
    container = next(item for item in containers.json() if item["name"] == "web-api")
    hidden_container = next(
        item for item in containers.json() if item["name"] == "internal-db"
    )
    assert container["compose_project"] == "web"
    assert container["gpu_devices"] == ["0"]
    assert container["gpu_all"] is False

    queued = client.post(
        f"/api/v1/containers/{container['id']}/actions",
        headers=admin_headers,
        json={"action": "logs", "tail": 100},
    )
    assert queued.status_code == 202
    task_id = queued.json()["id"]

    task = client.get("/api/v1/agent/tasks/next", headers=agent_headers)
    assert task.status_code == 200
    assert task.json()["id"] == task_id
    assert task.json()["action"] == "logs"

    result = client.post(
        f"/api/v1/agent/tasks/{task_id}/result",
        headers=agent_headers,
        json={"success": True, "result": "2026-08-04 service ready"},
    )
    assert result.status_code == 204

    completed = client.get(f"/api/v1/tasks/{task_id}", headers=admin_headers)
    assert completed.status_code == 200
    assert completed.json()["status"] == "success"
    assert "service ready" in completed.json()["result"]

    assert (
        client.get("/api/v1/agent/tasks/next", headers=agent_headers).status_code == 204
    )

    created_user = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "wecom_userid": "viewer.one",
            "display_name": "只读用户",
            "role": "viewer",
        },
    )
    assert created_user.status_code == 201
    viewer_id = created_user.json()["id"]
    configured = client.put(
        f"/api/v1/users/{viewer_id}/access",
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
                    "can_operate": False,
                }
            ],
        },
    )
    assert configured.status_code == 200
    viewer_headers = session_for("viewer.one")
    viewer_nodes = client.get("/api/v1/nodes", headers=viewer_headers)
    assert viewer_nodes.status_code == 200
    assert viewer_nodes.json()[0]["container_count"] == 1
    assert viewer_nodes.json()[0]["gpu_status"] == "ok"
    assert [gpu["uuid"] for gpu in viewer_nodes.json()[0]["gpus"]] == ["GPU-12345678"]
    viewer_dashboard = client.get("/api/v1/dashboard", headers=viewer_headers)
    assert viewer_dashboard.json()["gpus"]["total"] == 1
    viewer_container = client.get(
        f"/api/v1/containers/{container['id']}", headers=viewer_headers
    )
    assert [gpu["uuid"] for gpu in viewer_container.json()["assigned_gpus"]] == [
        "GPU-12345678"
    ]
    viewer_containers = client.get(
        f"/api/v1/nodes/{credentials['node_id']}/containers", headers=viewer_headers
    )
    assert [item["name"] for item in viewer_containers.json()] == ["web-api"]
    assert (
        client.get(
            f"/api/v1/containers/{hidden_container['id']}", headers=viewer_headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/containers/{hidden_container['id']}/actions",
            headers=viewer_headers,
            json={"action": "logs", "tail": 50},
        ).status_code
        == 404
    )
    logs_allowed = client.post(
        f"/api/v1/containers/{container['id']}/actions",
        headers=viewer_headers,
        json={"action": "logs", "tail": 50},
    )
    assert logs_allowed.status_code == 202
    forbidden = client.post(
        f"/api/v1/containers/{container['id']}/actions",
        headers=viewer_headers,
        json={"action": "restart"},
    )
    assert forbidden.status_code == 403
    assert (
        client.get(f"/api/v1/tasks/{task_id}", headers=viewer_headers).status_code
        == 404
    )
    assert (
        client.get("/api/v1/access/resources", headers=viewer_headers).status_code
        == 403
    )


def test_auth_required(client: TestClient):
    response = client.get("/api/v1/nodes")
    assert response.status_code == 401


def test_health_and_frontend(client: TestClient):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/healthz", headers={"Host": "evil.example"}).status_code == 400
    page = client.get("/")
    assert page.status_code == 200
    assert "云舵" in page.text
    assert 'id="new-user-access-mode"' in page.text
    assert "下一步：绑定容器" in page.text

    script = client.get("/assets/app.js")
    assert script.status_code == 200
    assert "await openAccess(created.id)" in script.text
    assert "restricted:false,rules:[]" in script.text

    style = client.get("/assets/app.css")
    assert style.status_code == 200
    assert ".container-main span{white-space:normal" in style.text
    assert "overflow-wrap:anywhere" in style.text
