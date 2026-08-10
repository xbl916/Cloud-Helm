import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from cloudhelm.alerts import (
    _node_value,
    evaluate_heartbeat_alerts,
    evaluate_offline_alerts,
    seed_default_alert_rules,
    send_alert_notification,
    update_node_alert_baselines,
)
from cloudhelm.config import get_settings
from cloudhelm.db import SessionLocal, initialize_database
from cloudhelm.models import (
    AccessRule,
    AlertEvent,
    AlertRule,
    AlertStatus,
    Container,
    Node,
    User,
    UserRole,
)
from cloudhelm.schemas import HeartbeatRequest


def test_alerts_create_only_transition_events_and_recover(tmp_path):
    target_engine = create_engine(f"sqlite:///{tmp_path / 'alerts.db'}")
    initialize_database(target_engine)
    settings = get_settings()
    now = datetime.now(UTC)
    with Session(target_engine) as db:
        seed_default_alert_rules(db, settings)
        node = Node(
            agent_key="alert-node",
            name="告警节点",
            agent_token_hash="hash",
            last_seen_at=now,
        )
        db.add(node)
        db.flush()
        container = Container(
            node_id=node.id,
            docker_id="alert-container",
            name="告警容器",
            status="running",
            health="unhealthy",
        )
        db.add(container)
        db.flush()
        high = HeartbeatRequest.model_validate(
            {
                "system_metrics_status": "ok",
                "system_metrics": {
                    "disk_total_bytes": 100,
                    "disk_used_bytes": 95,
                    "disk_inodes_total": 100,
                    "disk_inodes_used": 20,
                },
            }
        )
        first = evaluate_heartbeat_alerts(db, node, [container], high, now, settings)
        db.commit()
        assert len(first) == 2
        assert db.scalar(
            select(AlertEvent).where(AlertEvent.metric == "node_disk_percent")
        )
        assert db.scalar(
            select(AlertEvent).where(AlertEvent.metric == "container_unhealthy")
        )

        assert (
            evaluate_heartbeat_alerts(
                db, node, [container], high, now + timedelta(seconds=15), settings
            )
            == []
        )
        db.commit()
        assert len(db.scalars(select(AlertEvent)).all()) == 2

        unavailable = HeartbeatRequest.model_validate(
            {
                "system_metrics_status": "unavailable",
                "system_metrics_error": "temporary collection failure",
            }
        )
        assert (
            evaluate_heartbeat_alerts(
                db,
                node,
                [container],
                unavailable,
                now + timedelta(seconds=20),
                settings,
            )
            == []
        )
        db.commit()
        disk_events = list(
            db.scalars(
                select(AlertEvent).where(
                    AlertEvent.metric == "node_disk_percent"
                )
            ).all()
        )
        assert len(disk_events) == 1
        assert disk_events[0].status == AlertStatus.triggered

        container.health = "healthy"
        normal = HeartbeatRequest.model_validate(
            {
                "system_metrics_status": "ok",
                "system_metrics": {
                    "disk_total_bytes": 100,
                    "disk_used_bytes": 50,
                    "disk_inodes_total": 100,
                    "disk_inodes_used": 20,
                },
            }
        )
        recovered = evaluate_heartbeat_alerts(
            db, node, [container], normal, now + timedelta(seconds=30), settings
        )
        db.commit()
        assert len(recovered) == 2
        assert (
            len(
                db.scalars(
                    select(AlertEvent).where(
                        AlertEvent.status == AlertStatus.recovered
                    )
                ).all()
            )
            == 2
        )


def test_offline_alert_uses_configured_rule_threshold(tmp_path):
    target_engine = create_engine(f"sqlite:///{tmp_path / 'offline.db'}")
    initialize_database(target_engine)
    settings = get_settings()
    now = datetime.now(UTC)
    with Session(target_engine) as db:
        rule = AlertRule(
            name="测试离线",
            metric="node_offline",
            threshold=30,
            consecutive_required=1,
        )
        node = Node(
            agent_key="offline-node",
            name="离线节点",
            agent_token_hash="hash",
            last_seen_at=now - timedelta(seconds=31),
        )
        db.add_all([rule, node])
        db.commit()
        event_ids = evaluate_offline_alerts(db, now, settings)
        db.commit()
        assert len(event_ids) == 1
        event = db.get(AlertEvent, event_ids[0])
        assert event is not None
        assert event.status == AlertStatus.triggered


def test_extended_default_rules_are_seeded_once(tmp_path):
    target_engine = create_engine(f"sqlite:///{tmp_path / 'default-alerts.db'}")
    initialize_database(target_engine)
    settings = get_settings()
    with Session(target_engine) as db:
        seed_default_alert_rules(db, settings)
        rules = {rule.metric: rule for rule in db.scalars(select(AlertRule)).all()}
        assert len(rules) == 18
        assert rules["node_gpu_temperature_c"].enabled is True
        assert rules["node_gpu_utilization_percent"].enabled is False
        assert rules["node_network_surge_percent"].enabled is False
        removed = rules["container_disk_growth_mibps"]
        db.delete(removed)
        db.commit()
        seed_default_alert_rules(db, settings)
        assert db.scalar(
            select(AlertRule).where(
                AlertRule.metric == "container_disk_growth_mibps"
            )
        ) is None


def test_extended_alert_metrics_trigger_and_recover(tmp_path):
    target_engine = create_engine(f"sqlite:///{tmp_path / 'extended-alerts.db'}")
    initialize_database(target_engine)
    settings = get_settings()
    now = datetime.now(UTC)
    metrics = [
        "node_gpu_utilization_percent",
        "node_gpu_memory_percent",
        "node_gpu_temperature_c",
        "node_gpu_missing",
        "node_network_surge_percent",
        "node_load_percent",
        "node_swap_percent",
        "container_disk_growth_mibps",
        "container_exit_abnormal",
    ]
    with Session(target_engine) as db:
        rules = [
            AlertRule(
                name=f"测试-{metric}",
                metric=metric,
                threshold={
                    "node_gpu_utilization_percent": 95,
                    "node_gpu_memory_percent": 90,
                    "node_gpu_temperature_c": 85,
                    "node_gpu_missing": 1,
                    "node_network_surge_percent": 300,
                    "node_load_percent": 150,
                    "node_swap_percent": 80,
                    "container_disk_growth_mibps": 1,
                    "container_exit_abnormal": 1,
                }[metric],
            )
            for metric in metrics
        ]
        node = Node(
            agent_key="extended-alert-node",
            name="扩展告警节点",
            agent_token_hash="hash",
            gpu_expected_count=2,
            network_surge_percent=500,
        )
        db.add_all([*rules, node])
        db.flush()
        container = Container(
            node_id=node.id,
            docker_id="extended-alert-container",
            name="扩展告警容器",
            status="exited",
            exit_code=137,
            writable_layer_growth_mibps=2,
        )
        db.add(container)
        db.flush()
        high = HeartbeatRequest.model_validate(
            {
                "gpu_status": "ok",
                "gpus": [
                    {
                        "index": 0,
                        "uuid": "GPU-hot",
                        "name": "Hot GPU",
                        "utilization_gpu": 99,
                        "memory_used_mib": 950,
                        "memory_total_mib": 1000,
                        "temperature_c": 90,
                    }
                ],
                "system_metrics_status": "ok",
                "system_metrics": {
                    "cpu_count": 4,
                    "load_1": 8,
                    "swap_total_bytes": 100,
                    "swap_used_bytes": 90,
                },
            }
        )
        triggered = evaluate_heartbeat_alerts(
            db, node, [container], high, now, settings
        )
        db.commit()
        assert len(triggered) == len(metrics)

        node.network_surge_percent = 0
        container.status = "running"
        container.exit_code = 0
        container.writable_layer_growth_mibps = 0
        normal = HeartbeatRequest.model_validate(
            {
                "gpu_status": "ok",
                "gpus": [
                    {
                        "index": index,
                        "uuid": f"GPU-cool-{index}",
                        "name": "Cool GPU",
                        "utilization_gpu": 10,
                        "memory_used_mib": 100,
                        "memory_total_mib": 1000,
                        "temperature_c": 40,
                    }
                    for index in range(2)
                ],
                "system_metrics_status": "ok",
                "system_metrics": {
                    "cpu_count": 4,
                    "load_1": 1,
                    "swap_total_bytes": 100,
                    "swap_used_bytes": 10,
                },
            }
        )
        recovered = evaluate_heartbeat_alerts(
            db, node, [container], normal, now + timedelta(seconds=15), settings
        )
        db.commit()
        assert len(recovered) == len(metrics)


def test_alert_baselines_warm_up_and_track_expected_gpus():
    node = Node(
        agent_key="baseline-node",
        name="Baseline Node",
        agent_token_hash="hash",
    )

    def heartbeat(rate_mib: float) -> HeartbeatRequest:
        return HeartbeatRequest.model_validate(
            {
                "gpu_status": "ok",
                "gpus": [
                    {"index": index, "uuid": f"GPU-{index}", "name": "GPU"}
                    for index in range(2)
                ],
                "system_metrics_status": "ok",
                "system_metrics": {
                    "network_rx_bps": rate_mib * 1024 * 1024,
                },
            }
        )

    for _ in range(4):
        update_node_alert_baselines(node, heartbeat(5))
        assert node.network_surge_percent is None
    update_node_alert_baselines(node, heartbeat(30))
    assert node.gpu_expected_count == 2
    assert node.network_surge_percent == 500


def test_gpu_monitor_disabled_after_baseline_counts_as_missing():
    node = Node(
        agent_key="gpu-disabled-node",
        name="GPU Disabled Node",
        agent_token_hash="hash",
        gpu_expected_count=2,
    )
    assert _node_value(
        "node_gpu_missing", HeartbeatRequest(gpu_status="disabled"), node
    ) == 2


def test_host_metrics_unavailable_requires_two_reports(tmp_path):
    target_engine = create_engine(f"sqlite:///{tmp_path / 'metrics-down.db'}")
    initialize_database(target_engine)
    settings = get_settings()
    now = datetime.now(UTC)
    with Session(target_engine) as db:
        rule = AlertRule(
            name="测试指标采集失效",
            metric="node_metrics_unavailable",
            threshold=1,
            consecutive_required=2,
        )
        node = Node(
            agent_key="metrics-down-node",
            name="指标失效节点",
            agent_token_hash="hash",
        )
        db.add_all([rule, node])
        db.commit()
        unavailable = HeartbeatRequest(system_metrics_status="error")
        assert evaluate_heartbeat_alerts(
            db, node, [], unavailable, now, settings
        ) == []
        triggered = evaluate_heartbeat_alerts(
            db, node, [], unavailable, now + timedelta(seconds=15), settings
        )
        db.commit()
        assert len(triggered) == 1
        recovered = evaluate_heartbeat_alerts(
            db,
            node,
            [],
            HeartbeatRequest(system_metrics_status="ok"),
            now + timedelta(seconds=30),
            settings,
        )
        db.commit()
        assert len(recovered) == 1


def test_admin_can_manage_alert_rules(client, admin_headers, session_for):
    listed = client.get("/api/v1/alerts/rules", headers=admin_headers)
    assert listed.status_code == 200
    assert listed.json()
    created = client.post(
        "/api/v1/alerts/rules",
        headers=admin_headers,
        json={
            "name": "测试 CPU 告警",
            "metric": "node_cpu_percent",
            "threshold": 85,
            "consecutive_required": 2,
            "severity": "warning",
        },
    )
    assert created.status_code == 201
    payload = created.json()
    payload["threshold"] = 88
    payload.pop("id")
    payload.pop("updated_at")
    updated = client.put(
        f"/api/v1/alerts/rules/{created.json()['id']}",
        headers=admin_headers,
        json=payload,
    )
    assert updated.status_code == 200
    assert updated.json()["threshold"] == 88
    viewer = client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "wecom_userid": "alert-viewer",
            "display_name": "Alert Viewer",
            "role": "viewer",
        },
    )
    assert viewer.status_code == 201
    with SessionLocal() as db:
        viewer_user = db.get(User, viewer.json()["id"])
        assert viewer_user is not None
        viewer_user.resource_restricted = False
        db.commit()
    viewer_headers = session_for("alert-viewer")
    assert client.get(
        "/api/v1/alerts/rules", headers=viewer_headers
    ).status_code == 403
    with SessionLocal() as db:
        db.add(
            AlertEvent(
                rule_id=created.json()["id"],
                target_type="node",
                target_id="viewer-alert-node",
                node_id="viewer-alert-node",
                target_name="Viewer Alert Node",
                rule_name="测试 CPU 告警",
                status=AlertStatus.triggered,
                severity="warning",
                metric="node_cpu_percent",
                value=99,
                threshold=88,
                message="测试只读确认权限",
            )
        )
        db.commit()
    events = client.get("/api/v1/alerts/events", headers=viewer_headers)
    assert events.status_code == 200
    event = next(item for item in events.json() if item["target_id"] == "viewer-alert-node")
    assert event["can_acknowledge"] is False
    assert client.post(
        f"/api/v1/alerts/events/{event['id']}/acknowledge",
        headers=viewer_headers,
    ).status_code == 403


def test_wecom_alert_notification_records_delivery(monkeypatch):
    settings = get_settings().model_copy(
        update={"alert_notifications_enabled": True}
    )
    with SessionLocal() as db:
        rule = db.scalar(select(AlertRule).where(AlertRule.name == "节点离线"))
        assert rule is not None
        admin = db.scalar(select(User).where(User.wecom_userid == "admin-wecom-id"))
        assert admin is not None
        admin.alert_notifications = True
        node = Node(
            agent_key="notification-node-agent",
            name="通知节点",
            environment="zz-notification-test",
            agent_token_hash="hash",
        )
        subscriber = User(
            username="operator-wecom-id",
            wecom_userid="operator-wecom-id",
            display_name="通知订阅人",
            role=UserRole.viewer,
            resource_restricted=True,
        )
        unsubscribed = User(
            username="unsubscribed-wecom-id",
            wecom_userid="unsubscribed-wecom-id",
            display_name="未订阅用户",
            role=UserRole.viewer,
            resource_restricted=True,
        )
        inactive = User(
            username="inactive-wecom-id",
            wecom_userid="inactive-wecom-id",
            display_name="停用订阅人",
            role=UserRole.viewer,
            resource_restricted=True,
            is_active=False,
        )
        db.add_all([node, subscriber, unsubscribed, inactive])
        db.flush()
        db.add_all(
            [
                AccessRule(
                    user_id=subscriber.id,
                    scope_type="node",
                    node_id=node.id,
                    can_view=True,
                    alert_notify=True,
                ),
                AccessRule(
                    user_id=unsubscribed.id,
                    scope_type="node",
                    node_id=node.id,
                    can_view=True,
                ),
                AccessRule(
                    user_id=inactive.id,
                    scope_type="node",
                    node_id=node.id,
                    can_view=True,
                    alert_notify=True,
                ),
            ]
        )
        event = AlertEvent(
            rule_id=rule.id,
            target_type="node",
            target_id=node.id,
            node_id=node.id,
            target_name="通知节点",
            rule_name=rule.name,
            status=AlertStatus.triggered,
            severity="critical",
            metric=rule.metric,
            value=120,
            threshold=rule.threshold,
            message="触发：通知节点离线",
        )
        db.add(event)
        db.commit()
        event_id = event.id

    sent = {}

    async def fake_access_token(_settings):
        return "test-access-token"

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"errcode": 0, "errmsg": "ok"}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, params, json):
            sent.update(url=url, params=params, json=json)
            return FakeResponse()

    monkeypatch.setattr("cloudhelm.routers.auth._get_access_token", fake_access_token)
    monkeypatch.setattr("cloudhelm.alerts.httpx.AsyncClient", FakeClient)
    asyncio.run(send_alert_notification(event_id, settings))

    assert sent["params"] == {"access_token": "test-access-token"}
    assert sent["json"]["touser"] == "admin-wecom-id|operator-wecom-id"
    with SessionLocal() as db:
        delivered = db.get(AlertEvent, event_id)
        assert delivered is not None
        assert delivered.notified is True
        assert delivered.notification_error is None
        admin = db.scalar(select(User).where(User.wecom_userid == "admin-wecom-id"))
        assert admin is not None
        admin.alert_notifications = False
        subscriber_ids = list(
            db.scalars(
                select(User.id).where(
                    User.wecom_userid.in_(
                        [
                            "operator-wecom-id",
                            "unsubscribed-wecom-id",
                            "inactive-wecom-id",
                        ]
                    )
                )
            ).all()
        )
        db.execute(delete(AccessRule).where(AccessRule.user_id.in_(subscriber_ids)))
        db.execute(delete(User).where(User.id.in_(subscriber_ids)))
        db.delete(delivered)
        notification_node = db.scalar(
            select(Node).where(Node.agent_key == "notification-node-agent")
        )
        assert notification_node is not None
        db.delete(notification_node)
        db.commit()
