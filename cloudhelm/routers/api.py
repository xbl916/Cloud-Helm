import json
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import delete, select

from cloudhelm.access import can_access, load_access_rules, visible_inventory
from cloudhelm.audit import add_audit
from cloudhelm.dependencies import Admin, Config, CurrentUser, Db
from cloudhelm.models import (
    AccessRule,
    AuditLog,
    Container,
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


@router.get("/dashboard")
def dashboard(db: Db, settings: Config, user: CurrentUser) -> dict:
    nodes, containers, _ = visible_inventory(db, user)
    online_nodes = sum(
        _node_online(node, settings.node_offline_seconds) for node in nodes
    )
    running = sum(item.status == "running" for item in containers)
    unhealthy = sum(item.health == "unhealthy" for item in containers)
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
    }


@router.get("/nodes")
def list_nodes(db: Db, settings: Config, user: CurrentUser) -> list[dict]:
    nodes, containers, _ = visible_inventory(db, user)
    counts: dict[str, int] = {}
    running: dict[str, int] = {}
    for item in containers:
        counts[item.node_id] = counts.get(item.node_id, 0) + 1
        if item.status == "running":
            running[item.node_id] = running.get(item.node_id, 0) + 1
    return [
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
            "container_count": counts.get(node.id, 0),
            "running_count": running.get(node.id, 0),
        }
        for node in nodes
    ]


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
    result["permissions"] = {
        "view": True,
        "logs": can_access(user, rules, node, item, "logs"),
        "operate": can_access(user, rules, node, item, "operate"),
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
        "started_at": item.started_at,
        "ports": json.loads(item.ports_json or "{}"),
        "updated_at": _iso(item.updated_at),
    }


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
    permission = "logs" if payload.action == "logs" else "operate"
    if not can_access(user, rules, node, container, permission):
        raise HTTPException(status_code=403, detail="没有该容器的操作权限")
    if not node or not _node_online(node, settings.node_offline_seconds):
        raise HTTPException(status_code=409, detail="节点离线，无法下发操作")
    task = Task(
        node_id=node.id,
        container_id=container.id,
        docker_id=container.docker_id,
        action=payload.action,
        arguments_json=json.dumps(
            {"tail": payload.tail} if payload.action == "logs" else {}
        ),
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
        detail="task queued",
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


@router.get("/users")
def list_users(db: Db, _: Admin) -> list[dict]:
    return [
        {
            "id": user.id,
            "username": user.username,
            "wecom_userid": user.wecom_userid,
            "display_name": user.display_name,
            "role": user.role,
            "is_active": user.is_active,
            "resource_restricted": user.resource_restricted,
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
    values = {
        "scope_type": payload.scope_type,
        "environment": None,
        "node_id": None,
        "project": None,
        "container_id": None,
        "can_view": payload.can_view or payload.can_logs or payload.can_operate,
        "can_logs": payload.can_logs or payload.can_operate,
        "can_operate": payload.can_operate,
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


@router.get("/access/resources")
def access_resources(db: Db, _: Admin) -> dict:
    nodes = list(db.scalars(select(Node).order_by(Node.environment, Node.name)).all())
    containers = list(
        db.scalars(
            select(Container)
            .where(Container.present.is_(True))
            .order_by(Container.compose_project, Container.name)
        ).all()
    )
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
    return {
        "environments": sorted({node.environment for node in nodes}),
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
def get_user_access(user_id: str, db: Db, _: Admin) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    rules = db.scalars(
        select(AccessRule)
        .where(AccessRule.user_id == user.id)
        .order_by(AccessRule.scope_type, AccessRule.created_at)
    ).all()
    return {
        "user_id": user.id,
        "restricted": user.resource_restricted,
        "admin_bypass": user.role == UserRole.admin,
        "rules": [
            {
                "scope_type": rule.scope_type,
                "environment": rule.environment,
                "node_id": rule.node_id,
                "project": rule.project,
                "container_id": rule.container_id,
                "can_view": rule.can_view,
                "can_logs": rule.can_logs,
                "can_operate": rule.can_operate,
            }
            for rule in rules
        ],
    }


@router.put("/users/{user_id}/access")
def update_user_access(
    user_id: str,
    payload: AccessConfigInput,
    request: Request,
    db: Db,
    settings: Config,
    admin: Admin,
) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.role == UserRole.admin and payload.restricted:
        raise HTTPException(status_code=409, detail="管理员始终拥有全部资源权限")

    nodes = {node.id: node for node in db.scalars(select(Node)).all()}
    containers = {item.id: item for item in db.scalars(select(Container)).all()}
    deduplicated: dict[tuple, AccessRuleInput] = {
        _rule_identity(rule): rule for rule in payload.rules
    }
    validated = (
        [_validate_rule(rule, nodes, containers) for rule in deduplicated.values()]
        if payload.restricted
        else []
    )
    db.execute(delete(AccessRule).where(AccessRule.user_id == user.id))
    user.resource_restricted = payload.restricted
    if payload.restricted:
        for values in validated:
            db.add(AccessRule(user_id=user.id, **values))
    add_audit(
        db,
        action="user.access.update",
        target_type="user",
        target_id=user.id,
        target_name=user.username,
        user=admin,
        detail=f"restricted={payload.restricted}, rules={len(validated)}",
        request=request,
        settings=settings,
    )
    db.commit()
    return {
        "user_id": user.id,
        "restricted": user.resource_restricted,
        "rules": len(validated),
    }
