from __future__ import annotations

from contextlib import asynccontextmanager
from functools import lru_cache
from uuid import uuid4

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware

from infra.local.bootstrap_localstack import (
    configure_bucket_notifications,
    configure_input_bucket_cors,
    ensure_ingest_queue,
    ensure_input_bucket,
    ensure_jobs_table,
)
from .models import CreateJobRequest, CreateJobResponse, JobRecord, JobResponse, JobStatus, UploadInstructions, utc_now
from .settings import Settings, get_settings
from .store import DynamoDbJobStore, JobStore, InMemoryJobStore

UPLOAD_URL_TTL_SECONDS = 900


def bootstrap_local_resources_if_needed(settings: Settings) -> None:
    endpoint_url = settings.aws_endpoint_url or "http://localhost:4566"
    access_key = settings.aws_access_key_id or "test"
    secret_key = settings.aws_secret_access_key or "test"

    if settings.store_backend == "dynamodb" and settings.auto_create_jobs_table:
        ensure_jobs_table(
            table_name=settings.jobs_table_name,
            endpoint_url=endpoint_url,
            region_name=settings.aws_region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

    if settings.auto_create_input_bucket:
        ensure_input_bucket(
            bucket_name=settings.input_bucket_name,
            endpoint_url=endpoint_url,
            region_name=settings.aws_region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        configure_input_bucket_cors(
            bucket_name=settings.input_bucket_name,
            endpoint_url=endpoint_url,
            region_name=settings.aws_region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

    queue_url = None
    queue_arn = None
    if settings.auto_create_ingest_queue:
        queue_url, queue_arn = ensure_ingest_queue(
            queue_name=settings.ingest_queue_name,
            endpoint_url=endpoint_url,
            region_name=settings.aws_region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

    if settings.auto_configure_bucket_notifications:
        if queue_url is None or queue_arn is None:
            queue_url, queue_arn = ensure_ingest_queue(
                queue_name=settings.ingest_queue_name,
                endpoint_url=endpoint_url,
                region_name=settings.aws_region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
            )
        configure_bucket_notifications(
            bucket_name=settings.input_bucket_name,
            queue_url=queue_url,
            queue_arn=queue_arn,
            endpoint_url=endpoint_url,
            region_name=settings.aws_region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    bootstrap_local_resources_if_needed(get_settings())
    yield


app = FastAPI(title="Elastic API", version="0.1.0", lifespan=lifespan)

_settings_for_cors = get_settings()
if _settings_for_cors.aws_endpoint_url:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


def build_input_key(job_id: str) -> str:
    return f"inputs/{job_id}/source"


def build_output_key(job_id: str, preset: str) -> str:
    return f"outputs/{job_id}/final/{preset}.mp4"


def _job_response(job: JobRecord) -> JobResponse:
    return JobResponse(
        job_id=job.job_id,
        status=job.status,
        preset=job.preset,
        attempt_count=job.attempt_count,
        input_key=job.input_key,
        output_key=job.output_key,
        last_error=job.last_error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def build_upload_instructions(
    *,
    content_type: str,
    input_bucket: str,
    input_key: str,
    s3_client: BaseClient,
) -> UploadInstructions:
    upload_url = s3_client.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": input_bucket,
            "Key": input_key,
            "ContentType": content_type,
        },
        ExpiresIn=UPLOAD_URL_TTL_SECONDS,
        HttpMethod="PUT",
    )
    return UploadInstructions(
        method="PUT",
        url=upload_url,
        headers={"Content-Type": content_type},
        expires_in_seconds=UPLOAD_URL_TTL_SECONDS,
    )


@lru_cache
def _build_store(
    store_backend: str,
    jobs_table_name: str,
    aws_region: str,
    aws_endpoint_url: str | None,
    aws_access_key_id: str | None,
    aws_secret_access_key: str | None,
) -> JobStore:
    if store_backend == "dynamodb":
        client = boto3.client(
            "dynamodb",
            region_name=aws_region,
            endpoint_url=aws_endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )
        return DynamoDbJobStore(table_name=jobs_table_name, client=client)
    return InMemoryJobStore()


@lru_cache
def _build_s3_client(
    aws_region: str,
    aws_endpoint_url: str | None,
    aws_access_key_id: str | None,
    aws_secret_access_key: str | None,
) -> BaseClient:
    return boto3.client(
        "s3",
        region_name=aws_region,
        endpoint_url=aws_endpoint_url,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def get_store(settings: Settings = Depends(get_settings)) -> JobStore:
    return _build_store(
        store_backend=settings.store_backend,
        jobs_table_name=settings.jobs_table_name,
        aws_region=settings.aws_region,
        aws_endpoint_url=settings.aws_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


def get_s3_client(settings: Settings = Depends(get_settings)) -> BaseClient:
    return _build_s3_client(
        aws_region=settings.aws_region,
        aws_endpoint_url=settings.aws_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


@app.get("/healthz")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/jobs", response_model=list[JobResponse])
def list_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    store: JobStore = Depends(get_store),
) -> list[JobResponse]:
    return [_job_response(job) for job in store.list_jobs(limit=limit)]


@app.post("/jobs", response_model=CreateJobResponse, status_code=status.HTTP_201_CREATED)
def create_job(
    request: CreateJobRequest,
    settings: Settings = Depends(get_settings),
    store: JobStore = Depends(get_store),
    s3_client: BaseClient = Depends(get_s3_client),
) -> CreateJobResponse:
    if request.preset != "1080p":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only the 1080p preset is supported in v1.",
        )

    job_id = uuid4().hex
    timestamp = utc_now()
    input_key = build_input_key(job_id)
    output_key = build_output_key(job_id, request.preset)
    job = JobRecord(
        job_id=job_id,
        status=JobStatus.UPLOADING,
        preset=request.preset,
        input_bucket=settings.input_bucket_name,
        input_key=input_key,
        output_key=output_key,
        created_at=timestamp,
        updated_at=timestamp,
    )
    store.create_job(job)

    return CreateJobResponse(
        job_id=job.job_id,
        status=job.status,
        preset=job.preset,
        input_bucket=job.input_bucket,
        input_key=job.input_key,
        output_key=job.output_key,
        upload=build_upload_instructions(
            content_type=request.content_type,
            input_bucket=job.input_bucket,
            input_key=job.input_key,
            s3_client=s3_client,
        ),
    )


@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, store: JobStore = Depends(get_store)) -> JobResponse:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    return _job_response(job)
