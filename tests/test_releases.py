from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from cloudhelm.config import get_settings
from cloudhelm.db import SessionLocal
from cloudhelm.models import (
    Container,
    Node,
    ScalingService,
    ServiceInstance,
    Task,
    TaskStatus,
    User,
)
from cloudhelm.releases import _progress_service, resolve_local_container
from cloudhelm.security import digest_token


class UnusedProvider:
    pass


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
            desired_image=target,
            previous_image="docker.io/example/vlm:1.0",
            release_status="updating_local",
            release_requested_by=admin.id,
            release_started_at=now,
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

        _progress_service(db, service, now, UnusedProvider(), settings)  # type: ignore[arg-type]
        db.flush()
        tasks = list(
            db.scalars(
                select(Task)
                .where(Task.container_id.in_([item.id for item in containers]))
                .order_by(Task.created_at)
            ).all()
        )
        assert len(tasks) == 1
        assert tasks[0].container_id == containers[0].id
        assert service.release_task_id == tasks[0].id

        tasks[0].status = TaskStatus.success
        tasks[0].finished_at = now + timedelta(seconds=1)
        containers[0].image = target
        # Docker has started, but the business process has not reported Ready
        # after the replacement yet. The second node must remain untouched.
        _progress_service(
            db,
            service,
            now + timedelta(seconds=2),
            UnusedProvider(),  # type: ignore[arg-type]
            settings,
        )
        assert service.release_status == "verifying_local"
        assert db.scalar(select(Task).where(Task.container_id == containers[1].id)) is None

        instances[0].updated_at = now + timedelta(seconds=3)
        _progress_service(
            db,
            service,
            now + timedelta(seconds=4),
            UnusedProvider(),  # type: ignore[arg-type]
            settings,
        )
        assert service.release_task_id is None
        _progress_service(
            db,
            service,
            now + timedelta(seconds=5),
            UnusedProvider(),  # type: ignore[arg-type]
            settings,
        )
        second = db.scalar(select(Task).where(Task.container_id == containers[1].id))
        assert second is not None
        assert second.status == TaskStatus.pending

        second.status = TaskStatus.success
        second.finished_at = now + timedelta(seconds=6)
        containers[1].image = target
        instances[1].updated_at = now + timedelta(seconds=7)
        _progress_service(
            db,
            service,
            now + timedelta(seconds=8),
            UnusedProvider(),  # type: ignore[arg-type]
            settings,
        )
        _progress_service(
            db,
            service,
            now + timedelta(seconds=9),
            UnusedProvider(),  # type: ignore[arg-type]
            settings,
        )
        assert service.release_status == "completed"
        assert len(
            db.scalars(
                select(Task).where(
                    Task.container_id.in_([item.id for item in containers])
                )
            ).all()
        ) == 2
