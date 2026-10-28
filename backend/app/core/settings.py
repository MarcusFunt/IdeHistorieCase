"""Settings originate from container environment; no secrets are embedded in source."""
from functools import lru_cache
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

WORKER_HEALTH_TIMEOUT_SECONDS = 4.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    db_host: str = "postgres"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "ddu_game"
    db_user: str = "ddu_app"
    db_password: str = ""
    llm_base_url: str = Field(default="http://ddu-llm:8080", min_length=1, max_length=2048)
    llm_model_alias: str = Field(default="ddu-narrator", min_length=1, max_length=128)
    llm_enable_thinking: bool = False
    worker_poll_seconds: float = Field(default=10.0, ge=1.0, le=600.0)
    worker_stale_seconds: int = Field(default=45, ge=2, le=3600)

    @field_validator("llm_base_url")
    @classmethod
    def validate_llm_base_url(cls, value: str) -> str:
        value = value.strip()
        try:
            parsed = urlsplit(value)
            parsed.port  # Accessing port validates its syntax and range.
        except ValueError as error:
            raise ValueError("llm_base_url must be a valid HTTP(S) URL") from error
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("llm_base_url must use HTTP or HTTPS and include a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("llm_base_url must not include credentials")
        return value.rstrip("/")

    @field_validator("llm_model_alias")
    @classmethod
    def validate_llm_model_alias(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("llm_model_alias must not be empty")
        return value

    @model_validator(mode="after")
    def validate_worker_timing(self) -> Self:
        if self.worker_stale_seconds <= self.worker_poll_seconds + WORKER_HEALTH_TIMEOUT_SECONDS:
            raise ValueError(
                "worker_stale_seconds must exceed worker_poll_seconds plus the "
                "4-second health-check timeout; allow extra time for database writes"
            )
        return self

    @property
    def database_url(self) -> URL:
        # URL.create safely quotes punctuation in passwords, unlike manual DSN concatenation.
        return URL.create(
            "postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
