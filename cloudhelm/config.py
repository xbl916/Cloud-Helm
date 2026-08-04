from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="CLOUDHELM_", extra="ignore"
    )

    app_name: str = "云舵 Cloud Helm"
    environment: str = "production"
    database_url: str = "sqlite:////data/cloudhelm.db"
    agent_enrollment_token: str = Field(min_length=16)
    public_base_url: str
    wecom_corp_id: str = Field(min_length=2, max_length=128)
    wecom_agent_id: str = Field(min_length=1, max_length=32)
    wecom_secret: str = Field(min_length=8, max_length=512)
    wecom_miniprogram_secret: str | None = Field(
        default=None, min_length=8, max_length=512
    )
    bootstrap_admin_wecom_userid: str = Field(min_length=1, max_length=128)
    bootstrap_admin_display_name: str = Field(default="系统管理员", max_length=120)
    session_minutes: int = Field(default=60, ge=5, le=1440)
    max_sessions_per_user: int = Field(default=5, ge=1, le=20)
    oauth_state_seconds: int = Field(default=300, ge=60, le=600)
    wecom_api_timeout_seconds: float = Field(default=8.0, ge=2, le=30)
    wecom_api_base: str = "https://qyapi.weixin.qq.com"
    trust_proxy_headers: bool = False
    static_dir: Path = Path(__file__).parent / "static"
    max_task_result_bytes: int = Field(default=262144, ge=4096, le=2097152)
    node_offline_seconds: int = Field(default=60, ge=15, le=3600)

    @field_validator(
        "agent_enrollment_token", "wecom_secret", "wecom_miniprogram_secret"
    )
    @classmethod
    def reject_example_secrets(cls, value: str | None) -> str | None:
        if value is None:
            return value
        lowered = value.lower()
        if "change-me" in lowered or "replace-me" in lowered:
            raise ValueError("example secrets must be replaced")
        return value

    @field_validator(
        "wecom_corp_id", "wecom_agent_id", "bootstrap_admin_wecom_userid"
    )
    @classmethod
    def reject_example_identity(cls, value: str) -> str:
        if "replace_me" in value.lower():
            raise ValueError("example identity values must be replaced")
        return value.strip()

    @field_validator("public_base_url")
    @classmethod
    def normalize_public_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_public_url(self) -> "Settings":
        parsed = urlsplit(self.public_base_url)
        if not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("public_base_url must contain only scheme and host")
        if self.environment == "production" and parsed.scheme != "https":
            raise ValueError("public_base_url must use HTTPS in production")
        if self.environment == "production" and parsed.hostname == "ops.example.com":
            raise ValueError("example public_base_url must be replaced")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("public_base_url must use HTTP or HTTPS")
        return self

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_base_url).hostname or ""


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
