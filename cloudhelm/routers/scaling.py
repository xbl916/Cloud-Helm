import json
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select

from cloudhelm.audit import add_audit
from cloudhelm.dependencies import Admin, Config, Db
from cloudhelm.image_reference import parse_tagged_image, validate_tag_change
from cloudhelm.models import (
    Container,
    ScalingEvent,
    ScalingService,
    ServiceInstance,
)
from cloudhelm.releases import ACTIVE_RELEASE_STATUSES, resolve_local_container
from cloudhelm.schemas import (
    BusinessMetricReport,
    ModuleReleaseInput,
    ScalingServiceInput,
    ServiceInstanceCapacityInput,
    ServiceInstanceMonitoringInput,
)
from cloudhelm.security import digest_token, new_opaque_token

router = APIRouter(tags=["business-load-and-scaling"])


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _instance_out(instance: ServiceInstance, default_capacity: int) -> dict:
    return {
        "id": instance.id,
        "instance_id": instance.instance_id,
        "container_id": instance.container_id,
        "pool": instance.pool,
        "active_businesses": instance.active_businesses,
        "capacity_limit": instance.capacity_limit,
        "effective_capacity": instance.capacity_limit or default_capacity,
        "healthy": instance.healthy,
        "ready": instance.ready,
        "accepting": instance.accepting,
        "desired_state": instance.desired_state,
        "monitored": instance.monitored,
        "updated_at": _iso(instance.updated_at),
    }


def _service_out(db: Db, service: ScalingService, include_instances: bool = True) -> dict:
    result = {
        "id": service.id,
        "name": service.name,
        "service_type": service.service_type,
        "capacity_per_replica": service.capacity_per_replica,
        "autoscaling_enabled": service.autoscaling_enabled,
        "min_replicas": service.min_replicas,
        "max_replicas": service.max_replicas,
        "scale_up_threshold_percent": service.scale_up_threshold_percent,
        "scale_down_threshold_percent": service.scale_down_threshold_percent,
        "window_seconds": service.window_seconds,
        "cooldown_seconds": service.cooldown_seconds,
        "report_stale_seconds": service.report_stale_seconds,
        "scale_up_step": service.scale_up_step,
        "failover_replicas": service.failover_replicas,
        "provider": service.provider,
        "namespace": service.namespace,
        "workload_kind": service.workload_kind,
        "workload_name": service.workload_name,
        "acs_container_name": service.acs_container_name,
        "desired_image": service.desired_image,
        "previous_image": service.previous_image,
        "release_status": service.release_status,
        "release_error": service.release_error,
        "release_task_id": service.release_task_id,
        "release_started_at": _iso(service.release_started_at),
        "release_updated_at": _iso(service.release_updated_at),
        "current_replicas": service.current_replicas,
        "desired_replicas": service.desired_replicas,
        "current_businesses": service.current_businesses,
        "average_businesses": round(service.average_businesses, 2),
        "utilization_percent": round(service.utilization_percent, 2),
        "reporting_instances": service.reporting_instances,
        "status": service.status,
        "last_error": service.last_error,
        "last_report_at": _iso(service.last_report_at),
        "last_evaluated_at": _iso(service.last_evaluated_at),
        "last_scaled_at": _iso(service.last_scaled_at),
        "updated_at": _iso(service.updated_at),
    }
    if include_instances:
        instances = db.scalars(
            select(ServiceInstance)
            .where(ServiceInstance.service_id == service.id)
            .order_by(ServiceInstance.pool, ServiceInstance.instance_id)
        ).all()
        result["instances"] = [
            _instance_out(item, service.capacity_per_replica) for item in instances
        ]
    return result


def _apply_service_input(service: ScalingService, payload: ScalingServiceInput) -> None:
    for field, value in payload.model_dump().items():
        setattr(service, field, value)
    service.updated_at = datetime.now(UTC)


@router.post("/service-metrics/{service_id}/report")
def report_business_load(
    service_id: str,
    payload: BusinessMetricReport,
    db: Db,
    x_cloudhelm_service_token: Annotated[str | None, Header()] = None,
) -> dict:
    service = db.get(ScalingService, service_id)
    supplied_hash = digest_token(x_cloudhelm_service_token or "")
    if not service or not secrets.compare_digest(
        supplied_hash, service.report_token_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="业务指标上报凭据无效",
        )
    if payload.pool == "acs" and payload.container_id:
        raise HTTPException(
            status_code=422,
            detail="ACS 实例没有主机 Agent，不能关联本地容器",
        )
    container = None
    if payload.container_id:
        container = db.get(Container, payload.container_id)
        if not container:
            raise HTTPException(status_code=422, detail="关联容器不存在")
    instance = db.scalar(
        select(ServiceInstance).where(
            ServiceInstance.service_id == service.id,
            ServiceInstance.instance_id == payload.instance_id,
        )
    )
    if not instance:
        instance = ServiceInstance(
            service_id=service.id,
            instance_id=payload.instance_id,
        )
        db.add(instance)
    elif instance.pool != payload.pool:
        raise HTTPException(
            status_code=409,
            detail="同一实例 ID 不能在 local 与 acs 实例池之间切换",
        )
    now = datetime.now(UTC)
    if payload.pool == "local" and container:
        instance.container_id = container.id
    elif payload.pool == "acs":
        instance.container_id = None
    instance.active_businesses = payload.active_businesses
    instance.pool = payload.pool
    instance.healthy = payload.healthy
    instance.ready = payload.ready
    instance.accepting = payload.accepting
    instance.metadata_json = json.dumps(
        payload.metadata, ensure_ascii=False, separators=(",", ":")
    )
    instance.updated_at = now
    service.last_report_at = now
    db.commit()
    return {
        "accepted": True,
        "service_id": service.id,
        "instance_id": instance.instance_id,
        "container_id": instance.container_id,
        "container_linked": instance.container_id is not None,
        "desired_state": instance.desired_state,
        "report_after_seconds": 15,
    }


@router.get("/scaling/services")
def list_scaling_services(db: Db, admin: Admin) -> list[dict]:
    return [
        _service_out(db, service)
        for service in db.scalars(select(ScalingService).order_by(ScalingService.name)).all()
    ]


@router.post("/scaling/services", status_code=status.HTTP_201_CREATED)
def create_scaling_service(
    payload: ScalingServiceInput,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> dict:
    if db.scalar(select(ScalingService.id).where(ScalingService.name == payload.name)):
        raise HTTPException(status_code=409, detail="服务名称已存在")
    raw_token = new_opaque_token()
    service = ScalingService(report_token_hash=digest_token(raw_token))
    _apply_service_input(service, payload)
    db.add(service)
    db.flush()
    add_audit(
        db,
        action="scaling.service.create",
        target_type="scaling_service",
        target_id=service.id,
        target_name=service.name,
        user=admin,
        detail=f"type={service.service_type}, acs={service.namespace}/{service.workload_name}",
        request=request,
        settings=settings,
    )
    db.commit()
    result = _service_out(db, service)
    result["report_token"] = raw_token
    return result


@router.put("/scaling/services/{service_id}")
def update_scaling_service(
    service_id: str,
    payload: ScalingServiceInput,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> dict:
    service = db.get(ScalingService, service_id)
    if not service:
        raise HTTPException(status_code=404, detail="伸缩服务不存在")
    if service.release_status in ACTIVE_RELEASE_STATUSES:
        raise HTTPException(status_code=409, detail="镜像正在分阶段发布，暂时不能修改模块配置")
    duplicate = db.scalar(
        select(ScalingService.id).where(
            ScalingService.name == payload.name,
            ScalingService.id != service.id,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="服务名称已存在")
    _apply_service_input(service, payload)
    add_audit(
        db,
        action="scaling.service.update",
        target_type="scaling_service",
        target_id=service.id,
        target_name=service.name,
        user=admin,
        detail=(
            f"autoscaling={service.autoscaling_enabled}, "
            f"thresholds={service.scale_up_threshold_percent}/{service.scale_down_threshold_percent}"
        ),
        request=request,
        settings=settings,
    )
    db.commit()
    return _service_out(db, service)


def _start_image_release(
    db: Db,
    service: ScalingService,
    target_image: str,
    admin: Admin,
    now: datetime,
) -> str:
    if service.release_status in ACTIVE_RELEASE_STATUSES:
        raise HTTPException(status_code=409, detail="已有镜像发布正在进行")
    try:
        target = parse_tagged_image(target_image)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    local_instances = list(
        db.scalars(
            select(ServiceInstance).where(
                ServiceInstance.service_id == service.id,
                ServiceInstance.pool == "local",
                ServiceInstance.monitored.is_(True),
                ServiceInstance.container_id.is_not(None),
            )
        ).all()
    )
    if not local_instances:
        raise HTTPException(status_code=409, detail="模块没有关联且受监控的本地实例")
    local_images: list[str] = []
    for instance in local_instances:
        container = resolve_local_container(db, instance)
        if not container:
            raise HTTPException(
                status_code=409,
                detail=f"本地实例 {instance.instance_id} 的容器不可用",
            )
        if container.status != "running" or container.health == "unhealthy":
            raise HTTPException(
                status_code=409,
                detail=f"本地实例 {instance.instance_id} 当前不可健康替换",
            )
        try:
            validate_tag_change(container.image, target.canonical)
        except ValueError as exc:
            if not _same_image(container.image, target.canonical):
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        local_images.append(container.image)
    if all(_same_image(image, target.canonical) for image in local_images):
        raise HTTPException(status_code=409, detail="全部本地实例已经使用目标镜像")
    service.previous_image = service.desired_image or local_images[0]
    service.desired_image = target.canonical
    service.release_status = "preparing_acs"
    service.release_error = None
    service.release_requested_by = admin.id
    service.release_task_id = None
    service.release_acs_original_replicas = None
    service.release_started_at = now
    service.release_updated_at = now
    return target.canonical


def _same_image(left: str, right: str) -> bool:
    try:
        return parse_tagged_image(left).canonical == parse_tagged_image(right).canonical
    except ValueError:
        return False


@router.post("/scaling/services/{service_id}/release", status_code=status.HTTP_202_ACCEPTED)
def start_image_release(
    service_id: str,
    payload: ModuleReleaseInput,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> dict:
    service = db.get(ScalingService, service_id)
    if not service:
        raise HTTPException(status_code=404, detail="伸缩服务不存在")
    if not settings.autoscaling_enabled:
        raise HTTPException(status_code=409, detail="请先启用全局 ACS 连接开关")
    target = _start_image_release(
        db, service, payload.target_image, admin, datetime.now(UTC)
    )
    add_audit(
        db,
        action="scaling.service.image.release",
        target_type="scaling_service",
        target_id=service.id,
        target_name=service.name,
        user=admin,
        detail=f"image={service.previous_image}->{target}",
        request=request,
        settings=settings,
    )
    db.commit()
    return _service_out(db, service)


@router.post("/scaling/services/{service_id}/release/rollback", status_code=status.HTTP_202_ACCEPTED)
def rollback_image_release(
    service_id: str,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> dict:
    service = db.get(ScalingService, service_id)
    if not service:
        raise HTTPException(status_code=404, detail="伸缩服务不存在")
    if not service.previous_image:
        raise HTTPException(status_code=409, detail="没有可回滚的上一版本")
    if not settings.autoscaling_enabled:
        raise HTTPException(status_code=409, detail="请先启用全局 ACS 连接开关")
    target = _start_image_release(
        db, service, service.previous_image, admin, datetime.now(UTC)
    )
    add_audit(
        db,
        action="scaling.service.image.rollback",
        target_type="scaling_service",
        target_id=service.id,
        target_name=service.name,
        user=admin,
        detail=f"target_image={target}",
        request=request,
        settings=settings,
    )
    db.commit()
    return _service_out(db, service)


@router.post("/scaling/services/{service_id}/report-token/rotate")
def rotate_report_token(
    service_id: str,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> dict:
    service = db.get(ScalingService, service_id)
    if not service:
        raise HTTPException(status_code=404, detail="伸缩服务不存在")
    raw_token = new_opaque_token()
    service.report_token_hash = digest_token(raw_token)
    add_audit(
        db,
        action="scaling.service.token.rotate",
        target_type="scaling_service",
        target_id=service.id,
        target_name=service.name,
        user=admin,
        request=request,
        settings=settings,
    )
    db.commit()
    return {"service_id": service.id, "report_token": raw_token}


@router.put("/scaling/instances/{instance_id}/capacity")
def update_instance_capacity(
    instance_id: str,
    payload: ServiceInstanceCapacityInput,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> dict:
    instance = db.get(ServiceInstance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="服务实例不存在")
    service = db.get(ScalingService, instance.service_id)
    if not service:
        raise HTTPException(status_code=404, detail="伸缩服务不存在")
    instance.capacity_limit = payload.capacity_limit
    add_audit(
        db,
        action="scaling.instance.capacity.update",
        target_type="scaling_service",
        target_id=service.id,
        target_name=f"{service.name}/{instance.instance_id}",
        user=admin,
        detail=f"capacity={payload.capacity_limit or 'default'}",
        request=request,
        settings=settings,
    )
    db.commit()
    return _instance_out(instance, service.capacity_per_replica)


@router.put("/scaling/instances/{instance_id}/monitoring")
def update_instance_monitoring(
    instance_id: str,
    payload: ServiceInstanceMonitoringInput,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> dict:
    instance = db.get(ServiceInstance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="服务实例不存在")
    service = db.get(ScalingService, instance.service_id)
    if not service:
        raise HTTPException(status_code=404, detail="伸缩服务不存在")
    instance.monitored = payload.monitored
    if not payload.monitored:
        instance.desired_state = "serve"
        instance.drain_requested_at = None
    add_audit(
        db,
        action="scaling.instance.monitoring.update",
        target_type="scaling_service",
        target_id=service.id,
        target_name=f"{service.name}/{instance.instance_id}",
        user=admin,
        detail=f"monitored={payload.monitored}",
        request=request,
        settings=settings,
    )
    db.commit()
    return _instance_out(instance, service.capacity_per_replica)


@router.get("/scaling/events")
def list_scaling_events(db: Db, admin: Admin, limit: int = 100) -> list[dict]:
    limit = min(500, max(1, limit))
    services = {item.id: item.name for item in db.scalars(select(ScalingService)).all()}
    events = db.scalars(
        select(ScalingEvent).order_by(ScalingEvent.created_at.desc()).limit(limit)
    ).all()
    return [
        {
            "id": item.id,
            "service_id": item.service_id,
            "service_name": services.get(item.service_id, "未知服务"),
            "action": item.action,
            "status": item.status,
            "from_replicas": item.from_replicas,
            "to_replicas": item.to_replicas,
            "average_businesses": round(item.average_businesses, 2),
            "utilization_percent": round(item.utilization_percent, 2),
            "detail": item.detail,
            "created_at": _iso(item.created_at),
        }
        for item in events
    ]
