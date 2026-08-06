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
