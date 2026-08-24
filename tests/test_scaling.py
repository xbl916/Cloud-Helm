from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from cloudhelm.autoscaling import evaluate_service
from cloudhelm.config import get_settings
from cloudhelm.db import SessionLocal
from cloudhelm.models import (
    BusinessMetricSample,
    Container,
    ScalingEvent,
    ScalingService,
    ServiceInstance,
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
        json={"instance_id": "vlm-local-auto-1", "pool": "local", "active_businesses": 1},
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
            "agent_version": "0.9.1",
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
            "agent_version": "0.9.1",
            "containers": [
                {
                    "docker_id": "local-auto-container-123456",
                    "name": "vlm-local-auto",
                    "image": "example/vlm:1.0",
                    "status": "running",
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
        json={"instance_id": "vlm-local-auto-1", "pool": "local", "active_businesses": 2},
    )
    assert repeated.status_code == 200
    assert repeated.json()["container_id"] == local_container_id
    assert repeated.json()["container_linked"] is True

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

    def set_replicas(
        self, service: ScalingService, expected: int, target: int
    ) -> int:
        assert expected == self.replicas
        self.scale_calls.append((expected, target))
        self.replicas = target
        return target

    def prefer_pod_for_removal(
        self, service: ScalingService, pod_name: str
    ) -> None:
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
        assert event.action == "scale_up"
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
        assert acs.desired_state == "drain"
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
