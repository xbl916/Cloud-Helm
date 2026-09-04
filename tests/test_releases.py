from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
from sqlalchemy import select

from cloudhelm.autoscaling import AcsKubernetesClient
from cloudhelm.config import get_settings
from cloudhelm.db import SessionLocal
from cloudhelm.models import (
    Container,
    ImageRelease,
    ImageReleaseStep,
    Node,
    ScalingService,
    ServiceImageVersion,
    ServiceInstance,
    Task,
    TaskStatus,
    User,
)
from cloudhelm.releases import _progress_service, resolve_local_container
from cloudhelm.routers.scaling import _start_image_release
from cloudhelm.security import digest_token


class UnusedProvider:
    pass


def _response(status: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "https://acs.example.test/resource"),
    )


def test_release_guard_clones_old_image_into_independent_deployment():
    service = ScalingService(
        name=f"guard-{uuid4()}",
        report_token_hash="unused",
        namespace="inference",
        workload_kind="Deployment",
        workload_name="vlm-load",
    )
    provider = AcsKubernetesClient.__new__(AcsKubernetesClient)
    provider.client = MagicMock()
    workload = {
        "metadata": {"generation": 3},
        "spec": {
            "replicas": 2,
            "template": {
                "metadata": {"labels": {"app": "vlm"}},
                "spec": {
                    "containers": [
                        {"name": "vlm", "image": "example/vlm:1.0", "env": []}
                    ]
                },
            },
        },
    }
    provider.client.get.side_effect = [_response(404), _response(200, workload)]
    provider.client.post.return_value = _response(201, {})

    name = provider.ensure_release_guard(
        service, "12345678-1234-1234-1234-123456789abc", "example/vlm:1.0"
    )

    assert name == "vlm-load-cloudhelm-release-12345678"
    body = provider.client.post.call_args.kwargs["json"]
    assert body["spec"]["replicas"] == 1
    assert body["spec"]["selector"]["matchLabels"] == {
        "cloudhelm.io/release-id": "12345678-1234-1234-1234-123456789abc",
        "cloudhelm.io/instance-pool": "acs_release",
    }
    container = body["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "docker.io/example/vlm:1.0"
    assert {item["name"]: item["value"] for item in container["env"]} == {
        "CLOUDHELM_INSTANCE_POOL": "acs_release",
        "CLOUDHELM_RELEASE_ID": "12345678-1234-1234-1234-123456789abc",
    }


def test_start_release_creates_durable_release_steps_and_source_version():
    now = datetime.now(UTC)
    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.wecom_userid == "admin-wecom-id"))
        assert admin is not None
        service = ScalingService(
            name=f"VLM-durable-release-{uuid4()}",
            service_type="vlm",
            report_token_hash=digest_token("unused-durable-token"),
            capacity_per_replica=10,
            namespace="inference",
            workload_kind="Deployment",
            workload_name="vlm-durable",
        )
        node = Node(
            agent_key=f"durable-release-agent-{uuid4()}",
            name="持久化发布节点",
            agent_token_hash="unused",
            last_seen_at=now,
        )
        db.add_all([service, node])
        db.flush()
        container = Container(
            node_id=node.id,
            docker_id="durable-release-container-123456",
            name="vlm",
            image="example/vlm:1.0",
            status="running",
            present=True,
        )
        db.add(container)
        db.flush()
        instance = ServiceInstance(
            service_id=service.id,
            instance_id="local-durable-vlm",
            container_id=container.id,
            pool="local",
            monitored=True,
            updated_at=now,
        )
        db.add(instance)
        db.flush()

        target = _start_image_release(
            db, service, "example/vlm:2.0", admin, now
        )
        db.flush()

        release = db.get(ImageRelease, service.active_release_id)
        assert release is not None
        assert release.source_image == "docker.io/example/vlm:1.0"
        assert release.target_image == target
        assert release.status == "running"
        steps = list(
            db.scalars(
                select(ImageReleaseStep).where(
                    ImageReleaseStep.release_id == release.id
                )
            ).all()
        )
        assert len(steps) == 1
        assert steps[0].service_instance_id == instance.id
        assert steps[0].position == 1
        source = db.scalar(
            select(ServiceImageVersion).where(
                ServiceImageVersion.service_id == service.id,
                ServiceImageVersion.image == "docker.io/example/vlm:1.0",
            )
        )
        assert source is not None


def test_single_local_release_waits_for_old_guard_to_serve_before_drain():
    now = datetime.now(UTC)
    settings = get_settings()
    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.wecom_userid == "admin-wecom-id"))
        assert admin is not None
        service = ScalingService(
            name=f"VLM-single-guard-{uuid4()}",
            service_type="vlm",
            report_token_hash=digest_token("unused-single-guard-token"),
            capacity_per_replica=10,
            namespace="inference",
            workload_kind="Deployment",
            workload_name="vlm-load",
        )
        node = Node(
            agent_key=f"single-guard-agent-{uuid4()}",
            name="单实例保护节点",
            agent_token_hash="unused",
            last_seen_at=now,
        )
        db.add_all([service, node])
        db.flush()
        container = Container(
            node_id=node.id,
            docker_id="single-guard-container-123456",
            name="vlm",
            image="example/vlm:1.0",
            status="running",
            present=True,
        )
        db.add(container)
        db.flush()
        local = ServiceInstance(
            service_id=service.id,
            instance_id="local-single-guard",
            container_id=container.id,
            pool="local",
            healthy=True,
            ready=True,
            accepting=True,
            monitored=True,
            updated_at=now,
        )
        db.add(local)
        db.flush()
        _start_image_release(db, service, "example/vlm:2.0", admin, now)
        release = db.get(ImageRelease, service.active_release_id)
        assert release is not None
        step = db.scalar(
            select(ImageReleaseStep).where(ImageReleaseStep.release_id == release.id)
        )
        assert step is not None
        step.status = "prepared"
        step.stage = "image_prepared"
        service.release_status = "preparing_guard"
        provider = MagicMock()
        provider.ensure_release_guard.return_value = "vlm-release-guard"
        provider.release_guard_ready.return_value = True

        _progress_service(db, service, now, provider, settings)
        assert service.release_status == "waiting_guard"
        assert local.desired_state == "serve"
        _progress_service(db, service, now + timedelta(seconds=1), provider, settings)
        assert service.release_status == "waiting_guard"
        assert local.desired_state == "serve"

        db.add(
            ServiceInstance(
                service_id=service.id,
                instance_id="release-guard-pod-1",
                pool="acs_release",
                healthy=True,
                ready=True,
                accepting=True,
                monitored=True,
                endpoint_scheme="http",
                endpoint_host="10.20.0.8",
                endpoint_port=8000,
                metadata_json=f'{{"release_id":"{release.id}"}}',
                updated_at=now + timedelta(seconds=2),
            )
        )
        db.flush()
        _progress_service(db, service, now + timedelta(seconds=2), provider, settings)
        assert service.release_status == "draining_local"
        assert local.desired_state == "serve"
        _progress_service(db, service, now + timedelta(seconds=3), provider, settings)
        assert local.desired_state == "draining"


def test_high_load_template_updates_only_after_all_local_steps_complete():
    now = datetime.now(UTC)
    settings = get_settings()
    target = "docker.io/example/vlm:2.0"
    with SessionLocal() as db:
        service = ScalingService(
            name=f"VLM-template-order-{uuid4()}",
            service_type="vlm",
            report_token_hash="unused",
            desired_image=target,
            release_status="draining_local",
            namespace="inference",
            workload_kind="Deployment",
            workload_name="vlm-load",
        )
        db.add(service)
        db.flush()
        release = ImageRelease(
            service_id=service.id,
            source_image="docker.io/example/vlm:1.0",
            target_image=target,
            status="running",
            stage="draining_local",
            started_at=now,
        )
        db.add(release)
        db.flush()
        service.active_release_id = release.id
        db.add(
            ImageReleaseStep(
                release_id=release.id,
                position=1,
                status="completed",
                stage="healthy",
            )
        )
        db.flush()
        provider = MagicMock()
        provider.set_workload_image.return_value = target
        provider.workload_image_status.return_value = (True, target, 0)

        _progress_service(db, service, now, provider, settings)
        assert service.release_status == "updating_acs"
        provider.set_workload_image.assert_not_called()

        _progress_service(db, service, now + timedelta(seconds=1), provider, settings)
        assert service.release_status == "waiting_acs"
        provider.set_workload_image.assert_called_once_with(service, target)

        _progress_service(db, service, now + timedelta(seconds=2), provider, settings)
        assert service.release_status == "completed"
        version = db.scalar(
            select(ServiceImageVersion).where(
                ServiceImageVersion.service_id == service.id,
                ServiceImageVersion.image == target,
            )
        )
        assert version is not None


def test_rebinds_instance_after_compose_recreates_container():
    now = datetime.now(UTC)
    with SessionLocal() as db:
        node = Node(
            agent_key=f"rebind-agent-{uuid4()}",
            name="Compose 重建节点",
            agent_token_hash="unused",
            last_seen_at=now,
        )
        db.add(node)
        db.flush()
        old = Container(
            node_id=node.id,
            docker_id="old-compose-container",
            name="vlm-compose-1",
            image="example/vlm:1.0",
            compose_project="ai-stack",
            compose_service="vlm",
            present=False,
        )
        replacement = Container(
            node_id=node.id,
            docker_id="new-compose-container",
            name="vlm-compose-1",
            image="example/vlm:2.0",
            compose_project="ai-stack",
            compose_service="vlm",
            present=True,
        )
        db.add_all([old, replacement])
        db.flush()
        service = ScalingService(
            name=f"VLM-rebind-{uuid4()}",
            service_type="vlm",
            report_token_hash=digest_token("unused-rebind-token"),
            capacity_per_replica=10,
            namespace="inference",
            workload_kind="Deployment",
            workload_name="vlm-burst",
        )
        db.add(service)
        db.flush()
        instance = ServiceInstance(
            service_id=service.id,
            instance_id="local-vlm-compose",
            container_id=old.id,
            pool="local",
            monitored=True,
            updated_at=now,
        )
        db.add(instance)
        db.flush()

        assert resolve_local_container(db, instance) is replacement
        assert instance.container_id == replacement.id


def test_local_module_release_updates_one_container_at_a_time():
    now = datetime.now(UTC)
    settings = get_settings()
    target = "docker.io/example/vlm:2.0"
    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.wecom_userid == "admin-wecom-id"))
        assert admin is not None
        service = ScalingService(
            name=f"VLM-release-{uuid4()}",
            service_type="vlm",
            report_token_hash=digest_token("unused-release-token"),
            capacity_per_replica=10,
            namespace="inference",
            workload_kind="Deployment",
            workload_name="vlm-burst",
        )
        db.add(service)
        db.flush()
        nodes: list[Node] = []
        instances: list[ServiceInstance] = []
        containers: list[Container] = []
        for index in range(2):
            node = Node(
                agent_key=f"release-agent-{uuid4()}",
                name=f"发布节点 {index}",
                agent_token_hash="unused",
                last_seen_at=now,
            )
            db.add(node)
            db.flush()
            container = Container(
                node_id=node.id,
                docker_id=f"release-container-{index}-123456",
                name=f"vlm-{index}",
                image="example/vlm:1.0",
                status="running",
                present=True,
            )
            db.add(container)
            db.flush()
            instance = ServiceInstance(
                service_id=service.id,
                instance_id=f"local-vlm-{index}",
                container_id=container.id,
                pool="local",
                healthy=True,
                ready=True,
                accepting=True,
                monitored=True,
                updated_at=now,
            )
            db.add(instance)
            nodes.append(node)
            containers.append(container)
            instances.append(instance)
        db.flush()

        _start_image_release(db, service, target, admin, now)
        db.flush()

        _progress_service(db, service, now, UnusedProvider(), settings)  # type: ignore[arg-type]
        db.flush()
        first_prepare = db.get(Task, service.release_task_id)
        assert first_prepare is not None
        assert first_prepare.action == "prepare_image"
        assert first_prepare.container_id == containers[0].id
        first_prepare.status = TaskStatus.success
        first_prepare.finished_at = now + timedelta(seconds=1)
        _progress_service(
            db,
            service,
            now + timedelta(seconds=2),
            UnusedProvider(),  # type: ignore[arg-type]
            settings,
        )
        _progress_service(
            db,
            service,
            now + timedelta(seconds=3),
            UnusedProvider(),  # type: ignore[arg-type]
            settings,
        )
        second_prepare = db.get(Task, service.release_task_id)
        assert second_prepare is not None
        assert second_prepare.action == "prepare_image"
        assert second_prepare.container_id == containers[1].id
        second_prepare.status = TaskStatus.success
        second_prepare.finished_at = now + timedelta(seconds=4)
        _progress_service(
            db,
            service,
            now + timedelta(seconds=5),
            UnusedProvider(),  # type: ignore[arg-type]
            settings,
        )
        _progress_service(
            db,
            service,
            now + timedelta(seconds=6),
            UnusedProvider(),  # type: ignore[arg-type]
            settings,
        )
        _progress_service(
            db,
            service,
            now + timedelta(seconds=7),
            UnusedProvider(),  # type: ignore[arg-type]
            settings,
        )
        assert service.release_status == "draining_local"
        assert instances[0].desired_state == "draining"
        assert instances[1].desired_state == "serve"

        instances[0].desired_state = "maintenance"
        instances[0].accepting = False
        instances[0].active_businesses = 0
        _progress_service(
            db,
            service,
            now + timedelta(seconds=8),
            UnusedProvider(),  # type: ignore[arg-type]
            settings,
        )
        first_update = db.get(Task, service.release_task_id)
        assert first_update is not None
        assert first_update.action == "update_image"
        assert first_update.container_id == containers[0].id
        assert (
            db.scalar(
                select(Task).where(
                    Task.container_id == containers[1].id,
                    Task.action == "update_image",
                )
            )
            is None
        )

        first_update.status = TaskStatus.success
        first_update.finished_at = now + timedelta(seconds=9)
        containers[0].image = target
        _progress_service(
            db,
            service,
            now + timedelta(seconds=10),
            UnusedProvider(),  # type: ignore[arg-type]
            settings,
        )
        assert service.release_status == "verifying_local"
        assert instances[0].desired_state == "restoring"
        assert instances[1].desired_state == "serve"

        instances[0].healthy = True
        instances[0].ready = True
        instances[0].accepting = True
        instances[0].updated_at = now + timedelta(seconds=11)
        _progress_service(
            db,
            service,
            now + timedelta(seconds=12),
            UnusedProvider(),  # type: ignore[arg-type]
            settings,
        )
        assert instances[0].desired_state == "serve"
        _progress_service(
            db,
            service,
            now + timedelta(seconds=13),
            UnusedProvider(),  # type: ignore[arg-type]
            settings,
        )
        assert instances[1].desired_state == "draining"
