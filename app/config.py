from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_key: SecretStr = Field(min_length=16)
    database_url: str
    rabbitmq_url: str
    retry_base_seconds: float = Field(default=1, gt=0, le=3600)
    outbox_poll_seconds: float = Field(default=0.5, gt=0, le=60)
    webhook_timeout_seconds: float = Field(default=10, gt=0, le=60)
    gateway_min_seconds: float = Field(default=2, ge=0)
    gateway_max_seconds: float = Field(default=5, ge=0)
