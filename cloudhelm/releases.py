import json
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from cloudhelm.autoscaling import AcsKubernetesClient
from cloudhelm.config import Settings
from cloudhelm.image_reference import parse_tagged_image
from cloudhelm.models import (
    Container,
    ImageRelease,
    ImageReleaseStep,
    Node,
    ScalingEvent,
    ScalingService,
    ServiceImageVersion,
    ServiceInstance,
    Task,
    TaskStatus,
)

ACTIVE_RELEASE_STATUSES = {
    "preflighting_local",
    "preparing_guard",
    "waiting_guard",
    "draining_local",
    "preparing_acs",
    "waiting_acs",
    "updating_acs",
    "updating_local",
    "verifying_local",
    "cleaning_guard",
}


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _release_event(
    db: Session,
    service: ScalingService,
    status: str,
    detail: str,
    action: str = "image_release",
    from_replicas: int | None = None,
    to_replicas: int | None = None,
) -> None:
    db.add(
        ScalingEvent(
            service_id=service.id,
            action=action,
            status=status,
            from_replicas=(
                service.current_replicas
                if from_replicas is None
                else from_replicas
            ),
            to_replicas=(
                service.desired_replicas if to_replicas is None else to_replicas
            ),
            average_businesses=service.average_businesses,
            utilization_percent=service.utilization_percent,
            detail=detail[:500],
        )
    )


def _active_release(db: Session, service: ScalingService) -> ImageRelease | None:
    if not service.active_release_id:
        return None
    return db.get(ImageRelease, service.active_release_id)


def _sync_release(
    db: Session,
    service: ScalingService,
    now: datetime,
    *,
    status: str,
    stage: str,
    error: str | None = None,
) -> None:
    release = _active_release(db, service)
    if not release:
        return
    release.status = status
    release.stage = stage
    release.error = error[:500] if error else None
    release.updated_at = now
    if status == "completed":
        release.completed_at = now


def _release_step(
    db: Session, service: ScalingService, instance: ServiceInstance
) -> ImageReleaseStep | None:
    if not service.active_release_id:
        return None
    return db.scalar(
        select(ImageReleaseStep).where(
            ImageReleaseStep.release_id == service.active_release_id,
            ImageReleaseStep.service_instance_id == instance.id,
        )
    )


def _record_version(
    db: Session,
    service: ScalingService,
    image: str,
    now: datetime,
    release: ImageRelease | None,
) -> None:
    version = db.scalar(
        select(ServiceImageVersion).where(
            ServiceImageVersion.service_id == service.id,
            ServiceImageVersion.image == image,
        )
    )
    if not version:
        version = ServiceImageVersion(service_id=service.id, image=image)
        db.add(version)
    version.release_id = release.id if release else None
    version.activated_by = release.requested_by if release else None
    version.activated_at = now


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
    release = _active_release(db, service)
    if release:
        step = db.scalar(
            select(ImageReleaseStep).where(
                ImageReleaseStep.release_id == release.id,
                ImageReleaseStep.status.in_(
                    ["preparing", "draining", "updating", "verifying"]
                ),
            )
        )
        if step:
            step.status = "failed"
            step.error = error[:500]
            step.updated_at = now
            instance = (
                db.get(ServiceInstance, step.service_instance_id)
                if step.service_instance_id
                else None
            )
            if instance and instance.desired_state in {"draining", "maintenance"}:
                instance.desired_state = "restoring"
        _sync_release(
            db, service, now, status="failed", stage=service.release_status, error=error
        )
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


def _release_steps(db: Session, service: ScalingService) -> list[ImageReleaseStep]:
    if not service.active_release_id:
        return []
    return list(
        db.scalars(
            select(ImageReleaseStep)
            .where(ImageReleaseStep.release_id == service.active_release_id)
            .order_by(ImageReleaseStep.position)
        ).all()
    )


def _step_instance(db: Session, step: ImageReleaseStep) -> ServiceInstance | None:
    return (
        db.get(ServiceInstance, step.service_instance_id)
        if step.service_instance_id
        else None
    )


def _queue_local_task(
    db: Session,
    service: ScalingService,
    step: ImageReleaseStep,
    instance: ServiceInstance,
    container: Container,
    action: str,
    now: datetime,
    node_offline_seconds: int,
) -> None:
    if not service.release_requested_by:
        raise RuntimeError("发布操作人不存在，无法创建本地任务")
    node = db.get(Node, container.node_id)
    last_seen = _aware(node.last_seen_at) if node else None
    if (
        not node
        or not last_seen
        or (now - last_seen).total_seconds() > node_offline_seconds
    ):
        raise RuntimeError(f"节点离线：{instance.instance_id}")
    active_task = db.scalar(
        select(Task.id).where(
            Task.container_id == container.id,
            Task.status.in_([TaskStatus.pending, TaskStatus.dispatched]),
        )
    )
    if active_task:
        raise RuntimeError(f"实例 {instance.instance_id} 已有其他任务正在执行")
    arguments = {
        "expected_image": container.image,
        "target_image": service.desired_image,
    }
    if action == "update_image":
        arguments["startup_health_seconds"] = 300
        if container.compose_project and container.compose_service:
            arguments["require_compose_update"] = True
    task = Task(
        node_id=container.node_id,
        container_id=container.id,
        docker_id=container.docker_id,
        action=action,
        arguments_json=json.dumps(arguments),
        requested_by=service.release_requested_by,
    )
    db.add(task)
    db.flush()
    step.task_id = task.id
    step.started_at = step.started_at or now
    step.updated_at = now
    service.release_task_id = task.id
    service.release_updated_at = now


def _progress_preflight(
    db: Session,
    service: ScalingService,
    now: datetime,
    settings: Settings,
) -> None:
    steps = _release_steps(db, service)
    if service.release_task_id:
        task = db.get(Task, service.release_task_id)
        if not task:
            _fail_release(db, service, now, "镜像准备任务记录不存在")
            return
        if task.status in {TaskStatus.pending, TaskStatus.dispatched}:
            return
        step = db.scalar(
            select(ImageReleaseStep).where(ImageReleaseStep.task_id == task.id)
        )
        if task.status != TaskStatus.success or not step:
            _fail_release(db, service, now, task.error or "目标镜像准备失败")
            return
        step.status = "prepared"
        step.stage = "image_prepared"
        step.task_id = None
        step.updated_at = now
        service.release_task_id = None
        _release_event(db, service, "progress", "目标镜像与 Compose 配置预检查通过")
        return

    for step in steps:
        if step.status != "pending":
            continue
        instance = _step_instance(db, step)
        container = resolve_local_container(db, instance) if instance else None
        if not instance or not container:
            _fail_release(db, service, now, "本地发布实例或容器不存在")
            return
        if _image_matches(container.image, service.desired_image or ""):
            step.status = "completed"
            step.stage = "already_current"
            step.completed_at = now
            continue
        try:
            _queue_local_task(
                db,
                service,
                step,
                instance,
                container,
                "prepare_image",
                now,
                settings.node_offline_seconds,
            )
        except RuntimeError as exc:
            _fail_release(db, service, now, str(exc))
            return
        step.status = "preparing"
        step.stage = "pulling_image"
        _sync_release(db, service, now, status="running", stage="preflighting_local")
        return

    service.release_status = "preparing_guard" if len(steps) == 1 else "draining_local"
    _sync_release(db, service, now, status="running", stage=service.release_status)


def _guard_instances(
    db: Session, service: ScalingService, release: ImageRelease
) -> list[ServiceInstance]:
    candidates = db.scalars(
        select(ServiceInstance).where(
            ServiceInstance.service_id == service.id,
            ServiceInstance.pool == "acs_release",
            ServiceInstance.monitored.is_(True),
        )
    ).all()
    result = []
    for instance in candidates:
        try:
            metadata = json.loads(instance.metadata_json or "{}")
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        if metadata.get("release_id") == release.id:
            result.append(instance)
    return result


def _guard_is_serving(
    db: Session, service: ScalingService, release: ImageRelease, now: datetime
) -> bool:
    stale_before = now - timedelta(seconds=service.report_stale_seconds)
    return any(
        instance.healthy
        and instance.ready
        and instance.accepting
        and _aware(instance.updated_at) >= stale_before
        and instance.endpoint_scheme
        and instance.endpoint_host
        and instance.endpoint_port
        for instance in _guard_instances(db, service, release)
    )


def _progress_local_rollout(
    db: Session, service: ScalingService, now: datetime, settings: Settings
) -> bool:
    target = service.desired_image or ""
    steps = _release_steps(db, service)
    if service.release_task_id:
        task = db.get(Task, service.release_task_id)
        if not task:
            _fail_release(db, service, now, "本地发布任务记录不存在")
            return False
        if task.status in {TaskStatus.pending, TaskStatus.dispatched}:
            return False
        step = db.scalar(
            select(ImageReleaseStep).where(ImageReleaseStep.task_id == task.id)
        )
        instance = _step_instance(db, step) if step else None
        if task.status != TaskStatus.success or not step or not instance:
            _fail_release(db, service, now, task.error or "本地镜像更新任务失败")
            return False
        container = resolve_local_container(db, instance)
        if not container or not _image_matches(container.image, target):
            _fail_release(db, service, now, "本地容器没有切换到目标镜像")
            return False
        instance.desired_state = "restoring"
        step.status = "verifying"
        step.stage = "waiting_business_ready"
        step.compose_status = (
            "updated"
            if json.loads(task.arguments_json or "{}").get("require_compose_update")
            else "not_managed"
        )
        service.release_status = "verifying_local"
        _sync_release(db, service, now, status="running", stage="verifying_local")
        finished_at = _aware(task.finished_at)
        reported_at = _aware(instance.updated_at)
        if not (
            container.status == "running"
            and container.health != "unhealthy"
            and instance.healthy
            and instance.ready
            and instance.accepting
            and finished_at
            and reported_at
            and reported_at >= finished_at
        ):
            return False
        instance.desired_state = "serve"
        instance.drain_requested_at = None
        step.status = "completed"
        step.stage = "healthy"
        step.completed_at = now
        step.task_id = None
        service.release_task_id = None
        service.release_status = "draining_local"
        _release_event(
            db,
            service,
            "progress",
            f"本地实例 {instance.instance_id} 已使用新镜像并恢复健康上报",
        )
        return False

    for step in steps:
        if step.status == "completed":
            continue
        instance = _step_instance(db, step)
        container = resolve_local_container(db, instance) if instance else None
        if not instance or not container:
            _fail_release(db, service, now, "本地发布实例或容器不存在")
            return False
        if _image_matches(container.image, target):
            instance.desired_state = "serve"
            step.status = "completed"
            step.stage = "healthy"
            step.completed_at = now
            continue
        if step.status in {"prepared", "pending", "preparing"}:
            instance.desired_state = "draining"
            instance.drain_requested_at = now
            step.status = "draining"
            step.stage = "waiting_active_businesses"
            service.release_status = "draining_local"
            _sync_release(db, service, now, status="running", stage="draining_local")
            _release_event(
                db,
                service,
                "progress",
                f"本地实例 {instance.instance_id} 已从网关摘除，等待在途业务归零",
            )
            return False
        if step.status == "draining":
            if not (
                instance.desired_state == "maintenance"
                and not instance.accepting
                and instance.active_businesses == 0
            ):
                return False
            try:
                _queue_local_task(
                    db,
                    service,
                    step,
                    instance,
                    container,
                    "update_image",
                    now,
                    settings.node_offline_seconds,
                )
            except RuntimeError as exc:
                _fail_release(db, service, now, str(exc))
                return False
            step.status = "updating"
            step.stage = "updating_container"
            service.release_status = "updating_local"
            _sync_release(db, service, now, status="running", stage="updating_local")
            return False
        return False
    return True


def _complete_release(
    db: Session, service: ScalingService, release: ImageRelease, now: datetime
) -> None:
    target = service.desired_image or release.target_image
    service.release_status = "completed"
    service.release_error = None
    service.release_task_id = None
    service.release_updated_at = now
    _record_version(db, service, target, now, release)
    _sync_release(db, service, now, status="completed", stage="completed")
    _release_event(db, service, "success", "本地实例、Compose 与高负载 ACS 模板已统一到目标镜像")


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
    release = _active_release(db, service)
    if not release:
        _fail_release(db, service, now, "缺少持久化发布单，请重新发起发布")
        return
    if service.release_status == "preflighting_local":
        _progress_preflight(db, service, now, settings)
        return
    if service.release_status == "preparing_guard":
        name = provider.ensure_release_guard(service, release.id, release.source_image)
        release.protection_instance_id = name
        service.release_status = "waiting_guard"
        _sync_release(db, service, now, status="running", stage="waiting_guard")
        _release_event(
            db,
            service,
            "success",
            f"旧版本升级保护实例 {name} 已创建",
            action="release_guard_scale_up",
            from_replicas=0,
            to_replicas=1,
        )
        return
    if service.release_status == "waiting_guard":
        name = release.protection_instance_id or ""
        if not name or not provider.release_guard_ready(service, name, release.source_image):
            return
        if not _guard_is_serving(db, service, release, now):
            return
        service.release_status = "draining_local"
        _sync_release(db, service, now, status="running", stage="draining_local")
        _release_event(db, service, "progress", "旧版本升级保护实例已正常接单，开始排空本地实例")
        return
    if service.release_status in {"draining_local", "updating_local", "verifying_local"}:
        if not _progress_local_rollout(db, service, now, settings):
            return
        service.release_status = "updating_acs"
        _sync_release(db, service, now, status="running", stage="updating_acs")
        return
    if service.release_status in {"updating_acs", "preparing_acs"}:
        service.desired_image = provider.set_workload_image(service, target)
        service.release_status = "waiting_acs"
        _sync_release(db, service, now, status="running", stage="waiting_acs")
        _release_event(db, service, "progress", "高负载弹性模板已更新，等待现有副本滚动就绪")
        return
    if service.release_status == "waiting_acs":
        ready, _, _ = provider.workload_image_status(service, target)
        if not ready:
            return
        if release.protection_instance_id:
            service.release_status = "cleaning_guard"
            _sync_release(db, service, now, status="running", stage="cleaning_guard")
            return
        _complete_release(db, service, release, now)
        return
    if service.release_status == "cleaning_guard":
        guards = _guard_instances(db, service, release)
        if guards:
            for instance in guards:
                if instance.desired_state == "serve":
                    instance.desired_state = "draining"
                    instance.drain_requested_at = now
            if not all(
                instance.desired_state == "maintenance"
                and not instance.accepting
                and instance.active_businesses == 0
                for instance in guards
            ):
                return
        provider.delete_release_guard(service, release.protection_instance_id)
        for instance in guards:
            instance.monitored = False
        _release_event(
            db,
            service,
            "success",
            "旧版本升级保护实例已排空并删除",
            action="release_guard_scale_down",
            from_replicas=1,
            to_replicas=0,
        )
        _complete_release(db, service, release, now)


def progress_image_releases(
    db: Session, now: datetime, settings: Settings
) -> None:
    services = list(
        db.scalars(
            select(ScalingService).where(
                ScalingService.desired_image.is_not(None),
                ScalingService.release_status.in_(ACTIVE_RELEASE_STATUSES),
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
