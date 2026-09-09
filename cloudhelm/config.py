import base64
import binascii
import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="CLOUDHELM_", extra="ignore"
    )

    app_name: str = "云舵 Cloud Helm"
    environment: str = "production"
    display_timezone: str = Field(
        default_factory=lambda: os.environ.get("TZ", "Asia/Shanghai")
    )
    database_url: str = "sqlite:////data/cloudhelm.db"
    agent_enrollment_token: str = Field(min_length=16)
    public_base_url: str
    wecom_corp_id: str = Field(min_length=2, max_length=128)
    wecom_agent_id: str = Field(min_length=1, max_length=32)
    wecom_secret: str = Field(min_length=8, max_length=512)
    wecom_miniprogram_secret: str | None = Field(
        default=None, min_length=8, max_length=512
    )
    wecom_callback_token: str | None = Field(default=None, min_length=16, max_length=256)
    wecom_callback_aes_key: str | None = Field(default=None, min_length=43, max_length=43)
    bootstrap_admin_wecom_userid: str = Field(min_length=1, max_length=128)
    bootstrap_admin_display_name: str = Field(default="系统管理员", max_length=120)
    session_minutes: int = Field(default=60, ge=5, le=1440)
    max_sessions_per_user: int = Field(default=5, ge=1, le=20)
    oauth_state_seconds: int = Field(default=300, ge=60, le=600)
    wecom_api_timeout_seconds: float = Field(default=8.0, ge=2, le=30)
    wecom_api_base: str = "https://qyapi.weixin.qq.com"
    wecom_qr_authorize_url: str = "https://login.work.weixin.qq.com/wwlogin/sso/login"
    gateway_discovery_token: str | None = Field(
        default=None, min_length=16, max_length=512
    )
    gateway_discovery_allowed_cidrs: str = ""
    trust_proxy_headers: bool = False
    static_dir: Path = Path(__file__).parent / "static"
    max_task_result_bytes: int = Field(default=262144, ge=4096, le=2097152)
    node_offline_seconds: int = Field(default=60, ge=15, le=3600)
    metrics_history_enabled: bool = True
    metrics_history_interval_seconds: int = Field(default=300, ge=60, le=3600)
    metrics_history_retention_hours: int = Field(default=168, ge=1, le=8760)
    metrics_history_max_rows: int = Field(default=200000, ge=1000, le=20000000)
    alerts_enabled: bool = True
    alert_event_retention_hours: int = Field(default=2160, ge=24, le=17520)
    alert_event_max_rows: int = Field(default=10000, ge=100, le=1000000)
    alert_notifications_enabled: bool = False
    autoscaling_enabled: bool = False
    autoscaling_interval_seconds: int = Field(default=15, ge=5, le=300)
    autoscaling_history_retention_hours: int = Field(default=24, ge=1, le=8760)
    acs_kubernetes_api_url: str | None = None
    acs_kubernetes_bearer_token: str | None = Field(
        default=None, min_length=16, max_length=16384
    )
    acs_kubernetes_bearer_token_file: Path | None = None
    acs_kubernetes_ca_file: Path | None = None
    acs_kubernetes_timeout_seconds: float = Field(default=10, ge=2, le=60)
    image_release_timeout_seconds: int = Field(default=3600, ge=300, le=86400)

    @field_validator(
        "agent_enrollment_token",
        "wecom_secret",
        "wecom_miniprogram_secret",
        "gateway_discovery_token",
        "wecom_callback_token",
    )
    @classmethod
    def reject_example_secrets(cls, value: str | None) -> str | None:
        if value is None:
            return value
        lowered = value.lower()
        if "change-me" in lowered or "replace-me" in lowered or "replace_me" in lowered:
            raise ValueError("example secrets must be replaced")
        return value

    @field_validator("wecom_corp_id", "wecom_agent_id", "bootstrap_admin_wecom_userid")
    @classmethod
    def reject_example_identity(cls, value: str) -> str:
        if "replace_me" in value.lower():
            raise ValueError("example identity values must be replaced")
        return value.strip()

    @field_validator("public_base_url")
    @classmethod
    def normalize_public_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("display_timezone")
    @classmethod
    def validate_display_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("display_timezone must be a valid IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def validate_public_url(self) -> "Settings":
        if bool(self.wecom_callback_token) != bool(self.wecom_callback_aes_key):
            raise ValueError("wecom callback token and AES key must be configured together")
        if self.wecom_callback_aes_key:
            try:
                key = base64.b64decode(self.wecom_callback_aes_key + "=", validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("invalid wecom callback AES key") from exc
            if len(key) != 32:
                raise ValueError("invalid wecom callback AES key")
        parsed = urlsplit(self.public_base_url)
        if not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("public_base_url must contain only scheme and host")
        if self.environment == "production" and parsed.scheme != "https":
            raise ValueError("public_base_url must use HTTPS in production")
        if self.environment == "production" and parsed.hostname == "ops.example.com":
            raise ValueError("example public_base_url must be replaced")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("public_base_url must use HTTP or HTTPS")
        for name, url in {
            "wecom_qr_authorize_url": self.wecom_qr_authorize_url,
            "wecom_api_base": self.wecom_api_base,
        }.items():
            candidate = urlsplit(url)
            if (
                not candidate.hostname
                or candidate.scheme not in {"http", "https"}
                or candidate.username
                or candidate.password
                or candidate.query
                or candidate.fragment
            ):
                raise ValueError(f"{name} must be an absolute HTTP(S) URL")
            if self.environment == "production" and candidate.scheme != "https":
                raise ValueError(f"{name} must use HTTPS in production")
        cidrs = [
            item.strip()
            for item in self.gateway_discovery_allowed_cidrs.split(",")
            if item.strip()
        ]
        if self.gateway_discovery_token and not cidrs:
            raise ValueError(
                "gateway_discovery_allowed_cidrs is required when gateway discovery is enabled"
            )
        try:
            for cidr in cidrs:
                ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            raise ValueError(
                "gateway_discovery_allowed_cidrs contains invalid CIDR"
            ) from exc
        return self

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_base_url).hostname or ""

    @property
    def gateway_discovery_networks(
        self,
    ) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return tuple(
            ipaddress.ip_network(item.strip(), strict=False)
            for item in self.gateway_discovery_allowed_cidrs.split(",")
            if item.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
