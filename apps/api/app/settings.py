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

    # Lease-based job ownership. The lease must outlive several heartbeat
    # intervals so a slow renewal does not look like a dead worker.
    worker_id: str | None = Field(default=None)
    lease_duration_seconds: int = Field(default=90)
    max_attempts: int = Field(default=3)

    # Upload limits. Single presigned PUT tops out at 5 GiB on S3; anything at
    # or above the multipart threshold gets a multipart upload instead.
    multipart_threshold_bytes: int = Field(default=100 * 1024 * 1024)
    multipart_part_size_bytes: int = Field(default=64 * 1024 * 1024)
    max_upload_bytes: int = Field(default=50 * 1024 * 1024 * 1024)

    # Reconciliation sweeper (runs inside the API, which is always on).
    reconciler_enabled: bool = Field(default=False)
    reconciler_interval_seconds: int = Field(default=60)
    stale_uploading_seconds: int = Field(default=900)
    stale_queued_seconds: int = Field(default=300)
    upload_expiry_seconds: int = Field(default=86400)

    # Observability.
    log_json: bool = Field(default=False)
    metrics_enabled: bool = Field(default=False)
    metrics_port: int = Field(default=9100)


@lru_cache
def get_settings() -> Settings:
    return Settings()
