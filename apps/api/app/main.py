from __future__ import annotations

import math
from contextlib import asynccontextmanager
from functools import lru_cache
from uuid import uuid4

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app

from apps.common.obs import get_logger, setup_logging

from infra.local.bootstrap_localstack import (
    configure_bucket_notifications,
    configure_input_bucket_cors,
    ensure_ingest_queue,
    ensure_input_bucket,
    ensure_jobs_table,
)
from .models import (
    AbortUploadRequest,
    CompleteUploadRequest,
    CreateJobRequest,
    CreateJobResponse,
    JobRecord,
    JobResponse,
    JobStatus,
    MultipartUploadInstructions,
    PartUploadInstruction,
    UploadInstructions,
    utc_now,
)
from .reconciler import Reconciler
from .settings import Settings, get_settings
from .store import DynamoDbJobStore, JobStore, InMemoryJobStore

UPLOAD_URL_TTL_SECONDS = 900
MULTIPART_URL_TTL_SECONDS = 3600
S3_MAX_PARTS = 10_000
S3_MIN_PART_SIZE = 5 * 1024 * 1024

logger = get_logger("elastic.api")


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
    settings = get_settings()
    setup_logging(json_logs=settings.log_json)
    bootstrap_local_resources_if_needed(settings)

    reconciler: Reconciler | None = None
    if settings.reconciler_enabled:
        sqs_client = _build_sqs_client(
            aws_region=settings.aws_region,
            aws_endpoint_url=settings.aws_endpoint_url,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
        try:
            queue_url = sqs_client.get_queue_url(QueueName=settings.ingest_queue_name)["QueueUrl"]
        except ClientError as exc:
            logger.error("reconciler disabled: could not resolve queue url", extra={"error": str(exc)})
        else:
            reconciler = Reconciler(
                settings=settings,
                job_store=get_store(settings),
                s3_client=get_s3_client(settings),
                sqs_client=sqs_client,
                queue_url=queue_url,
            )
            reconciler.start()
    yield
    if reconciler is not None:
        reconciler.stop()


app = FastAPI(title="Elastic API", version="0.1.0", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())

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
        lease_owner=job.lease_owner,
        lease_expires_at=job.lease_expires_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def build_upload_instructions(
    *,
    content_type: str,
    size_bytes: int,
    input_bucket: str,
    input_key: str,
    s3_client: BaseClient,
) -> UploadInstructions:
    # ContentLength is part of the signature, so S3 rejects any upload whose
    # size differs from what the client declared at job creation.
    upload_url = s3_client.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": input_bucket,
            "Key": input_key,
            "ContentType": content_type,
            "ContentLength": size_bytes,
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


def _pick_part_size(size_bytes: int, configured_part_size: int) -> int:
    # Respect S3 limits: parts of at least 5 MiB and at most 10,000 parts.
    minimum_for_count = math.ceil(size_bytes / S3_MAX_PARTS)
    return max(configured_part_size, minimum_for_count, S3_MIN_PART_SIZE)


def build_multipart_upload_instructions(
    *,
    job_id: str,
    content_type: str,
    size_bytes: int,
    part_size_bytes: int,
    input_bucket: str,
    input_key: str,
    s3_client: BaseClient,
) -> MultipartUploadInstructions:
    created = s3_client.create_multipart_upload(
        Bucket=input_bucket,
        Key=input_key,
        ContentType=content_type,
    )
    upload_id = created["UploadId"]
    part_size = _pick_part_size(size_bytes, part_size_bytes)
    part_count = max(1, math.ceil(size_bytes / part_size))
    parts = [
        PartUploadInstruction(
            part_number=part_number,
            url=s3_client.generate_presigned_url(
                ClientMethod="upload_part",
                Params={
                    "Bucket": input_bucket,
                    "Key": input_key,
                    "UploadId": upload_id,
                    "PartNumber": part_number,
                },
                ExpiresIn=MULTIPART_URL_TTL_SECONDS,
            ),
        )
        for part_number in range(1, part_count + 1)
    ]
    return MultipartUploadInstructions(
        upload_id=upload_id,
        part_size_bytes=part_size,
        parts=parts,
        complete_path=f"/jobs/{job_id}/uploads/complete",
        abort_path=f"/jobs/{job_id}/uploads/abort",
        expires_in_seconds=MULTIPART_URL_TTL_SECONDS,
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


@lru_cache
def _build_sqs_client(
    aws_region: str,
    aws_endpoint_url: str | None,
    aws_access_key_id: str | None,
    aws_secret_access_key: str | None,
) -> BaseClient:
    return boto3.client(
        "sqs",
        region_name=aws_region,
        endpoint_url=aws_endpoint_url,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
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
    job_status: JobStatus | None = Query(default=None, alias="status"),
    store: JobStore = Depends(get_store),
) -> list[JobResponse]:
    return [_job_response(job) for job in store.list_jobs(limit=limit, status=job_status)]


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
    if request.size_bytes > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Upload exceeds the {settings.max_upload_bytes} byte limit.",
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

    use_multipart = request.size_bytes >= settings.multipart_threshold_bytes
    return CreateJobResponse(
        job_id=job.job_id,
        status=job.status,
        preset=job.preset,
        input_bucket=job.input_bucket,
        input_key=job.input_key,
        output_key=job.output_key,
        upload=None
        if use_multipart
        else build_upload_instructions(
            content_type=request.content_type,
            size_bytes=request.size_bytes,
            input_bucket=job.input_bucket,
            input_key=job.input_key,
            s3_client=s3_client,
        ),
        multipart_upload=build_multipart_upload_instructions(
            job_id=job.job_id,
            content_type=request.content_type,
            size_bytes=request.size_bytes,
            part_size_bytes=settings.multipart_part_size_bytes,
            input_bucket=job.input_bucket,
            input_key=job.input_key,
            s3_client=s3_client,
        )
        if use_multipart
        else None,
    )


@app.post("/jobs/{job_id}/uploads/complete", response_model=JobResponse)
def complete_multipart_upload(
    job_id: str,
    request: CompleteUploadRequest,
    store: JobStore = Depends(get_store),
    s3_client: BaseClient = Depends(get_s3_client),
) -> JobResponse:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if JobStatus(job.status) != JobStatus.UPLOADING:
        # Duplicate completion call after the pipeline already moved on.
        return _job_response(job)

    try:
        s3_client.complete_multipart_upload(
            Bucket=job.input_bucket,
            Key=job.input_key,
            UploadId=request.upload_id,
            MultipartUpload={
                "Parts": [
                    {"ETag": part.etag, "PartNumber": part.part_number}
                    for part in sorted(request.parts, key=lambda part: part.part_number)
                ]
            },
        )
    except ClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not complete multipart upload: {exc.response['Error'].get('Code', 'unknown')}",
        ) from exc

    refreshed = store.get_job(job_id)
    return _job_response(refreshed or job)


@app.post("/jobs/{job_id}/uploads/abort", response_model=JobResponse)
def abort_multipart_upload(
    job_id: str,
    request: AbortUploadRequest,
    store: JobStore = Depends(get_store),
    s3_client: BaseClient = Depends(get_s3_client),
) -> JobResponse:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    try:
        s3_client.abort_multipart_upload(
            Bucket=job.input_bucket,
            Key=job.input_key,
            UploadId=request.upload_id,
        )
    except ClientError as exc:
        logger.warning(
            "abort multipart upload failed",
            extra={"job_id": job_id, "error": str(exc)},
        )

    aborted = store.transition_job_state(
        job_id,
        allowed_current_statuses=(JobStatus.UPLOADING,),
        new_status=JobStatus.FAILED,
        updated_at=utc_now(),
        last_error="Upload aborted by the client.",
    )
    return _job_response(aborted or store.get_job(job_id) or job)


@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, store: JobStore = Depends(get_store)) -> JobResponse:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    return _job_response(job)
