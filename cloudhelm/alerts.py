import json
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from cloudhelm.access import can_receive_alert
from cloudhelm.config import Settings
from cloudhelm.models import (
    AccessRule,
    AlertEvent,
    AlertRule,
    AlertState,
    AlertStatus,
    Container,
    Node,
    User,
)
from cloudhelm.schemas import HeartbeatRequest

METRIC_LABELS = {
    "node_offline": "节点离线秒数",
    "node_cpu_percent": "节点 CPU 使用率",
    "node_memory_percent": "节点内存使用率",
    "node_disk_percent": "节点根磁盘使用率",
    "node_inode_percent": "节点 inode 使用率",
    "container_cpu_percent": "容器 CPU 使用率",
    "container_memory_percent": "容器内存使用率",
    "container_unhealthy": "容器健康检查异常",
    "container_oom_killed": "容器 OOM Kill",
    "container_restarting": "容器反复重启",
}


def seed_default_alert_rules(db: Session, settings: Settings) -> None:
    if db.scalar(select(AlertRule.id).limit(1)) is not None:
        return
    defaults = [
        ("节点离线", "node_offline", float(settings.node_offline_seconds), 1, "critical"),
        ("节点内存过高", "node_memory_percent", 90.0, 3, "warning"),
        ("节点根磁盘空间不足", "node_disk_percent", 90.0, 1, "critical"),
        ("节点 inode 空间不足", "node_inode_percent", 90.0, 1, "critical"),
        ("容器内存过高", "container_memory_percent", 95.0, 3, "warning"),
        ("容器健康检查失败", "container_unhealthy", 1.0, 1, "critical"),
        ("容器发生 OOM Kill", "container_oom_killed", 1.0, 1, "critical"),
        ("容器反复重启", "container_restarting", 1.0, 2, "warning"),
    ]
    for name, metric, threshold, consecutive, severity in defaults:
        db.add(
            AlertRule(
                name=name,
                metric=metric,
                threshold=threshold,
                consecutive_required=consecutive,
                severity=severity,
            )
        )
    db.commit()


def _scope_matches(
    rule: AlertRule, node: Node, container: Container | None = None
) -> bool:
    if rule.scope_type == "all":
        return True
    if rule.scope_type == "environment":
        return rule.environment == node.environment
    if rule.scope_type == "node":
        return rule.node_id == node.id
    return container is not None and rule.container_id == container.id


def _percent(used: float, total: float) -> float:
    return float(used) / float(total) * 100 if total else 0.0


def _node_value(metric: str, payload: HeartbeatRequest) -> float | None:
    if payload.system_metrics_status != "ok":
        return None
    system = payload.system_metrics
    values = {
        "node_cpu_percent": system.cpu_percent,
        "node_memory_percent": system.memory_percent,
        "node_disk_percent": _percent(system.disk_used_bytes, system.disk_total_bytes),
        "node_inode_percent": _percent(
            system.disk_inodes_used, system.disk_inodes_total
        ),
    }
    return float(values[metric]) if metric in values else None


def _container_value(metric: str, container: Container) -> float | None:
    values = {
        "container_cpu_percent": container.cpu_percent,
        "container_memory_percent": container.memory_percent,
        "container_unhealthy": float(container.health == "unhealthy"),
        "container_oom_killed": float(container.oom_killed),
        "container_restarting": float(container.status == "restarting"),
    }
    return float(values[metric]) if metric in values else None


def _is_breached(rule: AlertRule, value: float) -> bool:
    return value >= rule.threshold if rule.operator == "gte" else value <= rule.threshold


def _event_message(
    rule: AlertRule, target_name: str, value: float, status: AlertStatus
) -> str:
    metric = METRIC_LABELS.get(rule.metric, rule.metric)
    state = "触发" if status == AlertStatus.triggered else "恢复"
    return (
        f"{state}：{target_name} · {metric} 当前 {value:.2f}，"
        f"阈值 {rule.operator} {rule.threshold:g}"
    )


def _evaluate(
    db: Session,
    rule: AlertRule,
    node: Node,
    target_type: str,
    target_id: str,
    target_name: str,
    value: float,
    now: datetime,
) -> str | None:
    state = db.scalar(
        select(AlertState).where(
            AlertState.rule_id == rule.id,
            AlertState.target_type == target_type,
            AlertState.target_id == target_id,
        )
    )
    if not state:
        state = AlertState(
            rule_id=rule.id,
            target_type=target_type,
            target_id=target_id,
            node_id=node.id,
            active=False,
            consecutive_count=0,
            current_value=value,
        )
        db.add(state)
    state.current_value = value
    state.last_evaluated_at = now
    breached = _is_breached(rule, value)
    if breached:
        state.consecutive_count += 1
        if state.active or state.consecutive_count < rule.consecutive_required:
            return None
        state.active = True
        state.first_triggered_at = now
        status = AlertStatus.triggered
    else:
        state.consecutive_count = 0
        if not state.active:
            return None
        state.active = False
        state.first_triggered_at = None
        status = AlertStatus.recovered
    event = AlertEvent(
        rule_id=rule.id,
        target_type=target_type,
        target_id=target_id,
        node_id=node.id,
        target_name=target_name,
        rule_name=rule.name,
        status=status,
        severity=rule.severity,
        metric=rule.metric,
        value=value,
        threshold=rule.threshold,
        message=_event_message(rule, target_name, value, status),
    )
    db.add(event)
    db.flush()
    return event.id


def cleanup_alert_events(db: Session, settings: Settings, now: datetime) -> None:
    cutoff = now - timedelta(hours=settings.alert_event_retention_hours)
    db.execute(
        delete(AlertEvent)
        .where(AlertEvent.created_at < cutoff)
        .execution_options(synchronize_session=False)
    )
    boundary = db.scalar(
        select(AlertEvent.created_at)
        .order_by(AlertEvent.created_at.desc())
        .offset(settings.alert_event_max_rows - 1)
        .limit(1)
    )
    if boundary is not None:
        db.execute(
            delete(AlertEvent)
            .where(AlertEvent.created_at < boundary)
            .execution_options(synchronize_session=False)
        )


def evaluate_heartbeat_alerts(
    db: Session,
    node: Node,
    containers: list[Container],
    payload: HeartbeatRequest,
    now: datetime,
    settings: Settings,
) -> list[str]:
    if not settings.alerts_enabled:
        return []
    rules = list(
        db.scalars(
            select(AlertRule).where(
                AlertRule.enabled.is_(True), AlertRule.metric != "node_offline"
            )
        ).all()
    )
    notifications: list[str] = []
    created = False
    for rule in rules:
        if rule.metric.startswith("node_"):
            value = _node_value(rule.metric, payload)
            if value is not None and _scope_matches(rule, node):
                event_id = _evaluate(
                    db, rule, node, "node", node.id, node.name, value, now
                )
                if event_id:
                    created = True
                    if rule.notify:
                        notifications.append(event_id)
            continue
        for container in containers:
            value = _container_value(rule.metric, container)
            if value is None or not _scope_matches(rule, node, container):
                continue
            event_id = _evaluate(
                db,
                rule,
                node,
                "container",
                container.id,
                container.name,
                value,
                now,
            )
            if event_id:
                created = True
                if rule.notify:
                    notifications.append(event_id)
    if created:
        cleanup_alert_events(db, settings, now)
    return notifications


def evaluate_offline_alerts(
    db: Session, now: datetime, settings: Settings
) -> list[str]:
    if not settings.alerts_enabled:
        return []
    rules = list(
        db.scalars(
            select(AlertRule).where(
                AlertRule.enabled.is_(True), AlertRule.metric == "node_offline"
            )
        ).all()
    )
    notifications: list[str] = []
    created = False
    for node in db.scalars(select(Node)).all():
        last_seen = node.last_seen_at
        if last_seen and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        value = (now - last_seen).total_seconds() if last_seen else 1000000.0
        for rule in rules:
            if _scope_matches(rule, node):
                event_id = _evaluate(
                    db, rule, node, "node", node.id, node.name, value, now
                )
                if event_id:
                    created = True
                    if rule.notify:
                        notifications.append(event_id)
    if created:
        cleanup_alert_events(db, settings, now)
    return notifications


def alert_recipient_userids(db: Session, event: AlertEvent) -> list[str]:
    node = db.get(Node, event.node_id)
    if not node:
        return []
    container = (
        db.get(Container, event.target_id)
        if event.target_type == "container"
        else None
    )
    if event.target_type == "container" and not container:
        return []
    users = list(db.scalars(select(User).where(User.is_active.is_(True))).all())
    rules_by_user: dict[str, list[AccessRule]] = {}
    for rule in db.scalars(select(AccessRule)).all():
        rules_by_user.setdefault(rule.user_id, []).append(rule)
    return sorted(
        {
            user.wecom_userid
            for user in users
            if can_receive_alert(
                user,
                rules_by_user.get(user.id, []),
                node,
                container,
            )
        }
    )


async def send_alert_notification(event_id: str, settings: Settings) -> None:
    if not settings.alert_notifications_enabled:
        return
    from cloudhelm.db import SessionLocal
    from cloudhelm.routers.auth import _get_access_token

    with SessionLocal() as db:
        event = db.get(AlertEvent, event_id)
        if not event or event.notified:
            return
        recipients = alert_recipient_userids(db, event)
        if not recipients:
            return
        try:
            token = await _get_access_token(settings)
            content = f"【云舵告警】\n{event.message}\n时间：{event.created_at.isoformat()}"
            async with httpx.AsyncClient(
                timeout=settings.wecom_api_timeout_seconds
            ) as client:
                response = await client.post(
                    f"{settings.wecom_api_base}/cgi-bin/message/send",
                    params={"access_token": token},
                    json={
                        "touser": "|".join(recipients),
                        "msgtype": "text",
                        "agentid": int(settings.wecom_agent_id),
                        "text": {"content": content},
                        "safe": 0,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            if payload.get("errcode") != 0:
                raise RuntimeError(json.dumps(payload, ensure_ascii=False)[:400])
            event.notified = True
            event.notification_error = None
        except (httpx.HTTPError, HTTPException, RuntimeError, ValueError) as exc:
            event.notification_error = str(exc)[:500]
        db.commit()
