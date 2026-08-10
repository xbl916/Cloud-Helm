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
    AlertRuleSeed,
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
    "node_gpu_utilization_percent": "GPU 使用率",
    "node_gpu_memory_percent": "GPU 显存使用率",
    "node_gpu_temperature_c": "GPU 温度",
    "node_gpu_missing": "GPU 掉卡或监控异常",
    "node_network_surge_percent": "节点网络流量突增",
    "container_disk_growth_mibps": "容器可写层增长速度",
    "container_exit_abnormal": "容器异常退出",
    "node_load_percent": "宿主机 1 分钟负载率",
    "node_swap_percent": "宿主机 Swap 使用率",
    "node_metrics_unavailable": "宿主机指标采集失效",
}

METRIC_UNITS = {
    "node_cpu_percent": "%",
    "node_memory_percent": "%",
    "node_disk_percent": "%",
    "node_inode_percent": "%",
    "container_cpu_percent": "%",
    "container_memory_percent": "%",
    "node_gpu_utilization_percent": "%",
    "node_gpu_memory_percent": "%",
    "node_gpu_temperature_c": "°C",
    "node_gpu_missing": " 张",
    "node_network_surge_percent": "%",
    "container_disk_growth_mibps": " MiB/s",
    "node_load_percent": "%",
    "node_swap_percent": "%",
}

NETWORK_SURGE_MIN_BPS = 10 * 1024 * 1024
NETWORK_BASELINE_FLOOR_BPS = 1024 * 1024


def seed_default_alert_rules(db: Session, settings: Settings) -> None:
    defaults = [
        ("node-offline", "节点离线", "node_offline", float(settings.node_offline_seconds), 1, "critical", True),
        ("node-memory", "节点内存过高", "node_memory_percent", 90.0, 3, "warning", True),
        ("node-disk", "节点根磁盘空间不足", "node_disk_percent", 90.0, 1, "critical", True),
        ("node-inode", "节点 inode 空间不足", "node_inode_percent", 90.0, 1, "critical", True),
        ("container-memory", "容器内存过高", "container_memory_percent", 95.0, 3, "warning", True),
        ("container-health", "容器健康检查失败", "container_unhealthy", 1.0, 1, "critical", True),
        ("container-oom", "容器发生 OOM Kill", "container_oom_killed", 1.0, 1, "critical", True),
        ("container-restarting", "容器反复重启", "container_restarting", 1.0, 2, "warning", True),
        ("gpu-utilization", "GPU 使用率过高", "node_gpu_utilization_percent", 98.0, 4, "warning", False),
        ("gpu-memory", "GPU 显存使用率过高", "node_gpu_memory_percent", 95.0, 3, "warning", False),
        ("gpu-temperature", "GPU 温度过高", "node_gpu_temperature_c", 85.0, 2, "critical", True),
        ("gpu-missing", "GPU 掉卡或监控异常", "node_gpu_missing", 1.0, 2, "critical", True),
        ("network-surge", "节点网络流量突增", "node_network_surge_percent", 300.0, 1, "warning", False),
        ("container-disk-growth", "容器可写层增长过快", "container_disk_growth_mibps", 1.0, 1, "warning", False),
        ("container-exit", "容器异常退出", "container_exit_abnormal", 1.0, 1, "critical", True),
        ("node-load", "宿主机负载过高", "node_load_percent", 150.0, 4, "warning", True),
        ("node-swap", "宿主机 Swap 使用率过高", "node_swap_percent", 80.0, 3, "warning", True),
        ("node-metrics", "宿主机指标采集失效", "node_metrics_unavailable", 1.0, 2, "critical", True),
    ]
    for key, name, metric, threshold, consecutive, severity, enabled in defaults:
        if db.get(AlertRuleSeed, key):
            continue
        if not db.scalar(select(AlertRule.id).where(AlertRule.metric == metric)):
            db.add(
                AlertRule(
                    name=name,
                    metric=metric,
                    threshold=threshold,
                    consecutive_required=consecutive,
                    severity=severity,
                    enabled=enabled,
                )
            )
        db.add(AlertRuleSeed(key=key))
    db.commit()


def update_node_alert_baselines(node: Node, payload: HeartbeatRequest) -> None:
    """Update durable baselines once per heartbeat, independent of rule count."""
    if payload.gpu_status == "ok":
        node.gpu_expected_count = max(node.gpu_expected_count or 0, len(payload.gpus))
    if payload.system_metrics_status != "ok":
        node.network_surge_percent = None
        return
    current = (
        payload.system_metrics.network_rx_bps
        + payload.system_metrics.network_tx_bps
    )
    baseline = node.network_baseline_bps or 0.0
    samples = node.network_baseline_samples or 0
    node.network_surge_percent = None
    if samples >= 4 and current >= NETWORK_SURGE_MIN_BPS:
        node.network_surge_percent = max(
            0.0,
            (current - baseline) / max(baseline, NETWORK_BASELINE_FLOOR_BPS) * 100,
        )
    node.network_baseline_bps = (
        current if samples == 0 else baseline * 0.9 + current * 0.1
    )
    node.network_baseline_samples = min(samples + 1, 1000000)


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


def _node_value(
    metric: str, payload: HeartbeatRequest, node: Node
) -> float | None:
    if metric == "node_metrics_unavailable":
        return float(payload.system_metrics_status != "ok")
    if metric == "node_gpu_missing":
        expected_count = node.gpu_expected_count or 0
        if expected_count <= 0:
            return None
        current = len(payload.gpus) if payload.gpu_status == "ok" else 0
        return float(max(0, expected_count - current))
    if metric.startswith("node_gpu_"):
        if payload.gpu_status != "ok" or not payload.gpus:
            return None
        if metric == "node_gpu_utilization_percent":
            values = [
                gpu.utilization_gpu
                for gpu in payload.gpus
                if gpu.utilization_gpu is not None
            ]
        elif metric == "node_gpu_memory_percent":
            values = [
                gpu.memory_used_mib / gpu.memory_total_mib * 100
                for gpu in payload.gpus
                if gpu.memory_used_mib is not None and gpu.memory_total_mib
            ]
        elif metric == "node_gpu_temperature_c":
            values = [
                gpu.temperature_c
                for gpu in payload.gpus
                if gpu.temperature_c is not None
            ]
        else:
            return None
        return float(max(values)) if values else None
    if metric == "node_network_surge_percent":
        return node.network_surge_percent
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
        "node_load_percent": (
            system.load_1 / system.cpu_count * 100 if system.cpu_count else None
        ),
        "node_swap_percent": (
            _percent(system.swap_used_bytes, system.swap_total_bytes)
            if system.swap_total_bytes
            else None
        ),
    }
    value = values.get(metric)
    return float(value) if value is not None else None


def _container_value(metric: str, container: Container) -> float | None:
    values = {
        "container_cpu_percent": container.cpu_percent,
        "container_memory_percent": container.memory_percent,
        "container_unhealthy": float(container.health == "unhealthy"),
        "container_oom_killed": float(container.oom_killed),
        "container_restarting": float(container.status == "restarting"),
        "container_disk_growth_mibps": container.writable_layer_growth_mibps,
        "container_exit_abnormal": float(
            container.status in {"exited", "dead"}
            and container.exit_code not in {None, 0}
        ),
    }
    value = values.get(metric)
    return float(value) if value is not None else None


def _is_breached(rule: AlertRule, value: float) -> bool:
    return value >= rule.threshold if rule.operator == "gte" else value <= rule.threshold


def _event_message(
    rule: AlertRule, target_name: str, value: float, status: AlertStatus
) -> str:
    metric = METRIC_LABELS.get(rule.metric, rule.metric)
    unit = METRIC_UNITS.get(rule.metric, "")
    state = "触发" if status == AlertStatus.triggered else "恢复"
    return (
        f"{state}：{target_name} · {metric} 当前 {value:.2f}{unit}，"
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
            value = _node_value(rule.metric, payload, node)
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


def _alert_recipient_context(
    db: Session,
) -> tuple[list[User], dict[str, list[AccessRule]]]:
    users = list(db.scalars(select(User).where(User.is_active.is_(True))).all())
    rules_by_user: dict[str, list[AccessRule]] = {}
    for rule in db.scalars(select(AccessRule)).all():
        rules_by_user.setdefault(rule.user_id, []).append(rule)
    return users, rules_by_user


def _alert_recipient_userids(
    users: list[User],
    rules_by_user: dict[str, list[AccessRule]],
    node: Node,
    container: Container | None = None,
) -> list[str]:
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


def alert_recipient_userids_for_target(
    db: Session, node: Node, container: Container | None = None
) -> list[str]:
    users, rules_by_user = _alert_recipient_context(db)
    return _alert_recipient_userids(users, rules_by_user, node, container)


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
    return alert_recipient_userids_for_target(db, node, container)


def alert_test_targets(
    db: Session, rule: AlertRule
) -> list[tuple[Node, Container | None]]:
    nodes = list(db.scalars(select(Node).order_by(Node.name)).all())
    if rule.metric.startswith("container_"):
        containers = list(
            db.scalars(
                select(Container)
                .where(Container.present.is_(True))
                .order_by(Container.name)
            ).all()
        )
        nodes_by_id = {node.id: node for node in nodes}
        matches = []
        for container in containers:
            node = nodes_by_id.get(container.node_id)
            if node and _scope_matches(rule, node, container):
                matches.append((node, container))
        return matches
    return [(node, None) for node in nodes if _scope_matches(rule, node)]


def alert_test_delivery(
    db: Session, rule: AlertRule
) -> tuple[Node, Container | None, list[str]] | None:
    targets = alert_test_targets(db, rule)
    if not targets:
        return None
    users, rules_by_user = _alert_recipient_context(db)
    deliveries = [
        (
            node,
            container,
            _alert_recipient_userids(users, rules_by_user, node, container),
        )
        for node, container in targets
    ]
    return max(deliveries, key=lambda item: len(item[2]))


async def send_alert_notification(event_id: str, settings: Settings) -> None:
    if not settings.alert_notifications_enabled:
        return
    from cloudhelm.db import SessionLocal

    with SessionLocal() as db:
        event = db.get(AlertEvent, event_id)
        if not event or event.notified:
            return
        recipients = alert_recipient_userids(db, event)
        if not recipients:
            return
        try:
            content = f"【云舵告警】\n{event.message}\n时间：{event.created_at.isoformat()}"
            await send_wecom_text(recipients, content, settings)
            event.notified = True
            event.notification_error = None
        except (httpx.HTTPError, HTTPException, RuntimeError, ValueError) as exc:
            event.notification_error = str(exc)[:500]
        db.commit()


async def send_wecom_text(
    recipients: list[str], content: str, settings: Settings
) -> None:
    """Send one application text message through the configured WeCom app."""
    from cloudhelm.routers.auth import _get_access_token

    if not recipients:
        raise ValueError("企微消息接收人不能为空")
    token = await _get_access_token(settings)
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
