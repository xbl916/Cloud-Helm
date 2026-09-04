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
    auth_method: Mapped[str] = mapped_column(String(30), default="wecom_h5", index=True)
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
    provider: Mapped[str] = mapped_column(String(30), default="wecom_h5")
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
    gateway_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
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
    network_surge_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    system_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    system_metrics_status: Mapped[str] = mapped_column(
        String(20), default="unavailable"
    )
    system_metrics_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
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
    active_businesses: Mapped[int] = mapped_column(Integer, default=0)
    business_capacity: Mapped[int] = mapped_column(Integer, default=0)
    business_instances: Mapped[int] = mapped_column(Integer, default=0)


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
    seeded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    service_id: Mapped[str | None] = mapped_column(
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


class ScalingService(Base):
    """A logical AI workload whose replicas report their current business load."""

    __tablename__ = "scaling_services"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    service_type: Mapped[str] = mapped_column(String(20), default="other", index=True)
    report_token_hash: Mapped[str] = mapped_column(String(512))
    capacity_per_replica: Mapped[int] = mapped_column(Integer, default=1)
    autoscaling_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True
    )
    min_replicas: Mapped[int] = mapped_column(Integer, default=1)
    max_replicas: Mapped[int] = mapped_column(Integer, default=1)
    scale_up_threshold_percent: Mapped[float] = mapped_column(Float, default=70.0)
    scale_down_threshold_percent: Mapped[float] = mapped_column(Float, default=40.0)
    window_seconds: Mapped[int] = mapped_column(Integer, default=300)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=300)
    report_stale_seconds: Mapped[int] = mapped_column(Integer, default=60)
    scale_up_step: Mapped[int] = mapped_column(Integer, default=1)
    failover_replicas: Mapped[int] = mapped_column(Integer, default=1)
    provider: Mapped[str] = mapped_column(String(30), default="acs_kubernetes")
    namespace: Mapped[str] = mapped_column(String(253), default="default")
    workload_kind: Mapped[str] = mapped_column(String(20), default="Deployment")
    workload_name: Mapped[str] = mapped_column(String(253), default="")
    acs_container_name: Mapped[str] = mapped_column(String(253), default="")
    desired_image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    previous_image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    release_status: Mapped[str] = mapped_column(String(30), default="idle", index=True)
    release_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    release_requested_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    release_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    active_release_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    release_acs_original_replicas: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    release_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    release_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_replicas: Mapped[int] = mapped_column(Integer, default=0)
    desired_replicas: Mapped[int] = mapped_column(Integer, default=0)
    current_businesses: Mapped[int] = mapped_column(Integer, default=0)
    average_businesses: Mapped[float] = mapped_column(Float, default=0.0)
    utilization_percent: Mapped[float] = mapped_column(Float, default=0.0)
    reporting_instances: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="collecting")
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_report_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_scaled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ImageRelease(Base):
    """A durable image release or rollback requested for one logical service."""

    __tablename__ = "image_releases"
    __table_args__ = (
        Index("ix_image_releases_service_created", "service_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    service_id: Mapped[str] = mapped_column(
        ForeignKey("scaling_services.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20), default="release", index=True)
    source_image: Mapped[str] = mapped_column(String(512))
    target_image: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    stage: Mapped[str] = mapped_column(String(40), default="preflight")
    requested_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rollback_of_release_id: Mapped[str | None] = mapped_column(
        ForeignKey("image_releases.id", ondelete="SET NULL"), nullable=True
    )
    protection_instance_id: Mapped[str | None] = mapped_column(
        String(253), nullable=True
    )
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ImageReleaseStep(Base):
    """Per-instance progress used by the serial local release state machine."""

    __tablename__ = "image_release_steps"
    __table_args__ = (
        UniqueConstraint("release_id", "position", name="uq_release_step_position"),
        Index("ix_release_steps_release_status", "release_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    release_id: Mapped[str] = mapped_column(
        ForeignKey("image_releases.id", ondelete="CASCADE"), index=True
    )
    service_instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("service_instances.id", ondelete="SET NULL"), nullable=True
    )
    container_id: Mapped[str | None] = mapped_column(
        ForeignKey("containers.id", ondelete="SET NULL"), nullable=True
    )
    position: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    stage: Mapped[str] = mapped_column(String(40), default="pending")
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    compose_status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ServiceImageVersion(Base):
    """A successfully activated image that can be selected as a rollback target."""

    __tablename__ = "service_image_versions"
    __table_args__ = (
        UniqueConstraint("service_id", "image", name="uq_service_image_version"),
        Index("ix_service_versions_activated", "service_id", "activated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    service_id: Mapped[str] = mapped_column(
        ForeignKey("scaling_services.id", ondelete="CASCADE"), index=True
    )
    image: Mapped[str] = mapped_column(String(512))
    release_id: Mapped[str | None] = mapped_column(
        ForeignKey("image_releases.id", ondelete="SET NULL"), nullable=True
    )
    activated_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    compose_manifest_json: Mapped[str] = mapped_column(Text, default="{}")
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ServiceInstance(Base):
    __tablename__ = "service_instances"
    __table_args__ = (
        UniqueConstraint("service_id", "instance_id", name="uq_service_instance"),
        Index("ix_service_instances_updated", "service_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    service_id: Mapped[str] = mapped_column(
        ForeignKey("scaling_services.id", ondelete="CASCADE"), index=True
    )
    instance_id: Mapped[str] = mapped_column(String(253))
    container_id: Mapped[str | None] = mapped_column(
        ForeignKey("containers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    active_businesses: Mapped[int] = mapped_column(Integer, default=0)
    capacity_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pool: Mapped[str] = mapped_column(String(12), default="local", index=True)
    healthy: Mapped[bool] = mapped_column(Boolean, default=True)
    ready: Mapped[bool] = mapped_column(Boolean, default=True)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    accepting: Mapped[bool] = mapped_column(Boolean, default=True)
    desired_state: Mapped[str] = mapped_column(String(16), default="serve")
    endpoint_scheme: Mapped[str | None] = mapped_column(String(12), nullable=True)
    endpoint_host: Mapped[str | None] = mapped_column(String(253), nullable=True)
    endpoint_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    drain_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class BusinessMetricSample(Base):
    __tablename__ = "business_metric_samples"
    __table_args__ = (
        Index("ix_business_samples_service_time", "service_id", "sampled_at"),
        Index("ix_business_samples_time", "sampled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_id: Mapped[str] = mapped_column(
        ForeignKey("scaling_services.id", ondelete="CASCADE"), index=True
    )
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    active_businesses: Mapped[int] = mapped_column(Integer, default=0)
    total_capacity: Mapped[int] = mapped_column(Integer, default=0)
    reporting_instances: Mapped[int] = mapped_column(Integer, default=0)


class ScalingEvent(Base):
    __tablename__ = "scaling_events"
    __table_args__ = (
        Index("ix_scaling_events_service_time", "service_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    service_id: Mapped[str] = mapped_column(
        ForeignKey("scaling_services.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    from_replicas: Mapped[int] = mapped_column(Integer, default=0)
    to_replicas: Mapped[int] = mapped_column(Integer, default=0)
    average_businesses: Mapped[float] = mapped_column(Float, default=0.0)
    utilization_percent: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
