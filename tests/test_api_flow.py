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
            "system_metrics_status": "ok",
            "system_metrics": {
                "disk_total_bytes": 1000000000,
                "disk_used_bytes": 400000000,
                "disk_free_bytes": 600000000,
                "network_rx_bytes": 120000,
                "network_tx_bytes": 80000,
                "network_rx_bps": 2048.5,
                "network_tx_bps": 1024.25,
                "network_interfaces": ["ens65f0np0"],
                "network_interface_metrics": [
                    {
                        "name": "ens65f0np0",
                        "addresses": ["192.0.2.10/24"],
                        "rx_bytes": 120000,
                        "tx_bytes": 80000,
                        "rx_bps": 2048.5,
                        "tx_bps": 1024.25,
                    }
                ],
                    "cpu_percent": 36.5,
                    "cpu_count": 32,
                "memory_total_bytes": 34359738368,
                "memory_used_bytes": 12884901888,
                "memory_available_bytes": 21474836480,
                "memory_percent": 37.5,
                "swap_total_bytes": 4294967296,
                "swap_used_bytes": 1073741824,
                "load_1": 1.2,
                "load_5": 0.8,
                "load_15": 0.5,
                "uptime_seconds": 86400,
                "disk_inodes_total": 1000000,
                "disk_inodes_used": 250000,
                "disk_inodes_free": 750000,
            },
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
                    "network_rx_bytes": 50000,
                    "network_tx_bytes": 25000,
                    "network_rx_bps": 512.5,
                    "network_tx_bps": 256.25,
                        "writable_layer_bytes": 1048576,
                        "rootfs_bytes": 52428800,
                        "writable_layer_growth_mibps": 1.5,
                    "block_read_bytes": 2097152,
                    "block_write_bytes": 3145728,
                    "block_read_bps": 1024.5,
                    "block_write_bps": 2048.25,
                    "pids": 12,
                    "restart_count": 2,
                    "oom_killed": False,
                    "health_failing_streak": 0,
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
    assert nodes.json()[0]["gpu_expected_count"] == 2
    assert nodes.json()[0]["system_metrics_status"] == "ok"
    assert nodes.json()[0]["system_metrics"]["disk_used_bytes"] == 400000000
    assert nodes.json()[0]["system_metrics"]["cpu_percent"] == 36.5
    assert nodes.json()[0]["system_metrics"]["cpu_count"] == 32
    assert nodes.json()[0]["system_metrics"]["network_interface_metrics"][0] == {
        "name": "ens65f0np0",
        "addresses": ["192.0.2.10/24"],
        "rx_bytes": 120000,
        "tx_bytes": 80000,
        "rx_bps": 2048.5,
        "tx_bps": 1024.25,
    }
    reset_gpu_baseline = client.post(
        f"/api/v1/nodes/{credentials['node_id']}/gpu-baseline/reset",
        headers=admin_headers,
    )
    assert reset_gpu_baseline.status_code == 200
    assert reset_gpu_baseline.json()["gpu_expected_count"] == 2

    dashboard = client.get("/api/v1/dashboard", headers=admin_headers)
    assert dashboard.status_code == 200
    assert dashboard.json()["gpus"] == {
        "total": 2,
        "active": 2,
        "average_utilization": 30.0,
        "memory_used_mib": 3072,
        "memory_total_mib": 72174,
    }
    assert dashboard.json()["system"]["network_rx_bps"] == 2048.5
    assert dashboard.json()["system"]["disk_total_bytes"] == 1000000000

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
    assert container["network_rx_bps"] == 512.5
    assert container["writable_layer_bytes"] == 1048576
    assert container["writable_layer_growth_mibps"] == 1.5
    assert container["block_write_bps"] == 2048.25
    assert container["pids"] == 12
    assert container["restart_count"] == 2

    node_history = client.get(
        f"/api/v1/nodes/{credentials['node_id']}/metrics/history",
        headers=admin_headers,
    )
    assert node_history.status_code == 200
    assert node_history.json()[0]["cpu_percent"] == 36.5
    container_history = client.get(
        f"/api/v1/containers/{container['id']}/metrics/history",
        headers=admin_headers,
    )
    assert container_history.status_code == 200
    assert container_history.json()[0]["block_write_bps"] == 2048.25

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
    audit = client.get("/api/v1/audit?limit=20", headers=admin_headers)
    log_result_audit = next(
        item for item in audit.json() if item["action"] == "task.logs.result"
    )
    assert log_result_audit["action_label"] == "查看容器日志执行结果"
    assert log_result_audit["detail_label"] == "执行完成"
    assert "service ready" not in log_result_audit["detail"]

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
    assert viewer_nodes.json()[0]["system_metrics_status"] == "restricted"
    assert viewer_nodes.json()[0]["system_metrics"] == {}
    assert (
        client.get(
            f"/api/v1/nodes/{credentials['node_id']}/metrics/history",
            headers=viewer_headers,
        ).status_code
        == 404
    )
    viewer_dashboard = client.get("/api/v1/dashboard", headers=viewer_headers)
    assert viewer_dashboard.json()["gpus"]["total"] == 1
    assert viewer_dashboard.json()["system"]["node_count"] == 0
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
            f"/api/v1/containers/{container['id']}/metrics/history",
            headers=viewer_headers,
        ).status_code
        == 200
    )
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
    assert "renderNodeSystem(node)" in script.text
    assert "writable_layer_bytes" in script.text
    assert "can_manage:level==='manage'" in script.text
    assert "state.user.can_manage_access" in script.text
    assert "/access/preview" in script.text
    assert "expected_version" in script.text
    assert "global_alert_notify" in script.text
    assert "data-alert-notify" in script.text
    assert "node_gpu_temperature_c" in script.text
    assert "/gpu-baseline/reset" in script.text
    assert "/notification/test" in script.text
    assert "action_label" in script.text

    access_alert_style = client.get("/assets/access-alerts.css")
    assert access_alert_style.status_code == 200
    assert ".alert-subscribe" in access_alert_style.text
    assert ".alert-rule-actions" in access_alert_style.text

    style = client.get("/assets/app.css")
    assert style.status_code == 200
    assert ".container-main span{white-space:normal" in style.text
    assert "overflow-wrap:anywhere" in style.text
    assert "@media(max-width:480px)" in style.text
    assert "@media(max-width:390px)" in style.text
    assert "@media(max-width:360px)" in style.text
    assert "word-break:break-word" in style.text
