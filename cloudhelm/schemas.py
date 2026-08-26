import ipaddress
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from cloudhelm.models import TaskStatus, UserRole


class UserOut(BaseModel):
    id: str
    username: str
    wecom_userid: str
    display_name: str
    role: UserRole
    can_manage_access: bool = False
    can_view_scaling: bool = False

    model_config = {"from_attributes": True}


class MiniProgramLogin(BaseModel):
    code: str = Field(min_length=1, max_length=512)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("企业微信登录凭证不能为空")
        return value


class MiniProgramLoginOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    user: UserOut


class UserCreate(BaseModel):
    wecom_userid: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=120)
    role: UserRole = UserRole.viewer

    @field_validator("wecom_userid")
    @classmethod
    def normalize_wecom_userid(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("企业微信 UserId 不能为空")
        return value


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    wecom_userid: str | None = Field(default=None, min_length=1, max_length=128)
    role: UserRole | None = None
    is_active: bool | None = None
    elevated_access_action: Literal["retain", "clear"] | None = None

    @field_validator("wecom_userid")
    @classmethod
    def normalize_optional_wecom_userid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("企业微信 UserId 不能为空")
        return value


class AccessRuleInput(BaseModel):
    scope_type: Literal["all", "environment", "node", "project", "container", "service"]
    environment: str | None = Field(default=None, max_length=40)
    node_id: str | None = Field(default=None, max_length=36)
    project: str | None = Field(default=None, max_length=255)
    container_id: str | None = Field(default=None, max_length=36)
    service_id: str | None = Field(default=None, max_length=36)
    can_view: bool = True
    can_logs: bool = False
    can_operate: bool = False
    can_manage: bool = False
    alert_notify: bool = False


class AccessConfigInput(BaseModel):
    restricted: bool
    global_alert_notify: bool = False
    rules: list[AccessRuleInput] = Field(default_factory=list, max_length=500)
    expected_version: int | None = Field(default=None, ge=1)


class AlertRuleInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scope_type: Literal["all", "environment", "node", "container"] = "all"
    environment: str | None = Field(default=None, max_length=40)
    node_id: str | None = Field(default=None, max_length=36)
    container_id: str | None = Field(default=None, max_length=36)
    metric: Literal[
        "node_offline",
        "node_cpu_percent",
        "node_memory_percent",
        "node_disk_percent",
        "node_inode_percent",
        "container_cpu_percent",
        "container_memory_percent",
        "container_unhealthy",
        "container_oom_killed",
        "container_restarting",
        "node_gpu_utilization_percent",
        "node_gpu_memory_percent",
        "node_gpu_temperature_c",
        "node_gpu_missing",
        "node_network_surge_percent",
        "container_disk_growth_mibps",
        "container_exit_abnormal",
        "node_load_percent",
        "node_swap_percent",
        "node_metrics_unavailable",
        "service_business_overload_percent",
    ]
    operator: Literal["gte", "lte"] = "gte"
    threshold: float = Field(ge=0, le=1000000)
    consecutive_required: int = Field(default=1, ge=1, le=100)
    severity: Literal["info", "warning", "critical"] = "warning"
    enabled: bool = True
    notify: bool = True


class EnrollRequest(BaseModel):
    enrollment_token: str
    agent_key: str = Field(min_length=8, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    hostname: str = Field(default="", max_length=255)
    environment: str = Field(default="default", max_length=40)
    agent_version: str = Field(default="unknown", max_length=30)


class EnrollResponse(BaseModel):
    node_id: str
    node_token: str


class ContainerSnapshot(BaseModel):
    docker_id: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    image: str = Field(default="", max_length=512)
    status: str = Field(default="unknown", max_length=40)
    health: str | None = Field(default=None, max_length=40)
    compose_project: str | None = Field(default=None, max_length=255)
    compose_service: str | None = Field(default=None, max_length=255)
    cpu_percent: float = Field(default=0, ge=0, le=100000)
    memory_usage: int = Field(default=0, ge=0)
    memory_limit: int = Field(default=0, ge=0)
    memory_percent: float = Field(default=0, ge=0, le=100000)
    network_rx_bytes: int = Field(default=0, ge=0)
    network_tx_bytes: int = Field(default=0, ge=0)
    network_rx_bps: float = Field(default=0, ge=0)
    network_tx_bps: float = Field(default=0, ge=0)
    writable_layer_bytes: int = Field(default=0, ge=0)
    rootfs_bytes: int = Field(default=0, ge=0)
    writable_layer_growth_mibps: float | None = Field(default=None, ge=0)
    block_read_bytes: int = Field(default=0, ge=0)
    block_write_bytes: int = Field(default=0, ge=0)
    block_read_bps: float = Field(default=0, ge=0)
    block_write_bps: float = Field(default=0, ge=0)
    pids: int = Field(default=0, ge=0)
    restart_count: int = Field(default=0, ge=0)
    oom_killed: bool = False
    exit_code: int | None = None
    finished_at: str | None = Field(default=None, max_length=60)
    health_failing_streak: int = Field(default=0, ge=0)
    started_at: str | None = Field(default=None, max_length=60)
    ports: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    gpu_devices: list[str] = Field(default_factory=list, max_length=128)
    gpu_all: bool = False


class GpuSnapshot(BaseModel):
    index: int = Field(ge=0, le=1024)
    uuid: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    driver_version: str = Field(default="unknown", max_length=80)
    cuda_version: str | None = Field(default=None, max_length=40)
    utilization_gpu: float | None = Field(default=None, ge=0, le=100)
    utilization_memory: float | None = Field(default=None, ge=0, le=100)
    memory_used_mib: int | None = Field(default=None, ge=0)
    memory_total_mib: int | None = Field(default=None, ge=0)
    temperature_c: float | None = Field(default=None, ge=-100, le=300)
    power_draw_w: float | None = Field(default=None, ge=0, le=100000)
    power_limit_w: float | None = Field(default=None, ge=0, le=100000)
    fan_speed_percent: float | None = Field(default=None, ge=0, le=10000)
    mig_mode: str | None = Field(default=None, max_length=40)


class NetworkInterfaceMetricsSnapshot(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    addresses: list[Annotated[str, Field(max_length=80)]] = Field(
        default_factory=list, max_length=16
    )
    rx_bytes: int = Field(default=0, ge=0)
    tx_bytes: int = Field(default=0, ge=0)
    rx_bps: float = Field(default=0, ge=0)
    tx_bps: float = Field(default=0, ge=0)


class SystemMetricsSnapshot(BaseModel):
    cpu_percent: float = Field(default=0, ge=0, le=100)
    cpu_count: int = Field(default=0, ge=0, le=65536)
    memory_total_bytes: int = Field(default=0, ge=0)
    memory_used_bytes: int = Field(default=0, ge=0)
    memory_available_bytes: int = Field(default=0, ge=0)
    memory_percent: float = Field(default=0, ge=0, le=100)
    swap_total_bytes: int = Field(default=0, ge=0)
    swap_used_bytes: int = Field(default=0, ge=0)
    load_1: float = Field(default=0, ge=0)
    load_5: float = Field(default=0, ge=0)
    load_15: float = Field(default=0, ge=0)
    uptime_seconds: float = Field(default=0, ge=0)
    disk_total_bytes: int = Field(default=0, ge=0)
    disk_used_bytes: int = Field(default=0, ge=0)
    disk_free_bytes: int = Field(default=0, ge=0)
    disk_inodes_total: int = Field(default=0, ge=0)
    disk_inodes_used: int = Field(default=0, ge=0)
    disk_inodes_free: int = Field(default=0, ge=0)
    network_rx_bytes: int = Field(default=0, ge=0)
    network_tx_bytes: int = Field(default=0, ge=0)
    network_rx_bps: float = Field(default=0, ge=0)
    network_tx_bps: float = Field(default=0, ge=0)
    network_interfaces: list[str] = Field(default_factory=list, max_length=128)
    network_interface_metrics: list[NetworkInterfaceMetricsSnapshot] = Field(
        default_factory=list, max_length=128
    )


class HeartbeatRequest(BaseModel):
    hostname: str = Field(default="", max_length=255)
    agent_version: str = Field(default="unknown", max_length=30)
    docker_version: str = Field(default="unknown", max_length=80)
    os: str = Field(default="unknown", max_length=120)
    gpu_status: Literal["ok", "unavailable", "error", "disabled"] = "unavailable"
    gpu_error: str | None = Field(default=None, max_length=500)
    gpus: list[GpuSnapshot] = Field(default_factory=list, max_length=128)
    system_metrics_status: Literal["ok", "unavailable", "error"] = "unavailable"
    system_metrics_error: str | None = Field(default=None, max_length=500)
    system_metrics: SystemMetricsSnapshot = Field(default_factory=SystemMetricsSnapshot)
    containers: list[ContainerSnapshot] = Field(default_factory=list, max_length=2000)


class AgentTask(BaseModel):
    id: str
    docker_id: str
    action: Literal["start", "stop", "restart", "logs", "update_image"]
    arguments: dict[str, Any]


class TaskResultRequest(BaseModel):
    success: bool
    result: str | None = None
    error: str | None = None
    docker_id: str | None = Field(default=None, min_length=8, max_length=128)


class ActionRequest(BaseModel):
    action: Literal["start", "stop", "restart", "logs", "update_image"]
    tail: int = Field(default=200, ge=10, le=2000)
    target_image: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def require_target_image(self) -> "ActionRequest":
        if self.action == "update_image" and not self.target_image:
            raise ValueError("更新镜像时必须提供目标镜像")
        if self.action != "update_image" and self.target_image is not None:
            raise ValueError("该操作不接受目标镜像")
        if self.target_image is not None:
            self.target_image = self.target_image.strip()
        return self


class TaskOut(BaseModel):
    id: str
    node_id: str
    container_id: str | None
    action: str
    status: TaskStatus
    result: str | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}

    @field_serializer("created_at", "finished_at")
    def serialize_utc_datetime(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()


class BusinessMetricReport(BaseModel):
    instance_id: str = Field(min_length=1, max_length=253)
    active_businesses: int = Field(ge=0, le=100000000)
    pool: Literal["local", "acs"] = "local"
    healthy: bool = True
    ready: bool = True
    accepting: bool = True
    container_id: str | None = Field(default=None, max_length=36)
    endpoint_scheme: Literal["http", "https", "grpc", "grpcs"] | None = None
    endpoint_host: str | None = Field(default=None, min_length=1, max_length=253)
    endpoint_port: int | None = Field(default=None, ge=1, le=65535)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("instance_id")
    @classmethod
    def normalize_instance_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("实例 ID 不能为空")
        return value

    @field_validator("endpoint_host")
    @classmethod
    def normalize_endpoint_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        if (
            not value
            or any(character.isspace() for character in value)
            or any(character in value for character in "/?#@")
        ):
            raise ValueError("服务地址必须是 IP 或不含路径的主机名")
        try:
            return str(ipaddress.ip_address(value))
        except ValueError as exc:
            raise ValueError("服务地址必须是有效的 IPv4 或 IPv6 地址") from exc

    @model_validator(mode="after")
    def validate_endpoint(self) -> "BusinessMetricReport":
        if (self.endpoint_scheme is None) != (self.endpoint_port is None):
            raise ValueError("endpoint_scheme、endpoint_port 必须同时上报")
        if self.endpoint_host is not None and self.endpoint_scheme is None:
            raise ValueError("上报 endpoint_host 时必须同时上报协议和端口")
        return self


class ScalingServiceInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    service_type: Literal["vlm", "tts", "asr", "llm", "other"] = "other"
    capacity_per_replica: int = Field(default=1, ge=1, le=100000000)
    autoscaling_enabled: bool = False
    min_replicas: int = Field(default=0, ge=0, le=10000)
    max_replicas: int = Field(default=1, ge=1, le=10000)
    scale_up_threshold_percent: float = Field(default=70, gt=0, le=100)
    scale_down_threshold_percent: float = Field(default=40, ge=0, lt=100)
    window_seconds: int = Field(default=300, ge=60, le=3600)
    cooldown_seconds: int = Field(default=300, ge=30, le=86400)
    report_stale_seconds: int = Field(default=60, ge=15, le=3600)
    scale_up_step: int = Field(default=1, ge=1, le=1000)
    failover_replicas: int = Field(default=1, ge=1, le=10000)
    provider: Literal["acs_kubernetes"] = "acs_kubernetes"
    namespace: str = Field(default="default", min_length=1, max_length=253)
    workload_kind: Literal["Deployment", "StatefulSet"] = "Deployment"
    workload_name: str = Field(min_length=1, max_length=253)
    acs_container_name: str = Field(default="", max_length=253)

    @model_validator(mode="after")
    def validate_scaling_thresholds(self) -> "ScalingServiceInput":
        if self.min_replicas > self.max_replicas:
            raise ValueError("最小副本数不能大于最大副本数")
        if self.scale_down_threshold_percent >= self.scale_up_threshold_percent:
            raise ValueError("缩容阈值必须小于扩容阈值")
        return self


class ServiceInstanceCapacityInput(BaseModel):
    capacity_limit: int | None = Field(default=None, ge=1, le=100000000)


class ServiceInstanceMonitoringInput(BaseModel):
    monitored: bool


class NodeGatewayAddressInput(BaseModel):
    gateway_address: str | None = Field(default=None, max_length=45)

    @field_validator("gateway_address")
    @classmethod
    def normalize_gateway_address(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("网关访问地址必须是有效的 IPv4 或 IPv6 地址") from exc
        if (
            address.is_unspecified
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
        ):
            raise ValueError("该 IP 不能作为网关访问地址")
        return str(address)


class ModuleReleaseInput(BaseModel):
    target_image: str = Field(min_length=1, max_length=512)
