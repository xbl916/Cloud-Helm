import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import delete, select, update

from cloudhelm.access import (
    can_access,
    can_manage_resources,
    can_view_node_metrics,
    load_access_rules,
    visible_inventory,
)
from cloudhelm.audit import add_audit
from cloudhelm.dependencies import Admin, Config, CurrentUser, Db
from cloudhelm.image_reference import validate_tag_change
from cloudhelm.models import (
    AccessRule,
    AlertEvent,
    AlertRule,
    AlertState,
    AuditLog,
    Container,
    MetricSample,
    Node,
    Task,
    User,
    UserRole,
    WebSession,
)
from cloudhelm.schemas import (
    AccessConfigInput,
    AccessRuleInput,
    ActionRequest,
    AlertRuleInput,
    TaskOut,
    UserCreate,
    UserUpdate,
)

router = APIRouter(tags=["console"])


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _node_online(node: Node, offline_seconds: int) -> bool:
    if not node.last_seen_at:
        return False
    last_seen = node.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    return (datetime.now(UTC) - last_seen).total_seconds() <= offline_seconds


def _node_gpus(node: Node) -> list[dict]:
    try:
        value = json.loads(node.gpus_json or "[]")
        return value if isinstance(value, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _node_system_metrics(node: Node) -> dict:
    try:
        value = json.loads(node.system_metrics_json or "{}")
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _container_gpu_devices(container: Container) -> list[str]:
    try:
        value = json.loads(container.gpu_devices_json or "[]")
        return [str(item) for item in value] if isinstance(value, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _container_has_gpu(container: Container) -> bool:
    return container.gpu_all or bool(_container_gpu_devices(container))


def _assigned_gpus(node: Node, containers: list[Container]) -> list[dict]:
    gpus = _node_gpus(node)
    if any(item.gpu_all for item in containers):
        return gpus
    device_ids = {
        device
        for item in containers
        for device in _container_gpu_devices(item)
        if not device.startswith("count:")
    }
    return [
        gpu
        for gpu in gpus
        if str(gpu.get("index")) in device_ids or gpu.get("uuid") in device_ids
    ]


def _visible_node_gpus(
    user: User,
    rules: list[AccessRule],
    node: Node,
    visible_containers: list[Container],
) -> list[dict]:
    if can_view_node_metrics(user, rules, node):
        return _node_gpus(node)
    return _assigned_gpus(node, visible_containers)


@router.get("/dashboard")
def dashboard(db: Db, settings: Config, user: CurrentUser) -> dict:
    nodes, containers, rules = visible_inventory(db, user)
    online_nodes = sum(
        _node_online(node, settings.node_offline_seconds) for node in nodes
    )
    running = sum(item.status == "running" for item in containers)
    unhealthy = sum(item.health == "unhealthy" for item in containers)
    gpus = [
        gpu
        for node in nodes
        for gpu in _visible_node_gpus(
            user,
            rules,
            node,
            [item for item in containers if item.node_id == node.id],
        )
    ]
    gpu_utilization = [
        float(gpu["utilization_gpu"])
        for gpu in gpus
        if gpu.get("utilization_gpu") is not None
    ]
    system_metrics = [
        _node_system_metrics(node)
        for node in nodes
        if can_view_node_metrics(user, rules, node)
        and node.system_metrics_status == "ok"
    ]
    return {
        "nodes": {
            "total": len(nodes),
            "online": online_nodes,
            "offline": len(nodes) - online_nodes,
        },
        "containers": {
            "total": len(containers),
            "running": running,
            "stopped": len(containers) - running,
            "unhealthy": unhealthy,
        },
        "gpus": {
            "total": len(gpus),
            "active": sum(value > 0 for value in gpu_utilization),
            "average_utilization": round(sum(gpu_utilization) / len(gpu_utilization), 2)
            if gpu_utilization
            else 0.0,
            "memory_used_mib": sum(
                int(gpu.get("memory_used_mib") or 0) for gpu in gpus
            ),
            "memory_total_mib": sum(
                int(gpu.get("memory_total_mib") or 0) for gpu in gpus
            ),
        },
        "system": {
            "disk_used_bytes": sum(
                int(item.get("disk_used_bytes") or 0) for item in system_metrics
            ),
            "disk_total_bytes": sum(
                int(item.get("disk_total_bytes") or 0) for item in system_metrics
            ),
            "network_rx_bps": round(
                sum(float(item.get("network_rx_bps") or 0) for item in system_metrics),
                2,
            ),
            "network_tx_bps": round(
                sum(float(item.get("network_tx_bps") or 0) for item in system_metrics),
                2,
            ),
            "node_count": len(system_metrics),
        },
    }


@router.get("/nodes")
def list_nodes(db: Db, settings: Config, user: CurrentUser) -> list[dict]:
    nodes, containers, rules = visible_inventory(db, user)
    counts: dict[str, int] = {}
    running: dict[str, int] = {}
    for item in containers:
        counts[item.node_id] = counts.get(item.node_id, 0) + 1
        if item.status == "running":
            running[item.node_id] = running.get(item.node_id, 0) + 1
    result = []
    for node in nodes:
        node_containers = [item for item in containers if item.node_id == node.id]
        host_metrics_visible = can_view_node_metrics(user, rules, node)
        gpu_metrics_visible = host_metrics_visible or any(
            _container_has_gpu(item) for item in node_containers
        )
        gpus = _visible_node_gpus(user, rules, node, node_containers)
        result.append(
            {
                "id": node.id,
                "name": node.name,
                "hostname": node.hostname,
                "environment": node.environment,
                "online": _node_online(node, settings.node_offline_seconds),
                "last_seen_at": _iso(node.last_seen_at),
                "agent_version": node.agent_version,
                "docker_version": node.docker_version,
                "os": node.os,
                "gpu_status": node.gpu_status
                if gpu_metrics_visible
                else "restricted",
                "gpu_error": node.gpu_error if gpu_metrics_visible else None,
                "gpu_updated_at": _iso(node.gpu_updated_at)
                if gpu_metrics_visible
                else None,
                "gpus": gpus,
                "system_metrics_status": node.system_metrics_status
                if host_metrics_visible
                else "restricted",
                "system_metrics_error": node.system_metrics_error
                if host_metrics_visible
                else None,
                "system_metrics_updated_at": _iso(node.system_metrics_updated_at)
                if host_metrics_visible
                else None,
                "system_metrics": _node_system_metrics(node)
                if host_metrics_visible
                else {},
                "container_count": counts.get(node.id, 0),
                "running_count": running.get(node.id, 0),
            }
        )
    return result


@router.get("/nodes/{node_id}/containers")
def list_containers(node_id: str, db: Db, user: CurrentUser) -> list[dict]:
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    rules = load_access_rules(db, user)
    items = db.scalars(
        select(Container)
        .where(Container.node_id == node_id, Container.present.is_(True))
        .order_by(Container.compose_project, Container.name)
    ).all()
    visible = [item for item in items if can_access(user, rules, node, item, "view")]
    if not visible and not can_access(user, rules, node, permission="view"):
        raise HTTPException(status_code=404, detail="节点不存在")
    return [_container_dict(item) for item in visible]


@router.get("/containers/{container_id}")
def get_container(container_id: str, db: Db, user: CurrentUser) -> dict:
    item = db.get(Container, container_id)
    if not item or not item.present:
        raise HTTPException(status_code=404, detail="容器不存在")
    node = db.get(Node, item.node_id)
    rules = load_access_rules(db, user)
    if not node or not can_access(user, rules, node, item, "view"):
        raise HTTPException(status_code=404, detail="容器不存在")
    result = _container_dict(item)
    result["node_name"] = node.name if node else "未知节点"
    result["assigned_gpus"] = _assigned_gpus(node, [item])
    result["permissions"] = {
        "view": True,
        "logs": can_access(user, rules, node, item, "logs"),
        "operate": can_access(user, rules, node, item, "operate"),
        "manage": can_access(user, rules, node, item, "manage"),
        "update_image": can_access(user, rules, node, item, "manage"),
    }
    return result


def _container_dict(item: Container) -> dict:
    return {
        "id": item.id,
        "node_id": item.node_id,
        "docker_id": item.docker_id,
        "name": item.name,
        "image": item.image,
        "status": item.status,
        "health": item.health,
        "compose_project": item.compose_project,
        "compose_service": item.compose_service,
        "cpu_percent": round(item.cpu_percent, 2),
        "memory_usage": item.memory_usage,
        "memory_limit": item.memory_limit,
        "memory_percent": round(item.memory_percent, 2),
        "network_rx_bytes": item.network_rx_bytes,
        "network_tx_bytes": item.network_tx_bytes,
        "network_rx_bps": round(item.network_rx_bps, 2),
        "network_tx_bps": round(item.network_tx_bps, 2),
        "writable_layer_bytes": item.writable_layer_bytes,
        "rootfs_bytes": item.rootfs_bytes,
        "block_read_bytes": item.block_read_bytes,
        "block_write_bytes": item.block_write_bytes,
        "block_read_bps": round(item.block_read_bps, 2),
        "block_write_bps": round(item.block_write_bps, 2),
        "pids": item.pids,
        "restart_count": item.restart_count,
        "oom_killed": item.oom_killed,
        "exit_code": item.exit_code,
        "finished_at": item.finished_at,
        "health_failing_streak": item.health_failing_streak,
        "started_at": item.started_at,
        "ports": json.loads(item.ports_json or "{}"),
        "gpu_devices": _container_gpu_devices(item),
        "gpu_all": item.gpu_all,
        "updated_at": _iso(item.updated_at),
    }


def _history(
    db: Db, target_type: str, target_id: str, hours: int, limit: int
) -> list[dict]:
    since = datetime.now(UTC) - timedelta(hours=hours)
    samples = list(
        db.scalars(
            select(MetricSample)
            .where(
                MetricSample.target_type == target_type,
                MetricSample.target_id == target_id,
                MetricSample.sampled_at >= since,
            )
            .order_by(MetricSample.sampled_at.desc())
            .limit(limit)
        ).all()
    )
    return [
        {
            "sampled_at": _iso(item.sampled_at),
            "cpu_percent": round(item.cpu_percent, 2),
            "memory_usage": item.memory_usage,
            "memory_percent": round(item.memory_percent, 2),
            "network_rx_bps": round(item.network_rx_bps, 2),
            "network_tx_bps": round(item.network_tx_bps, 2),
            "disk_used_bytes": item.disk_used_bytes,
            "disk_total_bytes": item.disk_total_bytes,
            "block_read_bps": round(item.block_read_bps, 2),
            "block_write_bps": round(item.block_write_bps, 2),
            "pids": item.pids,
            "restart_count": item.restart_count,
        }
        for item in reversed(samples)
    ]


@router.get("/nodes/{node_id}/metrics/history")
def node_metric_history(
    node_id: str,
    db: Db,
    user: CurrentUser,
    hours: int = Query(default=24, ge=1, le=8760),
    limit: int = Query(default=1000, ge=2, le=2500),
) -> list[dict]:
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    rules = load_access_rules(db, user)
    if not can_view_node_metrics(user, rules, node):
        raise HTTPException(status_code=404, detail="节点不存在")
    return _history(db, "node", node.id, hours, limit)


@router.get("/containers/{container_id}/metrics/history")
def container_metric_history(
    container_id: str,
    db: Db,
    user: CurrentUser,
    hours: int = Query(default=24, ge=1, le=8760),
    limit: int = Query(default=1000, ge=2, le=2500),
) -> list[dict]:
    item = db.get(Container, container_id)
    node = db.get(Node, item.node_id) if item else None
    rules = load_access_rules(db, user)
    if not item or not node or not can_access(user, rules, node, item, "view"):
        raise HTTPException(status_code=404, detail="容器不存在")
    return _history(db, "container", item.id, hours, limit)


@router.post(
    "/containers/{container_id}/actions", response_model=TaskOut, status_code=202
)
def create_action(
    container_id: str,
    payload: ActionRequest,
    request: Request,
    db: Db,
    settings: Config,
    user: CurrentUser,
) -> Task:
    container = db.get(Container, container_id)
    if not container or not container.present:
        raise HTTPException(status_code=404, detail="容器不存在")
    node = db.get(Node, container.node_id)
    rules = load_access_rules(db, user)
    if not node or not can_access(user, rules, node, container, "view"):
        raise HTTPException(status_code=404, detail="容器不存在")
    arguments: dict = {}
    if payload.action == "update_image":
        if not can_access(user, rules, node, container, "manage"):
            raise HTTPException(status_code=403, detail="更新镜像需要该容器的管理权限")
        try:
            _, target_ref = validate_tag_change(
                container.image, payload.target_image or ""
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        arguments = {
            "expected_image": container.image,
            "target_image": target_ref.canonical,
        }
    else:
        permission = "logs" if payload.action == "logs" else "operate"
        if not can_access(user, rules, node, container, permission):
            raise HTTPException(status_code=403, detail="没有该容器的操作权限")
        if payload.action == "logs":
            arguments = {"tail": payload.tail}
    if not node or not _node_online(node, settings.node_offline_seconds):
        raise HTTPException(status_code=409, detail="节点离线，无法下发操作")
    task = Task(
        node_id=node.id,
        container_id=container.id,
        docker_id=container.docker_id,
        action=payload.action,
        arguments_json=json.dumps(arguments),
        requested_by=user.id,
    )
    db.add(task)
    add_audit(
        db,
        action=f"container.{payload.action}",
        target_type="container",
        target_id=container.id,
        target_name=f"{node.name}/{container.name}",
        user=user,
        detail=(
            f"task queued: {container.image} -> {arguments['target_image']}"
            if payload.action == "update_image"
            else "task queued"
        ),
        request=request,
        settings=settings,
    )
    db.commit()
    return task


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: str, db: Db, user: CurrentUser) -> Task:
    task = db.get(Task, task_id)
    if not task or (user.role != UserRole.admin and task.requested_by != user.id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.get("/audit")
def list_audit(
    db: Db,
    user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict]:
    query_limit = (
        limit
        if user.role == UserRole.admin or not user.resource_restricted
        else limit * 5
    )
    items = list(
        db.scalars(
            select(AuditLog).order_by(AuditLog.created_at.desc()).limit(query_limit)
        ).all()
    )
    if user.role != UserRole.admin and user.resource_restricted:
        rules = load_access_rules(db, user)
        nodes = {node.id: node for node in db.scalars(select(Node)).all()}
        containers = {item.id: item for item in db.scalars(select(Container)).all()}

        def visible(entry: AuditLog) -> bool:
            if entry.user_id == user.id:
                return True
            if entry.target_type == "node" and entry.target_id in nodes:
                return can_access(
                    user, rules, nodes[entry.target_id], permission="view"
                )
            if entry.target_type == "container" and entry.target_id in containers:
                container = containers[entry.target_id]
                node = nodes.get(container.node_id)
                return bool(node and can_access(user, rules, node, container, "view"))
            return False

        items = [item for item in items if visible(item)][:limit]
    return [
        {
            "id": item.id,
            "username": item.username,
            "action": item.action,
            "target_type": item.target_type,
            "target_name": item.target_name,
            "success": item.success,
            "detail": item.detail,
            "ip_address": item.ip_address,
            "created_at": _iso(item.created_at),
        }
        for item in items
    ]


def _alert_rule_out(rule: AlertRule) -> dict:
    return {
        "id": rule.id,
        "name": rule.name,
        "scope_type": rule.scope_type,
        "environment": rule.environment,
        "node_id": rule.node_id,
        "container_id": rule.container_id,
        "metric": rule.metric,
        "operator": rule.operator,
        "threshold": rule.threshold,
        "consecutive_required": rule.consecutive_required,
        "severity": rule.severity,
        "enabled": rule.enabled,
        "notify": rule.notify,
        "updated_at": _iso(rule.updated_at),
    }


def _validate_alert_scope(db: Db, payload: AlertRuleInput) -> None:
    if payload.metric.startswith("node_") and payload.scope_type == "container":
        raise HTTPException(status_code=422, detail="节点指标不能使用容器范围")
    if payload.scope_type == "environment":
        environments = {item for item in db.scalars(select(Node.environment)).all()}
        if not payload.environment or payload.environment not in environments:
            raise HTTPException(status_code=422, detail="告警环境不存在")
    if payload.scope_type == "node" and (
        not payload.node_id or not db.get(Node, payload.node_id)
    ):
        raise HTTPException(status_code=422, detail="告警节点不存在")
    if payload.scope_type == "container":
        container = db.get(Container, payload.container_id or "")
        if not container:
            raise HTTPException(status_code=422, detail="告警容器不存在")


def _alert_rule_values(payload: AlertRuleInput) -> dict:
    values = payload.model_dump()
    if payload.scope_type != "environment":
        values["environment"] = None
    if payload.scope_type != "node":
        values["node_id"] = None
    if payload.scope_type != "container":
        values["container_id"] = None
    return values


@router.get("/alerts/rules")
def list_alert_rules(db: Db, admin: Admin) -> list[dict]:
    return [
        _alert_rule_out(rule)
        for rule in db.scalars(select(AlertRule).order_by(AlertRule.name)).all()
    ]


@router.post("/alerts/rules", status_code=status.HTTP_201_CREATED)
def create_alert_rule(
    payload: AlertRuleInput,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> dict:
    _validate_alert_scope(db, payload)
    if db.scalar(select(AlertRule.id).where(AlertRule.name == payload.name)):
        raise HTTPException(status_code=409, detail="告警规则名称已存在")
    rule = AlertRule(**_alert_rule_values(payload))
    db.add(rule)
    db.flush()
    add_audit(
        db,
        action="alert.rule.create",
        target_type="alert_rule",
        target_id=rule.id,
        target_name=rule.name,
        user=admin,
        detail=f"metric={rule.metric}, threshold={rule.threshold}",
        request=request,
        settings=settings,
    )
    db.commit()
    return _alert_rule_out(rule)


@router.put("/alerts/rules/{rule_id}")
def update_alert_rule(
    rule_id: str,
    payload: AlertRuleInput,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> dict:
    rule = db.get(AlertRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="告警规则不存在")
    _validate_alert_scope(db, payload)
    duplicate = db.scalar(
        select(AlertRule.id).where(
            AlertRule.name == payload.name, AlertRule.id != rule.id
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="告警规则名称已存在")
    for field, value in _alert_rule_values(payload).items():
        setattr(rule, field, value)
    rule.updated_at = datetime.now(UTC)
    add_audit(
        db,
        action="alert.rule.update",
        target_type="alert_rule",
        target_id=rule.id,
        target_name=rule.name,
        user=admin,
        detail=f"metric={rule.metric}, threshold={rule.threshold}, enabled={rule.enabled}",
        request=request,
        settings=settings,
    )
    # A changed threshold/scope must not inherit an old consecutive count or
    # active state from the previous rule definition.
    db.execute(delete(AlertState).where(AlertState.rule_id == rule.id))
    db.commit()
    return _alert_rule_out(rule)


@router.delete("/alerts/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_alert_rule(
    rule_id: str,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> None:
    rule = db.get(AlertRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="告警规则不存在")
    add_audit(
        db,
        action="alert.rule.delete",
        target_type="alert_rule",
        target_id=rule.id,
        target_name=rule.name,
        user=admin,
        request=request,
        settings=settings,
    )
    db.execute(delete(AlertEvent).where(AlertEvent.rule_id == rule.id))
    db.execute(delete(AlertState).where(AlertState.rule_id == rule.id))
    db.delete(rule)
    db.commit()


def _can_access_alert(
    db: Db, user: User, event: AlertEvent, permission: str = "view"
) -> bool:
    if user.role == UserRole.admin:
        return True
    if permission == "operate" and user.role != UserRole.operator:
        return False
    if not user.resource_restricted:
        return True
    node = db.get(Node, event.node_id)
    if not node:
        return False
    container = (
        db.get(Container, event.target_id)
        if event.target_type == "container"
        else None
    )
    return can_access(user, load_access_rules(db, user), node, container, permission)


@router.get("/alerts/events")
def list_alert_events(
    db: Db,
    user: CurrentUser,
    limit: int = Query(default=100, ge=1, le=500),
    active_only: bool = False,
) -> list[dict]:
    events = list(
        db.scalars(
            select(AlertEvent).order_by(AlertEvent.created_at.desc()).limit(limit * 5)
        ).all()
    )
    active_keys = {
        (state.rule_id, state.target_type, state.target_id)
        for state in db.scalars(
            select(AlertState).where(AlertState.active.is_(True))
        ).all()
    }
    result = []
    for event in events:
        active = (event.rule_id, event.target_type, event.target_id) in active_keys
        if (active_only and not active) or not _can_access_alert(db, user, event):
            continue
        result.append(
            {
                "id": event.id,
                "rule_id": event.rule_id,
                "rule_name": event.rule_name,
                "target_type": event.target_type,
                "target_id": event.target_id,
                "target_name": event.target_name,
                "status": event.status,
                "active": active,
                "severity": event.severity,
                "metric": event.metric,
                "value": event.value,
                "threshold": event.threshold,
                "message": event.message,
                "notified": event.notified,
                "notification_error": event.notification_error,
                "acknowledged": event.acknowledged_at is not None,
                "can_acknowledge": _can_access_alert(db, user, event, "operate"),
                "created_at": _iso(event.created_at),
            }
        )
        if len(result) >= limit:
            break
    return result


@router.post(
    "/alerts/events/{event_id}/acknowledge",
    status_code=status.HTTP_204_NO_CONTENT,
)
def acknowledge_alert_event(
    event_id: str,
    request: Request,
    db: Db,
    settings: Config,
    user: CurrentUser,
) -> None:
    event = db.get(AlertEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="告警事件不存在")
    if not _can_access_alert(db, user, event, "operate"):
        raise HTTPException(status_code=403, detail="需要该资源的运维权限")
    event.acknowledged_by = user.id
    event.acknowledged_at = datetime.now(UTC)
    add_audit(
        db,
        action="alert.acknowledge",
        target_type=event.target_type,
        target_id=event.target_id,
        target_name=event.target_name,
        user=user,
        detail=f"rule={event.rule_name}",
        request=request,
        settings=settings,
    )
    db.commit()


@router.get("/users")
def list_users(db: Db, manager: CurrentUser) -> list[dict]:
    if not can_manage_resources(db, manager):
        raise HTTPException(status_code=403, detail="需要全局或资源管理权限")
    global_admin = manager.role == UserRole.admin
    manage_counts: dict[str, int] = {}
    for rule in db.scalars(
        select(AccessRule).where(AccessRule.can_manage.is_(True))
    ).all():
        manage_counts[rule.user_id] = manage_counts.get(rule.user_id, 0) + 1
    return [
        {
            "id": user.id,
            "username": user.username,
            "wecom_userid": user.wecom_userid,
            "display_name": user.display_name,
            "role": user.role,
            "is_active": user.is_active,
            "resource_restricted": user.resource_restricted,
            "access_version": user.access_version,
            "resource_admin_count": manage_counts.get(user.id, 0),
            "can_edit_account": global_admin and user.id != manager.id,
            "can_edit_access": (
                user.role != UserRole.admin
                and user.id != manager.id
                and (global_admin or user.resource_restricted)
            ),
            "created_at": _iso(user.created_at),
        }
        for user in db.scalars(select(User).order_by(User.username)).all()
    ]


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate, request: Request, db: Db, settings: Config, admin: Admin
) -> dict:
    wecom_userid = payload.wecom_userid.strip()
    if db.scalar(select(User).where(User.wecom_userid == wecom_userid)):
        raise HTTPException(status_code=409, detail="企业微信 UserId 已绑定")
    user = User(
        username=wecom_userid,
        wecom_userid=wecom_userid,
        display_name=payload.display_name,
        role=payload.role,
        resource_restricted=payload.role != UserRole.admin,
    )
    db.add(user)
    db.flush()
    add_audit(
        db,
        action="user.create",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        user=admin,
        detail=f"role={user.role.value}, wecom_userid={user.wecom_userid}",
        request=request,
        settings=settings,
    )
    db.commit()
    return {
        "id": user.id,
        "username": user.username,
        "wecom_userid": user.wecom_userid,
        "role": user.role,
    }


@router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    payload: UserUpdate,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == admin.id and payload.is_active is False:
        raise HTTPException(status_code=409, detail="不能停用当前登录账号")
    if user.id == admin.id and payload.role not in (None, UserRole.admin):
        raise HTTPException(status_code=409, detail="不能降低当前登录账号的角色")
    if user.id == admin.id and payload.wecom_userid is not None:
        raise HTTPException(status_code=409, detail="不能修改当前登录账号的企微绑定")
    removes_admin = user.role == UserRole.admin and (
        payload.role not in (None, UserRole.admin) or payload.is_active is False
    )
    if removes_admin:
        remaining_admins = db.scalar(
            select(User.id)
            .where(
                User.role == UserRole.admin,
                User.is_active.is_(True),
                User.id != user.id,
            )
            .limit(1)
        )
        if not remaining_admins:
            raise HTTPException(status_code=409, detail="至少需要保留一个启用的管理员")
    changes = payload.model_dump(exclude_unset=True)
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.role is not None:
        user.role = payload.role
        if payload.role == UserRole.admin:
            user.resource_restricted = False
            db.execute(delete(AccessRule).where(AccessRule.user_id == user.id))
        elif payload.role == UserRole.viewer:
            db.execute(
                update(AccessRule)
                .where(AccessRule.user_id == user.id)
                .values(can_manage=False)
            )
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.wecom_userid is not None:
        new_userid = payload.wecom_userid.strip()
        existing = db.scalar(
            select(User).where(
                User.wecom_userid == new_userid,
                User.id != user.id,
            )
        )
        if existing:
            raise HTTPException(status_code=409, detail="企业微信 UserId 已绑定")
        user.wecom_userid = new_userid
        user.username = new_userid
    security_changed = any(
        field in changes for field in ("wecom_userid", "role", "is_active")
    )
    if security_changed:
        now = datetime.now(UTC)
        sessions = db.scalars(
            select(WebSession).where(
                WebSession.user_id == user.id, WebSession.revoked_at.is_(None)
            )
        ).all()
        for web_session in sessions:
            web_session.revoked_at = now
            web_session.revoke_reason = "account changed"
    add_audit(
        db,
        action="user.update",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        user=admin,
        detail=json.dumps(changes, ensure_ascii=False, default=str),
        request=request,
        settings=settings,
    )
    db.commit()
    return {
        "id": user.id,
        "username": user.username,
        "wecom_userid": user.wecom_userid,
        "role": user.role,
        "is_active": user.is_active,
    }


@router.post("/users/{user_id}/sessions/revoke", status_code=status.HTTP_204_NO_CONTENT)
def revoke_user_sessions(
    user_id: str,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> None:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == admin.id:
        raise HTTPException(status_code=409, detail="请使用退出登录结束当前会话")
    now = datetime.now(UTC)
    sessions = db.scalars(
        select(WebSession).where(
            WebSession.user_id == user.id, WebSession.revoked_at.is_(None)
        )
    ).all()
    for web_session in sessions:
        web_session.revoked_at = now
        web_session.revoke_reason = "admin revoked"
    add_audit(
        db,
        action="user.sessions.revoke",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        user=admin,
        detail=f"sessions={len(sessions)}",
        request=request,
        settings=settings,
    )
    db.commit()


def _rule_identity(rule: AccessRuleInput) -> tuple:
    return (
        rule.scope_type,
        rule.environment,
        rule.node_id,
        rule.project,
        rule.container_id,
    )


def _validate_rule(
    payload: AccessRuleInput,
    nodes: dict[str, Node],
    containers: dict[str, Container],
) -> dict:
    elevated = payload.can_manage
    values = {
        "scope_type": payload.scope_type,
        "environment": None,
        "node_id": None,
        "project": None,
        "container_id": None,
        "can_view": (
            payload.can_view or payload.can_logs or payload.can_operate or elevated
        ),
        "can_logs": payload.can_logs or payload.can_operate or elevated,
        "can_operate": payload.can_operate or elevated,
        "can_manage": elevated,
    }
    if payload.scope_type == "all":
        return values
    if payload.scope_type == "environment":
        environments = {node.environment for node in nodes.values()}
        if not payload.environment or payload.environment not in environments:
            raise HTTPException(status_code=422, detail="授权环境不存在")
        values["environment"] = payload.environment
        return values
    if not payload.node_id or payload.node_id not in nodes:
        raise HTTPException(status_code=422, detail="授权节点不存在")
    values["node_id"] = payload.node_id
    if payload.scope_type == "node":
        return values
    if payload.scope_type == "project":
        exists = any(
            item.present
            and item.node_id == payload.node_id
            and item.compose_project == payload.project
            for item in containers.values()
        )
        if not payload.project or not exists:
            raise HTTPException(status_code=422, detail="Compose 项目不存在")
        values["project"] = payload.project
        return values
    container = containers.get(payload.container_id or "")
    if not container or not container.present or container.node_id != payload.node_id:
        raise HTTPException(status_code=422, detail="授权容器不存在")
    values["container_id"] = container.id
    return values


def _scope_value(rule: AccessRule | dict, field: str):
    return rule.get(field) if isinstance(rule, dict) else getattr(rule, field)


def _scope_key(rule: AccessRule | dict) -> str:
    return "|".join(
        str(_scope_value(rule, field) or "")
        for field in (
            "scope_type",
            "environment",
            "node_id",
            "project",
            "container_id",
        )
    )


def _scope_node_id(
    rule: AccessRule | dict, containers: dict[str, Container]
) -> str | None:
    node_id = _scope_value(rule, "node_id")
    if node_id:
        return str(node_id)
    container_id = _scope_value(rule, "container_id")
    container = containers.get(str(container_id or ""))
    return container.node_id if container else None


def _scope_contains(
    manager_rule: AccessRule,
    candidate: AccessRule | dict,
    nodes: dict[str, Node],
    containers: dict[str, Container],
) -> bool:
    manager_type = manager_rule.scope_type
    candidate_type = str(_scope_value(candidate, "scope_type"))
    if manager_type == "all":
        return True
    if candidate_type == "all":
        return False

    candidate_node_id = _scope_node_id(candidate, containers)
    candidate_node = nodes.get(candidate_node_id or "")
    if manager_type == "environment":
        if candidate_type == "environment":
            return manager_rule.environment == _scope_value(candidate, "environment")
        return bool(
            candidate_node
            and manager_rule.environment == candidate_node.environment
        )
    if manager_type == "node":
        return manager_rule.node_id == candidate_node_id
    if manager_type == "project":
        if manager_rule.node_id != candidate_node_id:
            return False
        if candidate_type == "project":
            return manager_rule.project == _scope_value(candidate, "project")
        if candidate_type == "container":
            container = containers.get(
                str(_scope_value(candidate, "container_id") or "")
            )
            return bool(container and container.compose_project == manager_rule.project)
        return False
    if manager_type == "container":
        return (
            candidate_type == "container"
            and manager_rule.container_id == _scope_value(candidate, "container_id")
        )
    return False


def _management_rules(db: Db, manager: User) -> list[AccessRule]:
    if manager.role != UserRole.operator or not manager.resource_restricted:
        return []
    return list(
        db.scalars(
            select(AccessRule).where(
                AccessRule.user_id == manager.id,
                AccessRule.can_manage.is_(True),
            )
        ).all()
    )


def _can_manage_scope(
    manager: User,
    management_rules: list[AccessRule],
    candidate: AccessRule | dict,
    nodes: dict[str, Node],
    containers: dict[str, Container],
) -> bool:
    return manager.role == UserRole.admin or any(
        _scope_contains(rule, candidate, nodes, containers)
        for rule in management_rules
    )


def _serialized_rule(rule: AccessRule | dict) -> dict:
    return {
        "scope_type": _scope_value(rule, "scope_type"),
        "environment": _scope_value(rule, "environment"),
        "node_id": _scope_value(rule, "node_id"),
        "project": _scope_value(rule, "project"),
        "container_id": _scope_value(rule, "container_id"),
        "can_view": bool(_scope_value(rule, "can_view")),
        "can_logs": bool(_scope_value(rule, "can_logs")),
        "can_operate": bool(_scope_value(rule, "can_operate")),
        "can_manage": bool(_scope_value(rule, "can_manage")),
    }


def _rule_level(rule: AccessRule | dict) -> str:
    if _scope_value(rule, "can_manage"):
        return "manage"
    if _scope_value(rule, "can_operate"):
        return "operate"
    if _scope_value(rule, "can_logs"):
        return "logs"
    return "view"


def _prepare_access_change(
    db: Db, manager: User, user: User, payload: AccessConfigInput
) -> tuple[list[dict], list[AccessRule], bool, list[AccessRule]]:
    if user.role == UserRole.admin and payload.restricted:
        raise HTTPException(status_code=409, detail="管理员始终拥有全部资源权限")
    scoped_manager = manager.role != UserRole.admin
    if scoped_manager and (
        user.role == UserRole.admin
        or user.id == manager.id
        or not user.resource_restricted
    ):
        raise HTTPException(status_code=403, detail="不能修改该账号的资源范围")
    if scoped_manager and not payload.restricted:
        raise HTTPException(status_code=403, detail="资源管理员不能授予全部资源")
    if payload.expected_version is not None and payload.expected_version != user.access_version:
        raise HTTPException(
            status_code=409,
            detail="资源权限已被其他管理员修改，请刷新后重新确认",
        )

    nodes = {node.id: node for node in db.scalars(select(Node)).all()}
    containers = {item.id: item for item in db.scalars(select(Container)).all()}
    deduplicated = {_rule_identity(rule): rule for rule in payload.rules}
    validated = (
        [_validate_rule(rule, nodes, containers) for rule in deduplicated.values()]
        if payload.restricted
        else []
    )
    if user.role != UserRole.operator and any(item["can_manage"] for item in validated):
        raise HTTPException(status_code=422, detail="只有运维角色可以设为资源管理员")
    management_rules = _management_rules(db, manager)
    if scoped_manager and any(
        not _can_manage_scope(manager, management_rules, item, nodes, containers)
        for item in validated
    ):
        raise HTTPException(status_code=403, detail="不能授予自身管理范围之外的资源")
    existing = list(
        db.scalars(select(AccessRule).where(AccessRule.user_id == user.id)).all()
    )
    editable_existing = (
        [
            rule
            for rule in existing
            if _can_manage_scope(
                manager, management_rules, rule, nodes, containers
            )
        ]
        if scoped_manager
        else existing
    )
    return validated, editable_existing, scoped_manager, management_rules


def _access_diff(existing: list[AccessRule], validated: list[dict]) -> dict:
    before = {_scope_key(rule): rule for rule in existing}
    after = {_scope_key(rule): rule for rule in validated}
    added = [
        {"scope": key, "level": _rule_level(after[key])}
        for key in sorted(after.keys() - before.keys())
    ]
    removed = [
        {"scope": key, "level": _rule_level(before[key])}
        for key in sorted(before.keys() - after.keys())
    ]
    changed = [
        {
            "scope": key,
            "from": _rule_level(before[key]),
            "to": _rule_level(after[key]),
        }
        for key in sorted(before.keys() & after.keys())
        if _rule_level(before[key]) != _rule_level(after[key])
    ]
    elevated = sum(
        item["level"] == "manage" for item in added
    ) + sum(item["to"] == "manage" and item["from"] != "manage" for item in changed)
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "management_elevations": elevated,
        },
    }


@router.get("/access/resources")
def access_resources(db: Db, manager: CurrentUser) -> dict:
    if not can_manage_resources(db, manager):
        raise HTTPException(status_code=403, detail="需要全局或资源管理权限")
    all_nodes = list(
        db.scalars(select(Node).order_by(Node.environment, Node.name)).all()
    )
    all_containers = list(
        db.scalars(
            select(Container)
            .where(Container.present.is_(True))
            .order_by(Container.compose_project, Container.name)
        ).all()
    )
    nodes_by_id = {node.id: node for node in all_nodes}
    containers_by_id = {item.id: item for item in all_containers}
    management_rules = _management_rules(db, manager)

    def manageable(candidate: dict) -> bool:
        return _can_manage_scope(
            manager,
            management_rules,
            candidate,
            nodes_by_id,
            containers_by_id,
        )

    visible_containers = [
        item
        for item in all_containers
        if manageable(
            {
                "scope_type": "container",
                "node_id": item.node_id,
                "container_id": item.id,
            }
        )
    ]
    visible_node_ids = {item.node_id for item in visible_containers}
    nodes = [
        node
        for node in all_nodes
        if node.id in visible_node_ids
        or manageable({"scope_type": "node", "node_id": node.id})
    ]
    node_ids = {node.id for node in nodes}
    containers = [item for item in visible_containers if item.node_id in node_ids]
    by_node: dict[str, list[dict]] = {node.id: [] for node in nodes}
    for item in containers:
        by_node.setdefault(item.node_id, []).append(
            {
                "id": item.id,
                "name": item.name,
                "project": item.compose_project,
                "service": item.compose_service,
                "status": item.status,
            }
        )
    editable_scope_keys: set[str] = set()
    for environment in {node.environment for node in nodes}:
        candidate = {"scope_type": "environment", "environment": environment}
        if manageable(candidate):
            editable_scope_keys.add(_scope_key(candidate))
    for node in nodes:
        candidate = {"scope_type": "node", "node_id": node.id}
        if manageable(candidate):
            editable_scope_keys.add(_scope_key(candidate))
        projects = {
            item.compose_project
            for item in containers
            if item.node_id == node.id and item.compose_project
        }
        for project in projects:
            project_scope = {
                "scope_type": "project",
                "node_id": node.id,
                "project": project,
            }
            if manageable(project_scope):
                editable_scope_keys.add(_scope_key(project_scope))
    for item in containers:
        candidate = {
            "scope_type": "container",
            "node_id": item.node_id,
            "container_id": item.id,
        }
        if manageable(candidate):
            editable_scope_keys.add(_scope_key(candidate))
    return {
        "environments": sorted({node.environment for node in nodes}),
        "partial": manager.role != UserRole.admin,
        "allow_unrestricted": manager.role == UserRole.admin,
        "editable_scope_keys": sorted(editable_scope_keys),
        "nodes": [
            {
                "id": node.id,
                "name": node.name,
                "hostname": node.hostname,
                "environment": node.environment,
                "containers": by_node.get(node.id, []),
            }
            for node in nodes
        ],
    }


@router.get("/users/{user_id}/access")
def get_user_access(user_id: str, db: Db, manager: CurrentUser) -> dict:
    if not can_manage_resources(db, manager):
        raise HTTPException(status_code=403, detail="需要全局或资源管理权限")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    scoped_manager = manager.role != UserRole.admin
    if scoped_manager and (
        user.role == UserRole.admin
        or user.id == manager.id
        or not user.resource_restricted
    ):
        raise HTTPException(status_code=403, detail="不能修改该账号的资源范围")
    all_rules = list(
        db.scalars(
            select(AccessRule)
            .where(AccessRule.user_id == user.id)
            .order_by(AccessRule.scope_type, AccessRule.created_at)
        ).all()
    )
    if scoped_manager:
        nodes = {node.id: node for node in db.scalars(select(Node)).all()}
        containers = {item.id: item for item in db.scalars(select(Container)).all()}
        management_rules = _management_rules(db, manager)
        rules = [
            rule
            for rule in all_rules
            if _can_manage_scope(
                manager, management_rules, rule, nodes, containers
            )
        ]
    else:
        rules = all_rules
    return {
        "user_id": user.id,
        "version": user.access_version,
        "restricted": user.resource_restricted,
        "admin_bypass": user.role == UserRole.admin,
        "partial": scoped_manager,
        "allow_unrestricted": not scoped_manager,
        "rules": [_serialized_rule(rule) for rule in rules],
    }


@router.post("/users/{user_id}/access/preview")
def preview_user_access(
    user_id: str,
    payload: AccessConfigInput,
    db: Db,
    manager: CurrentUser,
) -> dict:
    if not can_manage_resources(db, manager):
        raise HTTPException(status_code=403, detail="需要全局或资源管理权限")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    validated, existing, scoped_manager, _ = _prepare_access_change(
        db, manager, user, payload
    )
    return {
        "user_id": user.id,
        "version": user.access_version,
        "partial": scoped_manager,
        "restricted_changed": (
            not scoped_manager and user.resource_restricted != payload.restricted
        ),
        **_access_diff(existing, validated),
    }


@router.get("/users/{user_id}/access/effective")
def effective_user_access(user_id: str, db: Db, manager: CurrentUser) -> dict:
    if not can_manage_resources(db, manager):
        raise HTTPException(status_code=403, detail="需要全局或资源管理权限")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    nodes = {item.id: item for item in db.scalars(select(Node)).all()}
    containers = {
        item.id: item
        for item in db.scalars(
            select(Container).where(Container.present.is_(True))
        ).all()
    }
    target_rules = list(
        db.scalars(select(AccessRule).where(AccessRule.user_id == user.id)).all()
    )
    management_rules = _management_rules(db, manager)

    def manager_can_see(candidate: dict) -> bool:
        return _can_manage_scope(
            manager, management_rules, candidate, nodes, containers
        )

    resources = []
    for node in nodes.values():
        node_candidate = {"scope_type": "node", "node_id": node.id}
        if manager_can_see(node_candidate):
            resources.append(
                {
                    "scope_type": "node",
                    "resource_id": node.id,
                    "node_id": node.id,
                    "name": node.name,
                    "permissions": {
                        permission: can_access(
                            user, target_rules, node, permission=permission
                        )
                        for permission in ("view", "logs", "operate", "manage")
                    },
                    "sources": [
                        _scope_key(rule)
                        for rule in target_rules
                        if _scope_contains(rule, node_candidate, nodes, containers)
                    ]
                    or (["global-role"] if not user.resource_restricted else []),
                }
            )
        for container in (
            item for item in containers.values() if item.node_id == node.id
        ):
            candidate = {
                "scope_type": "container",
                "node_id": node.id,
                "container_id": container.id,
            }
            if not manager_can_see(candidate):
                continue
            resources.append(
                {
                    "scope_type": "container",
                    "resource_id": container.id,
                    "node_id": node.id,
                    "name": container.name,
                    "permissions": {
                        permission: can_access(
                            user, target_rules, node, container, permission
                        )
                        for permission in ("view", "logs", "operate", "manage")
                    },
                    "sources": [
                        _scope_key(rule)
                        for rule in target_rules
                        if _scope_contains(rule, candidate, nodes, containers)
                    ]
                    or (["global-role"] if not user.resource_restricted else []),
                }
            )
    return {
        "user_id": user.id,
        "version": user.access_version,
        "admin_bypass": user.role == UserRole.admin,
        "resources": resources,
    }


@router.put("/users/{user_id}/access")
def update_user_access(
    user_id: str,
    payload: AccessConfigInput,
    request: Request,
    db: Db,
    settings: Config,
    manager: CurrentUser,
) -> dict:
    if not can_manage_resources(db, manager):
        raise HTTPException(status_code=403, detail="需要全局或资源管理权限")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    validated, existing, scoped_manager, management_rules = _prepare_access_change(
        db, manager, user, payload
    )
    if payload.expected_version is not None:
        version_update = db.execute(
            update(User)
            .where(
                User.id == user.id,
                User.access_version == payload.expected_version,
            )
            .values(access_version=User.access_version + 1)
            .execution_options(synchronize_session=False)
        )
        if version_update.rowcount != 1:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="资源权限已被其他管理员修改，请刷新后重新确认",
            )
        db.refresh(user)
    else:
        user.access_version += 1
    nodes = {node.id: node for node in db.scalars(select(Node)).all()}
    containers = {item.id: item for item in db.scalars(select(Container)).all()}

    if scoped_manager:
        removed = 0
        for rule in existing:
            if _can_manage_scope(
                manager, management_rules, rule, nodes, containers
            ):
                db.delete(rule)
                removed += 1
    else:
        db.execute(delete(AccessRule).where(AccessRule.user_id == user.id))
        user.resource_restricted = payload.restricted
        removed = -1
    if payload.restricted:
        for values in validated:
            db.add(AccessRule(user_id=user.id, **values))
    add_audit(
        db,
        action="user.access.update",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        user=manager,
        detail=(
            f"restricted={payload.restricted}, rules={len(validated)}, "
            f"partial={scoped_manager}, replaced={removed}"
        ),
        request=request,
        settings=settings,
    )
    db.commit()
    return {
        "user_id": user.id,
        "restricted": user.resource_restricted,
        "rules": len(validated),
        "partial": scoped_manager,
        "version": user.access_version,
    }


@router.delete(
    "/users/{user_id}/access/management", status_code=status.HTTP_204_NO_CONTENT
)
def revoke_user_management(
    user_id: str,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> None:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    result = db.execute(
        update(AccessRule)
        .where(AccessRule.user_id == user.id, AccessRule.can_manage.is_(True))
        .values(can_manage=False)
    )
    user.access_version += 1
    add_audit(
        db,
        action="user.access.management.revoke",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        user=admin,
        detail=f"rules={result.rowcount}",
        request=request,
        settings=settings,
    )
    db.commit()


@router.get("/access/managers")
def list_resource_managers(
    db: Db,
    manager: CurrentUser,
    node_id: str = Query(min_length=1, max_length=36),
    container_id: str | None = Query(default=None, max_length=36),
) -> dict:
    node = db.get(Node, node_id)
    container = db.get(Container, container_id) if container_id else None
    if not node or (container_id and (not container or container.node_id != node.id)):
        raise HTTPException(status_code=404, detail="资源不存在")
    candidate = {
        "scope_type": "container" if container else "node",
        "node_id": node.id,
        "container_id": container.id if container else None,
    }
    nodes = {item.id: item for item in db.scalars(select(Node)).all()}
    containers = {item.id: item for item in db.scalars(select(Container)).all()}
    if manager.role != UserRole.admin and not _can_manage_scope(
        manager, _management_rules(db, manager), candidate, nodes, containers
    ):
        raise HTTPException(status_code=403, detail="需要该资源的管理权限")
    users = {item.id: item for item in db.scalars(select(User)).all()}
    entries = [
        {
            "user_id": user.id,
            "display_name": user.display_name,
            "wecom_userid": user.wecom_userid,
            "global": True,
            "sources": ["global-admin"],
        }
        for user in users.values()
        if user.role == UserRole.admin and user.is_active
    ]
    sources_by_user: dict[str, list[str]] = {}
    for rule in db.scalars(
        select(AccessRule).where(AccessRule.can_manage.is_(True))
    ).all():
        user = users.get(rule.user_id)
        if user and user.is_active and _scope_contains(rule, candidate, nodes, containers):
            sources_by_user.setdefault(user.id, []).append(_scope_key(rule))
    entries.extend(
        {
            "user_id": user_id,
            "display_name": users[user_id].display_name,
            "wecom_userid": users[user_id].wecom_userid,
            "global": False,
            "sources": sorted(sources),
        }
        for user_id, sources in sorted(sources_by_user.items())
    )
    return {"node_id": node.id, "container_id": container_id, "managers": entries}
