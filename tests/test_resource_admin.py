from fastapi.testclient import TestClient

from cloudhelm.db import SessionLocal
from cloudhelm.models import Container, Node


def _create_user(
    client: TestClient, admin_headers: dict[str, str], userid: str, role: str
) -> dict:
    response = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "wecom_userid": userid,
            "display_name": userid,
            "role": role,
        },
    )
    assert response.status_code == 201
    return response.json()


def test_global_admin_can_configure_own_alert_subscription(
    client: TestClient, admin_headers: dict[str, str]
):
    users = client.get("/api/v1/users", headers=admin_headers)
    assert users.status_code == 200
    admin = next(item for item in users.json() if item["role"] == "admin")
    assert admin["can_edit_access"] is True
    current = client.get(
        f"/api/v1/users/{admin['id']}/access", headers=admin_headers
    )
    assert current.status_code == 200
    enabled = client.put(
        f"/api/v1/users/{admin['id']}/access",
        headers=admin_headers,
        json={
            "restricted": False,
            "global_alert_notify": True,
            "rules": [],
            "expected_version": current.json()["version"],
        },
    )
    assert enabled.status_code == 200
    assert enabled.json()["global_alert_notify"] is True
    disabled = client.put(
        f"/api/v1/users/{admin['id']}/access",
        headers=admin_headers,
        json={
            "restricted": False,
            "global_alert_notify": False,
            "rules": [],
            "expected_version": enabled.json()["version"],
        },
    )
    assert disabled.status_code == 200
    assert disabled.json()["global_alert_notify"] is False


def test_scoped_manager_can_delegate_only_inside_own_container(
    client: TestClient, admin_headers: dict[str, str], session_for
):
    with SessionLocal() as db:
        node_a = Node(
            agent_key="resource-admin-node-a",
            name="资源管理节点 A",
            environment="resource-admin-test",
            agent_token_hash="hash-a",
        )
        node_b = Node(
            agent_key="resource-admin-node-b",
            name="资源管理节点 B",
            environment="resource-admin-test",
            agent_token_hash="hash-b",
        )
        db.add_all([node_a, node_b])
        db.flush()
        container_a = Container(
            node_id=node_a.id,
            docker_id="resource-admin-container-a",
            name="容器 A",
            image="example/a:1",
        )
        container_b = Container(
            node_id=node_b.id,
            docker_id="resource-admin-container-b",
            name="容器 B",
            image="example/b:1",
        )
        db.add_all([container_a, container_b])
        db.commit()
        node_a_id = node_a.id
        node_b_id = node_b.id
        container_a_id = container_a.id
        container_b_id = container_b.id

    manager = _create_user(
        client, admin_headers, "resource.manager", "operator"
    )
    target = _create_user(client, admin_headers, "resource.target", "operator")
    viewer = _create_user(client, admin_headers, "resource.viewer", "viewer")

    manager_grant = client.put(
        f"/api/v1/users/{manager['id']}/access",
        headers=admin_headers,
        json={
            "restricted": True,
            "rules": [
                {
                    "scope_type": "node",
                    "node_id": node_a_id,
                    "can_manage": True,
                }
            ],
        },
    )
    assert manager_grant.status_code == 200
    target_other_grant = client.put(
        f"/api/v1/users/{target['id']}/access",
        headers=admin_headers,
        json={
            "restricted": True,
            "rules": [
                {
                    "scope_type": "container",
                    "node_id": node_b_id,
                    "container_id": container_b_id,
                    "can_manage": True,
                }
            ],
        },
    )
    assert target_other_grant.status_code == 200

    manager_headers = session_for("resource.manager")
    me = client.get("/api/v1/auth/me", headers=manager_headers)
    assert me.status_code == 200
    assert me.json()["can_manage_access"] is True

    resources = client.get("/api/v1/access/resources", headers=manager_headers)
    assert resources.status_code == 200
    assert resources.json()["partial"] is True
    assert resources.json()["allow_unrestricted"] is False
    assert [node["id"] for node in resources.json()["nodes"]] == [node_a_id]
    assert resources.json()["nodes"][0]["containers"][0]["id"] == container_a_id

    users = client.get("/api/v1/users", headers=manager_headers)
    assert users.status_code == 200
    target_summary = next(
        item for item in users.json() if item["id"] == target["id"]
    )
    assert target_summary["can_edit_access"] is True
    assert target_summary["can_edit_account"] is False

    delegated = client.put(
        f"/api/v1/users/{target['id']}/access",
        headers=manager_headers,
        json={
            "restricted": True,
            "rules": [
                {
                    "scope_type": "container",
                    "node_id": node_a_id,
                    "container_id": container_a_id,
                    "can_manage": True,
                }
            ],
        },
    )
    assert delegated.status_code == 200
    assert delegated.json()["partial"] is True

    outside = client.put(
        f"/api/v1/users/{target['id']}/access",
        headers=manager_headers,
        json={
            "restricted": True,
            "rules": [
                {
                    "scope_type": "container",
                    "node_id": node_b_id,
                    "container_id": container_b_id,
                    "can_manage": True,
                }
            ],
        },
    )
    assert outside.status_code == 403
    assert (
        client.put(
            f"/api/v1/users/{target['id']}/access",
            headers=manager_headers,
            json={"restricted": False, "rules": []},
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/v1/users/{viewer['id']}/access",
            headers=manager_headers,
            json={
                "restricted": True,
                "rules": [
                    {
                        "scope_type": "container",
                        "node_id": node_a_id,
                        "container_id": container_a_id,
                        "can_manage": True,
                    }
                ],
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/users",
            headers=manager_headers,
            json={
                "wecom_userid": "not.allowed",
                "display_name": "Not Allowed",
                "role": "viewer",
            },
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/users/{target['id']}",
            headers=manager_headers,
            json={"is_active": False},
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/users/{manager['id']}/access",
            headers=manager_headers,
        ).status_code
        == 403
    )

    admin_headers = session_for("admin-wecom-id")
    admin_view = client.get(
        f"/api/v1/users/{target['id']}/access", headers=admin_headers
    )
    assert admin_view.status_code == 200
    scopes = {
        (rule["node_id"], rule["container_id"])
        for rule in admin_view.json()["rules"]
    }
    assert scopes == {
        (node_a_id, container_a_id),
        (node_b_id, container_b_id),
    }

    target_headers = session_for("resource.target")
    cannot_expand_to_node = client.put(
        f"/api/v1/users/{viewer['id']}/access",
        headers=target_headers,
        json={
            "restricted": True,
            "rules": [
                {
                    "scope_type": "node",
                    "node_id": node_a_id,
                    "can_view": True,
                }
            ],
        },
    )
    assert cannot_expand_to_node.status_code == 403
    allowed = client.get(
        f"/api/v1/containers/{container_a_id}", headers=target_headers
    )
    assert allowed.status_code == 200
    assert allowed.json()["permissions"]["manage"] is True
    other = client.get(
        f"/api/v1/containers/{container_b_id}", headers=target_headers
    )
    assert other.status_code == 200
    assert other.json()["permissions"]["manage"] is True


def test_access_preview_lock_effective_managers_and_emergency_revoke(
    client: TestClient, admin_headers: dict[str, str], session_for
):
    with SessionLocal() as db:
        node = Node(
            agent_key="access-safety-node",
            name="权限安全节点",
            environment="access-safety",
            agent_token_hash="hash",
        )
        db.add(node)
        db.flush()
        container = Container(
            node_id=node.id,
            docker_id="access-safety-container",
            name="权限安全容器",
            image="example/safety:1",
        )
        db.add(container)
        db.commit()
        node_id, container_id = node.id, container.id

    manager = _create_user(client, admin_headers, "safety.manager", "operator")
    target = _create_user(client, admin_headers, "safety.target", "operator")
    assert client.put(
        f"/api/v1/users/{manager['id']}/access",
        headers=admin_headers,
        json={
            "restricted": True,
            "rules": [
                {"scope_type": "node", "node_id": node_id, "can_manage": True}
            ],
        },
    ).status_code == 200

    manager_headers = session_for("safety.manager")
    initial = client.get(
        f"/api/v1/users/{target['id']}/access", headers=manager_headers
    ).json()
    payload = {
        "restricted": True,
        "expected_version": initial["version"],
        "rules": [
            {
                "scope_type": "container",
                "node_id": node_id,
                "container_id": container_id,
                "can_manage": True,
                "alert_notify": True,
            }
        ],
    }
    preview = client.post(
        f"/api/v1/users/{target['id']}/access/preview",
        headers=manager_headers,
        json=payload,
    )
    assert preview.status_code == 200
    assert preview.json()["summary"] == {
        "added": 1,
        "removed": 0,
        "changed": 0,
        "management_elevations": 1,
        "notification_changes": 1,
    }
    saved = client.put(
        f"/api/v1/users/{target['id']}/access",
        headers=manager_headers,
        json=payload,
    )
    assert saved.status_code == 200
    assert saved.json()["version"] == initial["version"] + 1
    configured = client.get(
        f"/api/v1/users/{target['id']}/access", headers=manager_headers
    )
    assert configured.status_code == 200
    assert configured.json()["rules"][0]["alert_notify"] is True
    stale = client.put(
        f"/api/v1/users/{target['id']}/access",
        headers=manager_headers,
        json=payload,
    )
    assert stale.status_code == 409

    effective = client.get(
        f"/api/v1/users/{target['id']}/access/effective",
        headers=manager_headers,
    )
    assert effective.status_code == 200
    container_access = next(
        item
        for item in effective.json()["resources"]
        if item["resource_id"] == container_id
    )
    assert container_access["permissions"] == {
        "view": True,
        "logs": True,
        "operate": True,
        "manage": True,
    }
    assert container_access["sources"]

    managers = client.get(
        f"/api/v1/access/managers?node_id={node_id}&container_id={container_id}",
        headers=manager_headers,
    )
    assert managers.status_code == 200
    manager_ids = {item["user_id"] for item in managers.json()["managers"]}
    assert {manager["id"], target["id"]}.issubset(manager_ids)

    admin_headers = session_for("admin-wecom-id")
    revoked = client.delete(
        f"/api/v1/users/{target['id']}/access/management",
        headers=admin_headers,
    )
    assert revoked.status_code == 204
    after = client.get(
        f"/api/v1/users/{target['id']}/access/effective",
        headers=admin_headers,
    ).json()
    container_access = next(
        item for item in after["resources"] if item["resource_id"] == container_id
    )
    assert container_access["permissions"]["manage"] is False
    assert container_access["permissions"]["operate"] is True
