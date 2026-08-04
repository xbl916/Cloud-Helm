from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from cloudhelm.models import TaskStatus, UserRole


class UserOut(BaseModel):
    id: str
    username: str
    wecom_userid: str
    display_name: str
    role: UserRole

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
    scope_type: Literal["all", "environment", "node", "project", "container"]
    environment: str | None = Field(default=None, max_length=40)
    node_id: str | None = Field(default=None, max_length=36)
    project: str | None = Field(default=None, max_length=255)
    container_id: str | None = Field(default=None, max_length=36)
    can_view: bool = True
    can_logs: bool = False
    can_operate: bool = False


class AccessConfigInput(BaseModel):
    restricted: bool
    rules: list[AccessRuleInput] = Field(default_factory=list, max_length=500)


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


class HeartbeatRequest(BaseModel):
    hostname: str = Field(default="", max_length=255)
    agent_version: str = Field(default="unknown", max_length=30)
    docker_version: str = Field(default="unknown", max_length=80)
    os: str = Field(default="unknown", max_length=120)
    gpu_status: Literal["ok", "unavailable", "error", "disabled"] = "unavailable"
    gpu_error: str | None = Field(default=None, max_length=500)
    gpus: list[GpuSnapshot] = Field(default_factory=list, max_length=128)
    containers: list[ContainerSnapshot] = Field(default_factory=list, max_length=2000)


class AgentTask(BaseModel):
    id: str
    docker_id: str
    action: Literal["start", "stop", "restart", "logs"]
    arguments: dict[str, Any]


class TaskResultRequest(BaseModel):
    success: bool
    result: str | None = None
    error: str | None = None


class ActionRequest(BaseModel):
    action: Literal["start", "stop", "restart", "logs"]
    tail: int = Field(default=200, ge=10, le=2000)


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
