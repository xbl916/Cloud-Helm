import socket
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLOUDHELM_AGENT_", extra="ignore")

    server_url: str
    enrollment_token: str = ""
    name: str = socket.gethostname()
    environment: str = "default"
    state_file: Path = Path("/data/agent-state.json")
    poll_seconds: float = Field(default=3.0, ge=1, le=60)
    report_seconds: float = Field(default=15.0, ge=5, le=300)
    request_timeout_seconds: float = Field(default=20.0, ge=3, le=120)
    verify_tls: bool = True
    max_containers: int = Field(default=500, ge=1, le=2000)
    disk_query_seconds: float = Field(default=300.0, ge=60, le=3600)
    host_root_path: Path = Path("/host/rootfs-marker")
    host_network_stats_path: Path = Path("/host/network-dev")
    host_cpu_stats_path: Path = Path("/host/proc-stat")
    host_memory_stats_path: Path = Path("/host/meminfo")
    host_load_stats_path: Path = Path("/host/loadavg")
    host_uptime_stats_path: Path = Path("/host/uptime")
    gpu_monitoring_enabled: bool = True
    nvidia_smi_path: Path = Path("/usr/bin/nvidia-smi")
    gpu_query_timeout_seconds: float = Field(default=5.0, ge=1, le=30)
    gpu_max_output_bytes: int = Field(default=4194304, ge=65536, le=16777216)

    @property
    def api_url(self) -> str:
        return self.server_url.rstrip("/") + "/api/v1"
