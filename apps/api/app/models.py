from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    CREATED = "CREATED"
    UPLOADING = "UPLOADING"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class CreateJobRequest(BaseModel):
    filename: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    size_bytes: int = Field(ge=1)
    preset: str


class UploadInstructions(BaseModel):
    method: str
    url: str
    headers: Dict[str, str]
    expires_in_seconds: int


class CreateJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    preset: str
    input_bucket: str
    input_key: str
    output_key: str
    upload: UploadInstructions


class JobRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    job_id: str
    status: JobStatus
    preset: str
    input_bucket: str
    input_key: str
    output_key: str
    attempt_count: int = 0
    last_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class JobResponse(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    job_id: str
    status: JobStatus
    preset: str
    attempt_count: int
    input_key: str
    output_key: str
    last_error: Optional[str]
    created_at: datetime
    updated_at: datetime
