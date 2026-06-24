from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="elastic_", env_file=".env", extra="ignore")

    store_backend: str = Field(default="memory")
    jobs_table_name: str = Field(default="elastic-jobs")
    input_bucket_name: str = Field(default="elastic-inputs")
    ingest_queue_name: str = Field(default="elastic-ingest")
    aws_region: str = Field(default="us-east-1")
    aws_endpoint_url: str | None = Field(default=None)
    aws_access_key_id: str | None = Field(default=None)
    aws_secret_access_key: str | None = Field(default=None)
    auto_create_jobs_table: bool = Field(default=False)
    auto_create_input_bucket: bool = Field(default=False)
    auto_create_ingest_queue: bool = Field(default=False)
    auto_configure_bucket_notifications: bool = Field(default=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()
