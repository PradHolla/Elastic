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


class PartUploadInstruction(BaseModel):
    part_number: int
    url: str


class MultipartUploadInstructions(BaseModel):
    upload_id: str
    part_size_bytes: int
    parts: list[PartUploadInstruction]
    complete_path: str
    abort_path: str
    expires_in_seconds: int


class CompletedPart(BaseModel):
    part_number: int = Field(ge=1)
    etag: str = Field(min_length=1)


class CompleteUploadRequest(BaseModel):
    upload_id: str = Field(min_length=1)
    parts: list[CompletedPart] = Field(min_length=1)


class AbortUploadRequest(BaseModel):
    upload_id: str = Field(min_length=1)


class CreateJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    preset: str
    input_bucket: str
    input_key: str
    output_key: str
    upload: Optional[UploadInstructions] = None
    multipart_upload: Optional[MultipartUploadInstructions] = None


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
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
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
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
