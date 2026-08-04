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
            "agent_version": "0.3.0",
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
            "agent_version": "0.3.0",
            "docker_version": "28.0.0",
            "os": "Linux / x86_64",
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
                },
            ],
        },
    )
    assert heartbeat.status_code == 204

    nodes = client.get("/api/v1/nodes", headers=admin_headers)
    assert nodes.status_code == 200
    assert nodes.json()[0]["online"] is True
    assert nodes.json()[0]["container_count"] == 2

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
