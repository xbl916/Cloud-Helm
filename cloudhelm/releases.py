import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudhelm.autoscaling import AcsKubernetesClient
from cloudhelm.config import Settings
from cloudhelm.image_reference import parse_tagged_image, validate_tag_change
from cloudhelm.models import (
    Container,
    Node,
    ScalingEvent,
    ScalingService,
    ServiceInstance,
    Task,
    TaskStatus,
)

ACTIVE_RELEASE_STATUSES = {
    "preparing_acs",
    "waiting_acs",
    "updating_local",
    "verifying_local",
}


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _release_event(
    db: Session, service: ScalingService, status: str, detail: str
) -> None:
    db.add(
        ScalingEvent(
            service_id=service.id,
            action="image_release",
            status=status,
            from_replicas=service.current_replicas,
            to_replicas=service.desired_replicas,
            average_businesses=service.average_businesses,
            utilization_percent=service.utilization_percent,
            detail=detail[:500],
        )
    )


def _fail_release(
    db: Session, service: ScalingService, now: datetime, error: str
) -> None:
    task = db.get(Task, service.release_task_id) if service.release_task_id else None
    if task and task.status == TaskStatus.pending:
        task.status = TaskStatus.expired
        task.error = "模块发布已停止，任务未下发"
        task.finished_at = now
    service.release_status = "failed"
    service.release_error = error[:500]
    service.release_task_id = None
    service.release_updated_at = now
    _release_event(db, service, "failed", error)


def _local_instances(db: Session, service: ScalingService) -> list[ServiceInstance]:
    return list(
        db.scalars(
            select(ServiceInstance)
            .where(
                ServiceInstance.service_id == service.id,
                ServiceInstance.pool == "local",
                ServiceInstance.monitored.is_(True),
                ServiceInstance.container_id.is_not(None),
            )
            .order_by(ServiceInstance.instance_id)
        ).all()
    )


def resolve_local_container(
    db: Session, instance: ServiceInstance
) -> Container | None:
    container = db.get(Container, instance.container_id) if instance.container_id else None
    if container and container.present:
        return container
    if not container:
        return None
    candidates_query = select(Container).where(
        Container.node_id == container.node_id,
        Container.present.is_(True),
    )
    if container.compose_project and container.compose_service:
        candidates_query = candidates_query.where(
            Container.compose_project == container.compose_project,
            Container.compose_service == container.compose_service,
        )
    else:
        candidates_query = candidates_query.where(Container.name == container.name)
    candidates = list(db.scalars(candidates_query.limit(2)).all())
    if len(candidates) != 1:
        return None
    instance.container_id = candidates[0].id
    return candidates[0]


def _image_matches(image: str, target: str) -> bool:
    try:
        return parse_tagged_image(image).canonical == parse_tagged_image(target).canonical
    except ValueError:
        return False


def _verify_finished_task(
    db: Session,
    service: ScalingService,
    task: Task,
    now: datetime,
) -> bool:
    if task.status in (TaskStatus.pending, TaskStatus.dispatched):
        return False
    if task.status != TaskStatus.success:
        _fail_release(db, service, now, task.error or "本地镜像更新任务失败")
        return False
    container = db.get(Container, task.container_id) if task.container_id else None
    instance = db.scalar(
        select(ServiceInstance).where(
            ServiceInstance.service_id == service.id,
            ServiceInstance.pool == "local",
            ServiceInstance.container_id == task.container_id,
            ServiceInstance.monitored.is_(True),
        )
    )
    finished_at = _aware(task.finished_at)
    reported_at = _aware(instance.updated_at) if instance else None
    verified = bool(
        container
        and container.present
        and container.status == "running"
        and container.health != "unhealthy"
        and _image_matches(container.image, service.desired_image or "")
        and instance
        and instance.healthy
        and instance.ready
        and instance.accepting
        and finished_at
        and reported_at
        and reported_at >= finished_at
    )
    if verified:
        service.release_task_id = None
        service.release_status = "updating_local"
        service.release_updated_at = now
        _release_event(
            db,
            service,
            "progress",
            f"本地实例 {instance.instance_id} 已使用新镜像并恢复健康上报",
        )
    else:
        service.release_status = "verifying_local"
    return verified


def _progress_service(
    db: Session,
    service: ScalingService,
    now: datetime,
    provider: AcsKubernetesClient,
    settings: Settings,
) -> None:
    target = service.desired_image
    if not target:
        return
    if service.release_status == "completed":
        instances = _local_instances(db, service)
        drift = any(
            (container := resolve_local_container(db, item)) is None
            or not _image_matches(container.image, target)
            for item in instances
        )
        ready, acs_image, _ = provider.workload_image_status(service, target)
        if not _image_matches(acs_image, target):
            service.release_status = "preparing_acs"
            service.release_error = None
        elif drift:
            # ACS already runs the desired version, so it remains the safety
            # capacity while a Compose restart drift is corrected locally.
            service.release_status = "updating_local"
            service.release_error = None
        else:
            return

    if service.release_status == "preparing_acs":
        current_replicas = provider.get_replicas(service)
        if service.release_acs_original_replicas is None:
            service.release_acs_original_replicas = current_replicas
        if current_replicas < 1:
            provider.set_replicas(service, current_replicas, 1)
        canonical = provider.set_workload_image(service, target)
        service.desired_image = canonical
        service.release_status = "waiting_acs"
        service.release_updated_at = now
        _release_event(
            db,
            service,
            "progress",
            "ACS 保护副本已启动，等待新镜像全部 Ready",
        )
        return

    if service.release_status == "waiting_acs":
        ready, _, _ = provider.workload_image_status(service, target)
        if not ready:
            return
        service.release_status = "updating_local"
        service.release_updated_at = now
        _release_event(
            db,
            service,
            "progress",
            "ACS 新版本已全部 Ready，开始逐个更新本地实例",
        )
        return

    if service.release_status in {"updating_local", "verifying_local"}:
        if service.release_task_id:
            task = db.get(Task, service.release_task_id)
            if not task:
                _fail_release(db, service, now, "本地发布任务记录不存在")
                return
            _verify_finished_task(db, service, task, now)
            return

        instances = _local_instances(db, service)
        if not instances:
            _fail_release(db, service, now, "没有关联且受监控的本地服务实例")
            return
        for instance in instances:
            container = resolve_local_container(db, instance)
            if not container:
                _fail_release(db, service, now, f"本地实例 {instance.instance_id} 的容器不存在")
                return
            if _image_matches(container.image, target):
                continue
            if container.status != "running" or container.health == "unhealthy":
                _fail_release(
                    db,
                    service,
                    now,
                    f"本地实例 {instance.instance_id} 当前不可健康替换",
                )
                return
            try:
                _, target_ref = validate_tag_change(container.image, target)
            except ValueError as exc:
                _fail_release(db, service, now, str(exc))
                return
            if not service.release_requested_by:
                _fail_release(db, service, now, "发布操作人不存在，无法创建本地任务")
                return
            node = db.get(Node, container.node_id)
            last_seen = _aware(node.last_seen_at) if node else None
            if (
                not node
                or not last_seen
                or (now - last_seen).total_seconds() > settings.node_offline_seconds
            ):
                _fail_release(db, service, now, f"节点离线：{instance.instance_id}")
                return
            active_task = db.scalar(
                select(Task.id).where(
                    Task.container_id == container.id,
                    Task.status.in_([TaskStatus.pending, TaskStatus.dispatched]),
                )
            )
            if active_task:
                return
            task = Task(
                node_id=container.node_id,
                container_id=container.id,
                docker_id=container.docker_id,
                action="update_image",
                arguments_json=json.dumps(
                    {
                        "expected_image": container.image,
                        "target_image": target_ref.canonical,
                        "startup_health_seconds": 300,
                    }
                ),
                requested_by=service.release_requested_by,
            )
            db.add(task)
            db.flush()
            service.release_task_id = task.id
            service.release_status = "updating_local"
            service.release_updated_at = now
            _release_event(
                db,
                service,
                "progress",
                f"已下发本地实例 {instance.instance_id} 的镜像更新任务",
            )
            return

        service.release_status = "completed"
        service.release_error = None
        service.release_task_id = None
        service.release_updated_at = now
        _release_event(
            db,
            service,
            "success",
            "ACS 与全部本地实例已统一到期望镜像；ACS 保护副本交由弹性策略安全回收",
        )


def progress_image_releases(
    db: Session, now: datetime, settings: Settings
) -> None:
    services = list(
        db.scalars(
            select(ScalingService).where(
                ScalingService.desired_image.is_not(None),
                ScalingService.release_status.in_(
                    ACTIVE_RELEASE_STATUSES | {"completed"}
                ),
            )
        ).all()
    )
    if not services:
        return
    if not settings.autoscaling_enabled:
        for service in services:
            if service.release_status in ACTIVE_RELEASE_STATUSES:
                _fail_release(db, service, now, "全局 ACS 连接开关未启用")
        return
    try:
        provider = AcsKubernetesClient(settings)
    except RuntimeError as exc:
        for service in services:
            if service.release_status in ACTIVE_RELEASE_STATUSES:
                _fail_release(db, service, now, str(exc))
        return
    try:
        for service in services:
            started = _aware(service.release_started_at)
            if (
                service.release_status in ACTIVE_RELEASE_STATUSES
                and started
                and now - started
                > timedelta(seconds=settings.image_release_timeout_seconds)
            ):
                _fail_release(db, service, now, "分阶段发布超时，已停止后续节点更新")
                continue
            try:
                _progress_service(db, service, now, provider, settings)
            except (httpx.HTTPError, RuntimeError, TypeError, ValueError) as exc:
                if service.release_status in ACTIVE_RELEASE_STATUSES:
                    _fail_release(db, service, now, str(exc))
    finally:
        provider.close()
