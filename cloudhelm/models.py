import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from cloudhelm.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class UserRole(str, enum.Enum):
    admin = "admin"
    operator = "operator"
    viewer = "viewer"


class TaskStatus(str, enum.Enum):
    pending = "pending"
    dispatched = "dispatched"
    success = "success"
    failed = "failed"
    expired = "expired"


class AlertStatus(str, enum.Enum):
    triggered = "triggered"
    recovered = "recovered"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    wecom_userid: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.viewer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    resource_restricted: Mapped[bool] = mapped_column(Boolean, default=True)
    alert_notifications: Mapped[bool] = mapped_column(Boolean, default=False)
    access_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WebSession(Base):
    __tablename__ = "web_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    revoke_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class OAuthState(Base):
    __tablename__ = "oauth_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    next_path: Mapped[str] = mapped_column(String(512), default="/")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    agent_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    hostname: Mapped[str] = mapped_column(String(255), default="")
    environment: Mapped[str] = mapped_column(String(40), default="default", index=True)
    agent_token_hash: Mapped[str] = mapped_column(String(512))
    agent_version: Mapped[str] = mapped_column(String(30), default="unknown")
    docker_version: Mapped[str] = mapped_column(String(80), default="unknown")
    os: Mapped[str] = mapped_column(String(120), default="unknown")
    labels_json: Mapped[str] = mapped_column(Text, default="{}")
    gpus_json: Mapped[str] = mapped_column(Text, default="[]")
    gpu_status: Mapped[str] = mapped_column(String(20), default="unavailable")
    gpu_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    gpu_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    gpu_expected_count: Mapped[int] = mapped_column(Integer, default=0)
    network_baseline_bps: Mapped[float] = mapped_column(Float, default=0.0)
    network_baseline_samples: Mapped[int] = mapped_column(Integer, default=0)
    network_surge_percent: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    system_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    system_metrics_status: Mapped[str] = mapped_column(
        String(20), default="unavailable"
    )
    system_metrics_error: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    system_metrics_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metrics_history_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Container(Base):
    __tablename__ = "containers"
    __table_args__ = (
        UniqueConstraint("node_id", "docker_id", name="uq_container_node_docker"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    docker_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(255), index=True)
    image: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(40), default="unknown", index=True)
    health: Mapped[str | None] = mapped_column(String(40), nullable=True)
    compose_project: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )
    compose_service: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cpu_percent: Mapped[float] = mapped_column(Float, default=0.0)
    memory_usage: Mapped[int] = mapped_column(Integer, default=0)
    memory_limit: Mapped[int] = mapped_column(Integer, default=0)
    memory_percent: Mapped[float] = mapped_column(Float, default=0.0)
    network_rx_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    network_tx_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    network_rx_bps: Mapped[float] = mapped_column(Float, default=0.0)
    network_tx_bps: Mapped[float] = mapped_column(Float, default=0.0)
    writable_layer_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    rootfs_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    writable_layer_growth_mibps: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    block_read_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    block_write_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    block_read_bps: Mapped[float] = mapped_column(Float, default=0.0)
    block_write_bps: Mapped[float] = mapped_column(Float, default=0.0)
    pids: Mapped[int] = mapped_column(Integer, default=0)
    restart_count: Mapped[int] = mapped_column(Integer, default=0)
    oom_killed: Mapped[bool] = mapped_column(Boolean, default=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(60), nullable=True)
    health_failing_streak: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[str | None] = mapped_column(String(60), nullable=True)
    ports_json: Mapped[str] = mapped_column(Text, default="{}")
    labels_json: Mapped[str] = mapped_column(Text, default="{}")
    gpu_devices_json: Mapped[str] = mapped_column(Text, default="[]")
    gpu_all: Mapped[bool] = mapped_column(Boolean, default=False)
    present: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    container_id: Mapped[str | None] = mapped_column(
        ForeignKey("containers.id", ondelete="SET NULL"), nullable=True
    )
    docker_id: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(30), index=True)
    arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), default=TaskStatus.pending, index=True
    )
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MetricSample(Base):
    __tablename__ = "metric_samples"
    __table_args__ = (
        Index(
            "ix_metric_samples_target_time",
            "target_type",
            "target_id",
            "sampled_at",
        ),
        Index("ix_metric_samples_sampled_at", "sampled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(String(10))
    target_id: Mapped[str] = mapped_column(String(36))
    node_id: Mapped[str] = mapped_column(String(36))
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cpu_percent: Mapped[float] = mapped_column(Float, default=0.0)
    memory_usage: Mapped[int] = mapped_column(BigInteger, default=0)
    memory_percent: Mapped[float] = mapped_column(Float, default=0.0)
    network_rx_bps: Mapped[float] = mapped_column(Float, default=0.0)
    network_tx_bps: Mapped[float] = mapped_column(Float, default=0.0)
    disk_used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    disk_total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    block_read_bps: Mapped[float] = mapped_column(Float, default=0.0)
    block_write_bps: Mapped[float] = mapped_column(Float, default=0.0)
    pids: Mapped[int] = mapped_column(Integer, default=0)
    restart_count: Mapped[int] = mapped_column(Integer, default=0)


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    scope_type: Mapped[str] = mapped_column(String(20), default="all", index=True)
    environment: Mapped[str | None] = mapped_column(String(40), nullable=True)
    node_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    container_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    metric: Mapped[str] = mapped_column(String(40), index=True)
    operator: Mapped[str] = mapped_column(String(8), default="gte")
    threshold: Mapped[float] = mapped_column(Float, default=0)
    consecutive_required: Mapped[int] = mapped_column(Integer, default=1)
    severity: Mapped[str] = mapped_column(String(12), default="warning")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    notify: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AlertRuleSeed(Base):
    __tablename__ = "alert_rule_seeds"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    seeded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class AlertState(Base):
    __tablename__ = "alert_states"
    __table_args__ = (
        UniqueConstraint(
            "rule_id", "target_type", "target_id", name="uq_alert_state_target"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rule_id: Mapped[str] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True
    )
    target_type: Mapped[str] = mapped_column(String(12), index=True)
    target_id: Mapped[str] = mapped_column(String(36), index=True)
    node_id: Mapped[str] = mapped_column(String(36), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    consecutive_count: Mapped[int] = mapped_column(Integer, default=0)
    current_value: Mapped[float] = mapped_column(Float, default=0)
    first_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class AlertEvent(Base):
    __tablename__ = "alert_events"
    __table_args__ = (
        Index("ix_alert_events_created", "created_at"),
        Index("ix_alert_events_target", "target_type", "target_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rule_id: Mapped[str] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True
    )
    target_type: Mapped[str] = mapped_column(String(12))
    target_id: Mapped[str] = mapped_column(String(36))
    node_id: Mapped[str] = mapped_column(String(36), index=True)
    target_name: Mapped[str] = mapped_column(String(255))
    rule_name: Mapped[str] = mapped_column(String(120))
    status: Mapped[AlertStatus] = mapped_column(Enum(AlertStatus), index=True)
    severity: Mapped[str] = mapped_column(String(12), default="warning")
    metric: Mapped[str] = mapped_column(String(40))
    value: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    message: Mapped[str] = mapped_column(String(500))
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    notification_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    username: Mapped[str] = mapped_column(String(128), default="system")
    action: Mapped[str] = mapped_column(String(80), index=True)
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class AccessRule(Base):
    __tablename__ = "access_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    scope_type: Mapped[str] = mapped_column(String(20), index=True)
    environment: Mapped[str | None] = mapped_column(String(40), nullable=True)
    node_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    project: Mapped[str | None] = mapped_column(String(255), nullable=True)
    container_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    can_view: Mapped[bool] = mapped_column(Boolean, default=True)
    can_logs: Mapped[bool] = mapped_column(Boolean, default=False)
    can_operate: Mapped[bool] = mapped_column(Boolean, default=False)
    can_manage: Mapped[bool] = mapped_column(Boolean, default=False)
    alert_notify: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
