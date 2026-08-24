import ipaddress
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from cloudhelm.audit import add_audit
from cloudhelm.dependencies import Admin, Config, Db
from cloudhelm.image_reference import parse_tagged_image, validate_tag_change
from cloudhelm.models import (
    Container,
    Node,
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


def _instance_out(db: Db, instance: ServiceInstance, default_capacity: int) -> dict:
    container = (
        db.get(Container, instance.container_id) if instance.container_id else None
    )
    node = db.get(Node, container.node_id) if container else None
    try:
        gpu_devices = (
            json.loads(container.gpu_devices_json or "[]") if container else []
        )
    except (json.JSONDecodeError, TypeError):
        gpu_devices = []
    try:
        node_gpus = json.loads(node.gpus_json or "[]") if node else []
    except (json.JSONDecodeError, TypeError):
        node_gpus = []
    device_ids = (
        {str(item) for item in gpu_devices} if isinstance(gpu_devices, list) else set()
    )
    assigned_gpus = (
        [
            item
            for item in node_gpus
            if isinstance(item, dict)
            and (
                bool(container and container.gpu_all)
                or str(item.get("index")) in device_ids
                or str(item.get("uuid")) in device_ids
            )
        ]
        if isinstance(node_gpus, list)
        else []
    )
    return {
        "id": instance.id,
        "instance_id": instance.instance_id,
        "container_id": instance.container_id,
        "container_name": container.name if container else None,
        "node_name": node.name if node else None,
        "gpu_all": bool(container and container.gpu_all),
        "gpu_devices": [str(item) for item in gpu_devices]
        if isinstance(gpu_devices, list)
        else [],
        "gpu_names": [
            str(item.get("name") or item.get("uuid") or f"GPU {item.get('index')}")
            for item in assigned_gpus
        ],
        "pool": instance.pool,
        "active_businesses": instance.active_businesses,
        "capacity_limit": instance.capacity_limit,
        "effective_capacity": instance.capacity_limit or default_capacity,
        "healthy": instance.healthy,
        "ready": instance.ready,
        "accepting": instance.accepting,
        "desired_state": instance.desired_state,
        "endpoint_scheme": instance.endpoint_scheme,
        "endpoint_host": _effective_endpoint_host(db, instance),
        "reported_endpoint_host": instance.endpoint_host,
        "endpoint_port": instance.endpoint_port,
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
            _instance_out(db, item, service.capacity_per_replica) for item in instances
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
    settings: Config,
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
    if (
        payload.pool == "acs"
        and payload.endpoint_scheme is not None
        and payload.endpoint_host is None
    ):
        raise HTTPException(status_code=422, detail="ACS 实例必须上报 Pod IP")
    if payload.endpoint_host is not None:
        address = ipaddress.ip_address(payload.endpoint_host)
        if (
            address.is_unspecified
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
        ):
            raise HTTPException(status_code=422, detail="服务地址不可被网关安全路由")
        if not settings.gateway_discovery_networks:
            raise HTTPException(
                status_code=422,
                detail="服务端尚未配置网关可发布地址网段",
            )
        if not any(address in network for network in settings.gateway_discovery_networks):
            raise HTTPException(
                status_code=422,
                detail="服务地址不在允许发布给网关的网段内",
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
    instance.endpoint_scheme = payload.endpoint_scheme
    instance.endpoint_host = payload.endpoint_host
    instance.endpoint_port = payload.endpoint_port
    instance.metadata_json = json.dumps(
        payload.metadata, ensure_ascii=False, separators=(",", ":")
    )
    instance.updated_at = now
    service.last_report_at = now
    db.commit()
    effective_host = _effective_endpoint_host(db, instance)
    return {
        "accepted": True,
        "service_id": service.id,
        "instance_id": instance.instance_id,
        "container_id": instance.container_id,
        "container_linked": instance.container_id is not None,
        "desired_state": instance.desired_state,
        "endpoint_registered": bool(
            effective_host and instance.endpoint_scheme and instance.endpoint_port
        ),
        "report_after_seconds": max(
            5, min(15, service.report_stale_seconds // 3)
        ),
    }


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _discoverable_instance(
    db: Db,
    service: ScalingService,
    instance: ServiceInstance,
    now: datetime,
    settings: Config,
) -> bool:
    host = _effective_endpoint_host(db, instance)
    if (
        not instance.monitored
        or not instance.healthy
        or not instance.ready
        or not instance.accepting
        or instance.desired_state != "serve"
        or not instance.endpoint_scheme
        or not host
        or not instance.endpoint_port
        or _aware(instance.updated_at)
        < now - timedelta(seconds=service.report_stale_seconds)
    ):
        return False
    address = ipaddress.ip_address(host)
    if not any(address in network for network in settings.gateway_discovery_networks):
        return False
    if not instance.container_id:
        return True
    container = db.get(Container, instance.container_id)
    return bool(
        container
        and container.present
        and container.status == "running"
        and container.health != "unhealthy"
    )


def _effective_endpoint_host(db: Db, instance: ServiceInstance) -> str | None:
    if instance.pool == "local" and instance.container_id:
        container = db.get(Container, instance.container_id)
        node = db.get(Node, container.node_id) if container else None
        if node and node.gateway_address:
            return node.gateway_address
    return instance.endpoint_host


def _endpoint_url(instance: ServiceInstance, host: str) -> str:
    formatted_host = f"[{host}]" if ":" in host else host
    return f"{instance.endpoint_scheme}://{formatted_host}:{instance.endpoint_port}"


@router.get("/service-discovery/routes")
def discover_gateway_routes(
    request: Request,
    db: Db,
    settings: Config,
    x_cloudhelm_gateway_token: Annotated[str | None, Header()] = None,
) -> Response:
    if not settings.gateway_discovery_token:
        raise HTTPException(status_code=503, detail="服务端尚未配置网关发现令牌")
    if not secrets.compare_digest(
        digest_token(x_cloudhelm_gateway_token or ""),
        digest_token(settings.gateway_discovery_token),
    ):
        raise HTTPException(status_code=401, detail="网关发现凭据无效")

    now = datetime.now(UTC)
    services = list(db.scalars(select(ScalingService).order_by(ScalingService.name)))
    routes = []
    for service in services:
        instances = db.scalars(
            select(ServiceInstance)
            .where(ServiceInstance.service_id == service.id)
            .order_by(ServiceInstance.pool, ServiceInstance.instance_id)
        ).all()
        endpoints = []
        for instance in instances:
            if not _discoverable_instance(db, service, instance, now, settings):
                continue
            host = _effective_endpoint_host(db, instance)
            if not host:
                continue
            endpoints.append(
                {
                "instance_id": instance.instance_id,
                "pool": instance.pool,
                "scheme": instance.endpoint_scheme,
                "host": host,
                "port": instance.endpoint_port,
                "url": _endpoint_url(instance, host),
                "expires_at": (
                    _aware(instance.updated_at)
                    + timedelta(seconds=service.report_stale_seconds)
                ).isoformat(),
                }
            )
        routes.append(
            {
                "service_id": service.id,
                "service_name": service.name,
                "service_type": service.service_type,
                "endpoints": endpoints,
            }
        )

    generation = digest_token(
        json.dumps(routes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    etag = f'"{generation}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "no-store",
        "X-CloudHelm-Poll-After": "2",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return JSONResponse(
        {
            "generation": generation,
            "generated_at": now.isoformat(),
            "authoritative": True,
            "poll_after_seconds": 2,
            "services": routes,
        },
        headers=headers,
    )


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
    return _instance_out(db, instance, service.capacity_per_replica)


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
    return _instance_out(db, instance, service.capacity_per_replica)


@router.delete(
    "/scaling/instances/{instance_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_service_instance(
    instance_id: str,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> Response:
    instance = db.get(ServiceInstance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="服务实例不存在")
    service = db.get(ScalingService, instance.service_id)
    if not service:
        raise HTTPException(status_code=404, detail="伸缩服务不存在")
    if service.release_status in ACTIVE_RELEASE_STATUSES:
        raise HTTPException(status_code=409, detail="镜像正在分阶段发布，不能删除实例")
    if instance.container_id:
        raise HTTPException(
            status_code=409,
            detail="该实例仍关联本地容器；请先修正或移除容器关联标签",
        )
    add_audit(
        db,
        action="scaling.instance.delete",
        target_type="scaling_service",
        target_id=service.id,
        target_name=f"{service.name}/{instance.instance_id}",
        user=admin,
        detail=(
            f"instance_id={instance.instance_id},pool={instance.pool},"
            f"active_businesses={instance.active_businesses}"
        ),
        request=request,
        settings=settings,
    )
    db.delete(instance)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
