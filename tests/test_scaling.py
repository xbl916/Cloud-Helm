from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from cloudhelm.autoscaling import evaluate_service
from cloudhelm.config import get_settings
from cloudhelm.db import SessionLocal
from cloudhelm.models import (
    AccessRule,
    BusinessMetricSample,
    Container,
    ScalingEvent,
    ScalingService,
    ServiceInstance,
    User,
    UserRole,
)
from cloudhelm.security import digest_token


def _payload(name: str) -> dict:
    return {
        "name": name,
        "service_type": "vlm",
        "capacity_per_replica": 10,
        "autoscaling_enabled": False,
        "min_replicas": 0,
        "max_replicas": 5,
        "scale_up_threshold_percent": 70,
        "scale_down_threshold_percent": 40,
        "window_seconds": 300,
        "cooldown_seconds": 300,
        "report_stale_seconds": 60,
        "scale_up_step": 1,
        "failover_replicas": 2,
        "provider": "acs_kubernetes",
        "namespace": "inference",
        "workload_kind": "Deployment",
        "workload_name": "vlm-burst",
    }


def test_business_service_report_and_instance_capacity(
    client, admin_headers: dict[str, str]
):
    name = f"VLM-{uuid4()}"
    created = client.post(
        "/api/v1/scaling/services",
        headers=admin_headers,
        json=_payload(name),
    )
    assert created.status_code == 201
    service = created.json()
    assert service["report_token"]

    denied = client.post(
        f"/api/v1/service-metrics/{service['id']}/report",
        headers={"X-CloudHelm-Service-Token": "wrong-token"},
        json={"instance_id": "local-vlm-1", "active_businesses": 7},
    )
    assert denied.status_code == 401

    reported = client.post(
        f"/api/v1/service-metrics/{service['id']}/report",
        headers={"X-CloudHelm-Service-Token": service["report_token"]},
        json={
            "instance_id": "local-vlm-1",
            "pool": "local",
            "active_businesses": 7,
            "healthy": True,
            "ready": True,
            "accepting": True,
            "metadata": {"model": "example-vlm"},
        },
    )
    assert reported.status_code == 200
    assert reported.json()["desired_state"] == "serve"

    listed = client.get("/api/v1/scaling/services")
    target = next(item for item in listed.json() if item["id"] == service["id"])
    instance = target["instances"][0]
    assert instance["pool"] == "local"
    assert instance["active_businesses"] == 7
    assert instance["effective_capacity"] == 10

    changed = client.put(
        f"/api/v1/scaling/instances/{instance['id']}/capacity",
        headers=admin_headers,
        json={"capacity_limit": 16},
    )
    assert changed.status_code == 200
    assert changed.json()["effective_capacity"] == 16

    ignored = client.put(
        f"/api/v1/scaling/instances/{instance['id']}/monitoring",
        headers=admin_headers,
        json={"monitored": False},
    )
    assert ignored.status_code == 200
    assert ignored.json()["monitored"] is False

    deleted = client.delete(
        f"/api/v1/scaling/instances/{instance['id']}", headers=admin_headers
    )
    assert deleted.status_code == 204
    listed_after_delete = client.get("/api/v1/scaling/services")
    target_after_delete = next(
        item for item in listed_after_delete.json() if item["id"] == service["id"]
    )
    assert target_after_delete["instances"] == []


def test_local_instance_manual_maintenance_waits_for_drain_and_healthy_restore(
    client, admin_headers: dict[str, str]
):
    created = client.post(
        "/api/v1/scaling/services",
        headers=admin_headers,
        json=_payload(f"VLM-maintenance-{uuid4()}"),
    ).json()
    report_headers = {"X-CloudHelm-Service-Token": created["report_token"]}
    report_url = f"/api/v1/service-metrics/{created['id']}/report"
    report = client.post(
        report_url,
        headers=report_headers,
        json={
            "instance_id": "local-maintenance-1",
            "pool": "local",
            "active_businesses": 2,
            "healthy": True,
            "ready": True,
            "accepting": True,
        },
    )
    assert report.status_code == 200
    service = next(
        item
        for item in client.get("/api/v1/scaling/services").json()
        if item["id"] == created["id"]
    )
    instance = service["instances"][0]

    entered = client.post(
        f"/api/v1/scaling/instances/{instance['id']}/maintenance",
        headers=admin_headers,
    )
    assert entered.status_code == 200
    assert entered.json()["desired_state"] == "draining"

    drained = client.post(
        report_url,
        headers=report_headers,
        json={
            "instance_id": "local-maintenance-1",
            "pool": "local",
            "active_businesses": 0,
            "healthy": True,
            "ready": True,
            "accepting": False,
        },
    )
    assert drained.status_code == 200
    assert drained.json()["desired_state"] == "maintenance"

    exiting = client.post(
        f"/api/v1/scaling/instances/{instance['id']}/maintenance/exit",
        headers=admin_headers,
    )
    assert exiting.status_code == 200
    assert exiting.json()["desired_state"] == "restoring"

    restored = client.post(
        report_url,
        headers=report_headers,
        json={
            "instance_id": "local-maintenance-1",
            "pool": "local",
            "active_businesses": 0,
            "healthy": True,
            "ready": True,
            "accepting": True,
        },
    )
    assert restored.status_code == 200
    assert restored.json()["desired_state"] == "serve"


def test_ai_service_permissions_split_view_configure_and_release(
    client, admin_headers: dict[str, str], session_for
):
    created = client.post(
        "/api/v1/scaling/services",
        headers=admin_headers,
        json=_payload(f"VLM-permissions-{uuid4()}"),
    )
    assert created.status_code == 201
    service = created.json()

    users = [
        ("scaling-viewer", UserRole.viewer, False, False),
        ("scaling-configurer", UserRole.operator, True, False),
        ("scaling-releaser", UserRole.operator, True, True),
        ("scaling-hidden", UserRole.viewer, False, False),
    ]
    with SessionLocal() as db:
        for userid, role, can_operate, can_manage in users:
            user = User(
                username=userid,
                wecom_userid=userid,
                display_name=userid,
                role=role,
                resource_restricted=True,
            )
            db.add(user)
            db.flush()
            if userid != "scaling-hidden":
                db.add(
                    AccessRule(
                        user_id=user.id,
                        scope_type="service",
                        service_id=service["id"],
                        can_view=True,
                        can_operate=can_operate,
                        can_manage=can_manage,
                    )
                )
        db.commit()

    viewer_headers = session_for("scaling-viewer")
    assert client.get("/api/v1/auth/me").json()["can_view_scaling"] is True
    viewer_services = client.get("/api/v1/scaling/services").json()
    assert [item["id"] for item in viewer_services] == [service["id"]]
    assert viewer_services[0]["permissions"] == {
        "view": True,
        "operate": False,
        "manage": False,
    }
    assert (
        client.put(
            f"/api/v1/scaling/services/{service['id']}",
            headers=viewer_headers,
            json=_payload(service["name"]),
        ).status_code
        == 403
    )

    configurer_headers = session_for("scaling-configurer")
    configurer_service = client.get("/api/v1/scaling/services").json()[0]
    assert configurer_service["permissions"] == {
        "view": True,
        "operate": True,
        "manage": False,
    }
    assert (
        client.put(
            f"/api/v1/scaling/services/{service['id']}",
            headers=configurer_headers,
            json=_payload(service["name"]),
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/scaling/services/{service['id']}/report-token/rotate",
            headers=configurer_headers,
        ).status_code
        == 403
    )

    releaser_headers = session_for("scaling-releaser")
    releaser_service = client.get("/api/v1/scaling/services").json()[0]
    assert releaser_service["permissions"] == {
        "view": True,
        "operate": True,
        "manage": True,
    }
    assert (
        client.post(
            f"/api/v1/scaling/services/{service['id']}/report-token/rotate",
            headers=releaser_headers,
        ).status_code
        == 200
    )

    session_for("scaling-hidden")
    assert client.get("/api/v1/auth/me").json()["can_view_scaling"] is False
    assert client.get("/api/v1/scaling/services").json() == []


def test_initial_admin_decides_whether_downgrade_retains_elevated_service_rights(
    client, admin_headers: dict[str, str], session_for
):
    created = client.post(
        "/api/v1/scaling/services",
        headers=admin_headers,
        json=_payload(f"VLM-role-change-{uuid4()}"),
    ).json()
    with SessionLocal() as db:
        user = User(
            username="role-change-operator",
            wecom_userid="role-change-operator",
            display_name="Role Change Operator",
            role=UserRole.operator,
            resource_restricted=True,
        )
        db.add(user)
        db.flush()
        user_id = user.id
        db.add(
            AccessRule(
                user_id=user.id,
                scope_type="service",
                service_id=created["id"],
                can_view=True,
                can_operate=True,
                can_manage=True,
            )
        )
        db.add(
            User(
                username="secondary-role-admin",
                wecom_userid="secondary-role-admin",
                display_name="Secondary Role Admin",
                role=UserRole.admin,
                resource_restricted=False,
            )
        )
        db.commit()

    secondary_headers = session_for("secondary-role-admin")
    listed = client.get("/api/v1/users").json()
    target = next(item for item in listed if item["id"] == user_id)
    assert target["can_edit_account"] is True
    assert target["can_edit_role"] is False
    denied = client.patch(
        f"/api/v1/users/{user_id}",
        headers=secondary_headers,
        json={"role": "viewer", "elevated_access_action": "retain"},
    )
    assert denied.status_code == 403

    admin_headers = session_for("admin-wecom-id")
    listed = client.get("/api/v1/users").json()
    target = next(item for item in listed if item["id"] == user_id)
    assert target["can_edit_role"] is True
    missing_decision = client.patch(
        f"/api/v1/users/{user_id}",
        headers=admin_headers,
        json={"role": "viewer"},
    )
    assert missing_decision.status_code == 409

    changed = client.patch(
        f"/api/v1/users/{user_id}",
        headers=admin_headers,
        json={"role": "viewer", "elevated_access_action": "retain"},
    )
    assert changed.status_code == 200
    assert changed.json()["role"] == "viewer"
    with SessionLocal() as db:
        rule = db.scalar(select(AccessRule).where(AccessRule.user_id == user_id))
        assert rule is not None
        assert rule.can_view is True
        assert rule.can_operate is True
        assert rule.can_manage is True

    viewer_headers = session_for("role-change-operator")
    assert (
        client.post(
            f"/api/v1/scaling/services/{created['id']}/report-token/rotate",
            headers=viewer_headers,
        ).status_code
        == 403
    )

    admin_headers = session_for("admin-wecom-id")
    promoted = client.patch(
        f"/api/v1/users/{user_id}",
        headers=admin_headers,
        json={"role": "operator"},
    )
    assert promoted.status_code == 200
    cleared = client.patch(
        f"/api/v1/users/{user_id}",
        headers=admin_headers,
        json={"role": "viewer", "elevated_access_action": "clear"},
    )
    assert cleared.status_code == 200
    with SessionLocal() as db:
        rule = db.scalar(select(AccessRule).where(AccessRule.user_id == user_id))
        assert rule is not None
        assert rule.can_operate is False
        assert rule.can_manage is False


def test_heterogeneous_instance_capacities_drive_service_utilization():
    now = datetime.now(UTC)
    provider = FakeAcsProvider(0)
    with SessionLocal() as db:
        service = ScalingService(
            name=f"VLM-mixed-gpu-{uuid4()}",
            service_type="vlm",
            report_token_hash=digest_token("unused-token"),
            capacity_per_replica=10,
            autoscaling_enabled=False,
            namespace="inference",
            workload_kind="Deployment",
            workload_name="vlm-mixed-gpu",
        )
        db.add(service)
        db.flush()
        db.add_all(
            [
                ServiceInstance(
                    service_id=service.id,
                    instance_id="vlm-a10",
                    pool="local",
                    capacity_limit=8,
                    active_businesses=4,
                    updated_at=now,
                ),
                ServiceInstance(
                    service_id=service.id,
                    instance_id="vlm-l40s",
                    pool="local",
                    capacity_limit=24,
                    active_businesses=12,
                    updated_at=now,
                ),
            ]
        )
        db.flush()

        evaluate_service(db, service, now, _enabled_settings(), provider)

        sample = db.scalar(
            select(BusinessMetricSample)
            .where(BusinessMetricSample.service_id == service.id)
            .order_by(BusinessMetricSample.sampled_at.desc())
        )
        assert sample is not None
        assert sample.total_capacity == 32
        assert sample.active_businesses == 16
        assert service.utilization_percent == 50


def test_gateway_discovery_adds_and_removes_only_eligible_endpoints(
    client, admin_headers: dict[str, str]
):
    assert client.get("/api/v1/service-discovery/routes").status_code == 503
    settings = get_settings().model_copy(
        update={
            "gateway_discovery_token": "test-gateway-discovery-token",
            "gateway_discovery_allowed_cidrs": "10.10.0.0/16",
        }
    )
    client.app.dependency_overrides[get_settings] = lambda: settings
    try:
        created = client.post(
            "/api/v1/scaling/services",
            headers=admin_headers,
            json=_payload(f"ASR-discovery-{uuid4()}"),
        )
        assert created.status_code == 201
        service = created.json()
        report_headers = {"X-CloudHelm-Service-Token": service["report_token"]}
        discovery_headers = {
            "X-CloudHelm-Gateway-Token": "test-gateway-discovery-token"
        }

        denied = client.get(
            "/api/v1/service-discovery/routes",
            headers={"X-CloudHelm-Gateway-Token": "wrong-token"},
        )
        assert denied.status_code == 401

        invalid_endpoint = client.post(
            f"/api/v1/service-metrics/{service['id']}/report",
            headers=report_headers,
            json={
                "instance_id": "asr-local-invalid",
                "pool": "local",
                "active_businesses": 0,
                "endpoint_host": "10.10.0.22",
            },
        )
        assert invalid_endpoint.status_code == 422

        outside_allowed_network = client.post(
            f"/api/v1/service-metrics/{service['id']}/report",
            headers=report_headers,
            json={
                "instance_id": "asr-local-outside",
                "pool": "local",
                "active_businesses": 0,
                "endpoint_scheme": "http",
                "endpoint_host": "192.168.50.20",
                "endpoint_port": 9000,
            },
        )
        assert outside_allowed_network.status_code == 422

        reported = client.post(
            f"/api/v1/service-metrics/{service['id']}/report",
            headers=report_headers,
            json={
                "instance_id": "asr-local-1",
                "pool": "local",
                "active_businesses": 1,
                "healthy": True,
                "ready": True,
                "accepting": True,
                "endpoint_scheme": "http",
                "endpoint_host": "10.10.0.21",
                "endpoint_port": 9000,
            },
        )
        assert reported.status_code == 200
        assert reported.json()["endpoint_registered"] is True

        discovered = client.get(
            "/api/v1/service-discovery/routes", headers=discovery_headers
        )
        assert discovered.status_code == 200
        route = next(
            item
            for item in discovered.json()["services"]
            if item["service_id"] == service["id"]
        )
        assert len(route["endpoints"]) == 1
        endpoint = route["endpoints"][0]
        assert endpoint | {"expires_at": "ignored"} == {
            "instance_id": "asr-local-1",
            "pool": "local",
            "scheme": "http",
            "host": "10.10.0.21",
            "port": 9000,
            "url": "http://10.10.0.21:9000",
            "expires_at": "ignored",
        }
        unchanged = client.get(
            "/api/v1/service-discovery/routes",
            headers={
                **discovery_headers,
                "If-None-Match": discovered.headers["etag"],
            },
        )
        assert unchanged.status_code == 304

        unhealthy = client.post(
            f"/api/v1/service-metrics/{service['id']}/report",
            headers=report_headers,
            json={
                "instance_id": "asr-local-1",
                "pool": "local",
                "active_businesses": 0,
                "healthy": False,
                "ready": False,
                "accepting": False,
            },
        )
        assert unhealthy.status_code == 200
        changed = client.get(
            "/api/v1/service-discovery/routes", headers=discovery_headers
        )
        route = next(
            item
            for item in changed.json()["services"]
            if item["service_id"] == service["id"]
        )
        assert route["endpoints"] == []
        assert changed.headers["etag"] != discovered.headers["etag"]
    finally:
        client.app.dependency_overrides.pop(get_settings, None)


def test_local_discovery_prefers_manually_configured_node_address(
    client, admin_headers: dict[str, str]
):
    settings = get_settings().model_copy(
        update={
            "gateway_discovery_token": "test-gateway-discovery-token",
            "gateway_discovery_allowed_cidrs": "10.30.0.0/16",
        }
    )
    client.app.dependency_overrides[get_settings] = lambda: settings
    try:
        created = client.post(
            "/api/v1/scaling/services",
            headers=admin_headers,
            json=_payload(f"VLM-node-address-{uuid4()}"),
        )
        service = created.json()
        enrolled = client.post(
            "/api/v1/agent/enroll",
            json={
                "enrollment_token": "test-agent-enrollment-token",
                "agent_key": f"gateway-address-agent-{uuid4()}",
                "name": "网关地址测试节点",
                "hostname": "vlm-local-host",
            },
        )
        credentials = enrolled.json()
        heartbeat = client.post(
            "/api/v1/agent/heartbeat",
            headers={
                "X-Node-Id": credentials["node_id"],
                "X-Agent-Token": credentials["node_token"],
            },
            json={
                "hostname": "vlm-local-host",
                "containers": [
                    {
                        "docker_id": "gateway-local-container-1234",
                        "name": "vlm-shared-image",
                        "image": "example/vlm:shared",
                        "status": "running",
                        "labels": {
                            "cloudhelm.service_id": service["id"],
                            "cloudhelm.instance_id": "vlm-local-shared-1",
                        },
                    }
                ],
            },
        )
        assert heartbeat.status_code == 204

        configured = client.put(
            f"/api/v1/nodes/{credentials['node_id']}/gateway-address",
            headers=admin_headers,
            json={"gateway_address": "10.30.0.50"},
        )
        assert configured.status_code == 200
        report = client.post(
            f"/api/v1/service-metrics/{service['id']}/report",
            headers={"X-CloudHelm-Service-Token": service["report_token"]},
            json={
                "instance_id": "vlm-local-shared-1",
                "pool": "local",
                "active_businesses": 0,
                "healthy": True,
                "ready": True,
                "accepting": True,
                "endpoint_scheme": "http",
                "endpoint_port": 9000,
            },
        )
        assert report.status_code == 200
        assert report.json()["endpoint_registered"] is True

        discovered = client.get(
            "/api/v1/service-discovery/routes",
            headers={"X-CloudHelm-Gateway-Token": "test-gateway-discovery-token"},
        )
        route = next(
            item
            for item in discovered.json()["services"]
            if item["service_id"] == service["id"]
        )
        assert route["endpoints"][0]["host"] == "10.30.0.50"
        assert route["endpoints"][0]["url"] == "http://10.30.0.50:9000"

        nodes = client.get("/api/v1/nodes", headers=admin_headers)
        node = next(
            item for item in nodes.json() if item["id"] == credentials["node_id"]
        )
        assert node["gateway_address"] == "10.30.0.50"
    finally:
        client.app.dependency_overrides.pop(get_settings, None)


def test_agent_labels_auto_bind_only_local_service_instances(
    client, admin_headers: dict[str, str]
):
    created = client.post(
        "/api/v1/scaling/services",
        headers=admin_headers,
        json=_payload(f"VLM-auto-bind-{uuid4()}"),
    )
    assert created.status_code == 201
    service = created.json()
    report_headers = {"X-CloudHelm-Service-Token": service["report_token"]}

    local_report = client.post(
        f"/api/v1/service-metrics/{service['id']}/report",
        headers=report_headers,
        json={
            "instance_id": "vlm-local-auto-1",
            "pool": "local",
            "active_businesses": 1,
        },
    )
    assert local_report.status_code == 200
    assert local_report.json()["container_linked"] is False
    acs_report = client.post(
        f"/api/v1/service-metrics/{service['id']}/report",
        headers=report_headers,
        json={"instance_id": "vlm-acs-pod-1", "pool": "acs", "active_businesses": 0},
    )
    assert acs_report.status_code == 200

    enrolled = client.post(
        "/api/v1/agent/enroll",
        json={
            "enrollment_token": "test-agent-enrollment-token",
            "agent_key": f"auto-bind-agent-{uuid4()}",
            "name": "自动关联节点",
            "hostname": "auto-bind-node",
            "agent_version": "0.9.5",
        },
    )
    assert enrolled.status_code == 200
    credentials = enrolled.json()
    heartbeat = client.post(
        "/api/v1/agent/heartbeat",
        headers={
            "X-Node-Id": credentials["node_id"],
            "X-Agent-Token": credentials["node_token"],
        },
        json={
            "hostname": "auto-bind-node",
            "agent_version": "0.9.5",
            "gpu_status": "ok",
            "gpus": [
                {
                    "index": 0,
                    "uuid": "GPU-L40S-AUTO-BIND",
                    "name": "NVIDIA L40S",
                }
            ],
            "containers": [
                {
                    "docker_id": "local-auto-container-123456",
                    "name": "vlm-local-auto",
                    "image": "example/vlm:1.0",
                    "status": "running",
                    "gpu_devices": ["0"],
                    "labels": {
                        "cloudhelm.service_id": service["id"],
                        "cloudhelm.instance_id": "vlm-local-auto-1",
                    },
                },
                {
                    "docker_id": "invalid-acs-container-123456",
                    "name": "must-not-bind-acs",
                    "image": "example/vlm:1.0",
                    "status": "running",
                    "labels": {
                        "cloudhelm.service_id": service["id"],
                        "cloudhelm.instance_id": "vlm-acs-pod-1",
                    },
                },
            ],
        },
    )
    assert heartbeat.status_code == 204

    with SessionLocal() as db:
        local = db.scalar(
            select(ServiceInstance).where(
                ServiceInstance.service_id == service["id"],
                ServiceInstance.instance_id == "vlm-local-auto-1",
            )
        )
        acs = db.scalar(
            select(ServiceInstance).where(
                ServiceInstance.service_id == service["id"],
                ServiceInstance.instance_id == "vlm-acs-pod-1",
            )
        )
        assert local is not None and local.container_id is not None
        linked = db.get(Container, local.container_id)
        assert linked is not None
        assert linked.docker_id == "local-auto-container-123456"
        assert acs is not None
        assert acs.pool == "acs"
        assert acs.container_id is None
        local_container_id = local.container_id

    repeated = client.post(
        f"/api/v1/service-metrics/{service['id']}/report",
        headers=report_headers,
        json={
            "instance_id": "vlm-local-auto-1",
            "pool": "local",
            "active_businesses": 2,
        },
    )
    assert repeated.status_code == 200
    assert repeated.json()["container_id"] == local_container_id
    assert repeated.json()["container_linked"] is True

    listed = client.get("/api/v1/scaling/services")
    target = next(item for item in listed.json() if item["id"] == service["id"])
    local_out = next(
        item
        for item in target["instances"]
        if item["instance_id"] == "vlm-local-auto-1"
    )
    assert local_out["node_name"] == "自动关联节点"
    assert local_out["container_name"] == "vlm-local-auto"
    assert local_out["gpu_devices"] == ["0"]
    assert local_out["gpu_names"] == ["NVIDIA L40S"]

    linked_delete = client.delete(
        f"/api/v1/scaling/instances/{local_out['id']}", headers=admin_headers
    )
    assert linked_delete.status_code == 409
    assert "仍关联本地容器" in linked_delete.json()["detail"]

    rejected = client.post(
        f"/api/v1/service-metrics/{service['id']}/report",
        headers=report_headers,
        json={
            "instance_id": "vlm-acs-pod-2",
            "pool": "acs",
            "container_id": local_container_id,
            "active_businesses": 0,
        },
    )
    assert rejected.status_code == 422


class FakeAcsProvider:
    def __init__(self, replicas: int):
        self.replicas = replicas
        self.scale_calls: list[tuple[int, int]] = []
        self.preferred_pods: list[str] = []

    def get_replicas(self, service: ScalingService) -> int:
        return self.replicas

    def set_replicas(self, service: ScalingService, expected: int, target: int) -> int:
        assert expected == self.replicas
        self.scale_calls.append((expected, target))
        self.replicas = target
        return target

    def prefer_pod_for_removal(self, service: ScalingService, pod_name: str) -> None:
        self.preferred_pods.append(pod_name)


def _enabled_settings():
    return get_settings().model_copy(update={"autoscaling_enabled": True})


def test_local_failure_starts_acs_failover_without_waiting_for_window():
    now = datetime.now(UTC)
    provider = FakeAcsProvider(0)
    with SessionLocal() as db:
        service = ScalingService(
            name=f"ASR-failover-{uuid4()}",
            service_type="asr",
            report_token_hash=digest_token("unused-token"),
            capacity_per_replica=4,
            autoscaling_enabled=True,
            min_replicas=0,
            max_replicas=5,
            failover_replicas=2,
            namespace="inference",
            workload_kind="Deployment",
            workload_name="asr-burst",
        )
        db.add(service)
        db.flush()
        db.add(
            ServiceInstance(
                service_id=service.id,
                instance_id="local-asr-1",
                pool="local",
                healthy=False,
                ready=False,
                active_businesses=0,
                updated_at=now,
            )
        )
        db.flush()

        evaluate_service(db, service, now, _enabled_settings(), provider)
        db.commit()

        assert provider.scale_calls == [(0, 2)]
        assert service.status == "scaling_up_failover"
        event = db.scalar(
            select(ScalingEvent)
            .where(ScalingEvent.service_id == service.id)
            .order_by(ScalingEvent.created_at.desc())
        )
        assert event is not None
        assert event.action == "failover_scale_up"
        assert "本地实例故障" in event.detail


def test_low_load_drains_only_acs_instance_before_scaling_down():
    now = datetime.now(UTC)
    provider = FakeAcsProvider(1)
    with SessionLocal() as db:
        service = ScalingService(
            name=f"TTS-drain-{uuid4()}",
            service_type="tts",
            report_token_hash=digest_token("unused-token"),
            capacity_per_replica=10,
            autoscaling_enabled=True,
            min_replicas=0,
            max_replicas=5,
            scale_down_threshold_percent=40,
            window_seconds=300,
            cooldown_seconds=30,
            namespace="inference",
            workload_kind="Deployment",
            workload_name="tts-burst",
        )
        db.add(service)
        db.flush()
        local = ServiceInstance(
            service_id=service.id,
            instance_id="local-tts-1",
            pool="local",
            healthy=True,
            ready=True,
            accepting=True,
            active_businesses=1,
            updated_at=now,
        )
        acs = ServiceInstance(
            service_id=service.id,
            instance_id="tts-burst-abc123",
            pool="acs",
            healthy=True,
            ready=True,
            accepting=True,
            active_businesses=0,
            updated_at=now,
        )
        db.add_all([local, acs])
        db.add(
            BusinessMetricSample(
                service_id=service.id,
                sampled_at=now - timedelta(seconds=285),
                active_businesses=1,
                total_capacity=20,
                reporting_instances=2,
            )
        )
        db.flush()

        evaluate_service(db, service, now, _enabled_settings(), provider)
        assert provider.scale_calls == []
        assert local.desired_state == "serve"
        assert acs.desired_state == "draining"
        assert service.status == "draining"

        acs.accepting = False
        acs.active_businesses = 0
        acs.updated_at = now + timedelta(seconds=15)
        evaluate_service(
            db,
            service,
            now + timedelta(seconds=15),
            _enabled_settings(),
            provider,
        )
        db.commit()

        assert provider.preferred_pods == ["tts-burst-abc123"]
        assert provider.scale_calls == [(1, 0)]
        assert local.desired_state == "serve"
        assert service.status == "scaling_down"
