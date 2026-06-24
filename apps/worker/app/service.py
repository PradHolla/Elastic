from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any, Optional

import boto3
from botocore.client import BaseClient
from botocore.config import Config

from apps.api.app.models import JobStatus, utc_now
from apps.api.app.settings import Settings, get_settings
from apps.api.app.store import DynamoDbJobStore, JobStore, InMemoryJobStore

DEFAULT_OUTPUT_PRESET = "1080p"
FFMPEG_VIDEO_PRESET = "medium"
FFMPEG_VIDEO_CRF = "23"
FFMPEG_VIDEO_HEIGHT = "1080"


@dataclass(frozen=True)
class WorkerContext:
    settings: Settings
    job_store: JobStore
    s3_client: BaseClient
    sqs_client: BaseClient
    queue_url: str
    shutdown_event: threading.Event = field(default_factory=threading.Event)
    visibility_timeout_seconds: int = 60
    visibility_renewal_interval_seconds: int = 15


@dataclass
class MessageVisibilityLease:
    sqs_client: BaseClient
    queue_url: str
    receipt_handle: str
    shutdown_event: threading.Event
    visibility_timeout_seconds: int = 60
    renewal_interval_seconds: int = 15
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="elastic-sqs-lease", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.renewal_interval_seconds + 1)

    def extend_once(self) -> None:
        self.sqs_client.change_message_visibility(
            QueueUrl=self.queue_url,
            ReceiptHandle=self.receipt_handle,
            VisibilityTimeout=self.visibility_timeout_seconds,
        )

    def release_now(self) -> None:
        self.sqs_client.change_message_visibility(
            QueueUrl=self.queue_url,
            ReceiptHandle=self.receipt_handle,
            VisibilityTimeout=0,
        )

    def _run(self) -> None:
        while not self._stop_event.wait(self.renewal_interval_seconds):
            if self.shutdown_event.is_set():
                return
            try:
                self.extend_once()
            except Exception as exc:
                print(f"[worker] failed to extend visibility for {self.receipt_handle}: {exc}")


def build_s3_client(settings: Settings) -> BaseClient:
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def build_sqs_client(settings: Settings) -> BaseClient:
    return boto3.client(
        "sqs",
        region_name=settings.aws_region,
        endpoint_url=settings.aws_endpoint_url,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


def build_job_store(settings: Settings) -> JobStore:
    if settings.store_backend == "dynamodb":
        client = boto3.client(
            "dynamodb",
            region_name=settings.aws_region,
            endpoint_url=settings.aws_endpoint_url,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
        return DynamoDbJobStore(table_name=settings.jobs_table_name, client=client)
    return InMemoryJobStore()


def build_worker_context(settings: Optional[Settings] = None) -> WorkerContext:
    settings = settings or get_settings()
    return WorkerContext(
        settings=settings,
        job_store=build_job_store(settings),
        s3_client=build_s3_client(settings),
        sqs_client=build_sqs_client(settings),
        queue_url=_get_queue_url(settings),
    )


def _get_queue_url(settings: Settings) -> str:
    sqs_client = build_sqs_client(settings)
    return sqs_client.get_queue_url(QueueName=settings.ingest_queue_name)["QueueUrl"]


def normalize_s3_event_message(message_body: str, *, sqs_message_id: str) -> list[dict[str, Any]]:
    payload = json.loads(message_body)
    records = payload.get("Records", [])
    normalized: list[dict[str, Any]] = []
    for record in records:
        bucket = record["s3"]["bucket"]["name"]
        object_key = record["s3"]["object"]["key"]
        key_parts = object_key.split("/")
        job_id = key_parts[1] if len(key_parts) >= 3 and key_parts[0] == "inputs" else "unknown"
        normalized.append(
            {
                "job_id": job_id,
                "bucket": bucket,
                "object_key": object_key,
                "size_bytes": record["s3"]["object"].get("size"),
                "preset": DEFAULT_OUTPUT_PRESET,
                "attempt": 1,
                "event_metadata": {
                    "sqs_message_id": sqs_message_id,
                    "s3_event_name": record.get("eventName"),
                    "s3_sequencer": record["s3"]["object"].get("sequencer"),
                    "event_time": record.get("eventTime"),
                },
            }
        )
    return normalized


def handle_sqs_message(message: dict[str, Any], context: WorkerContext, *, acknowledge: bool = True) -> bool:
    normalized_records = normalize_s3_event_message(message["Body"], sqs_message_id=message["MessageId"])
    lease = MessageVisibilityLease(
        sqs_client=context.sqs_client,
        queue_url=context.queue_url,
        receipt_handle=message["ReceiptHandle"],
        shutdown_event=context.shutdown_event,
        visibility_timeout_seconds=context.visibility_timeout_seconds,
        renewal_interval_seconds=context.visibility_renewal_interval_seconds,
    )
    lease.start()
    should_delete = True
    try:
        for event in normalized_records:
            handled = process_normalized_event(event, context, lease=lease)
            should_delete = should_delete and handled
            if not handled:
                break
    finally:
        lease.stop()
    if acknowledge and should_delete:
        context.sqs_client.delete_message(QueueUrl=context.queue_url, ReceiptHandle=message["ReceiptHandle"])
    return should_delete


def run_worker_loop(
    *,
    context: WorkerContext | None = None,
    delete: bool = True,
    wait_time_seconds: int = 5,
) -> None:
    context = context or build_worker_context()
    print("[worker] starting poll loop")
    while not context.shutdown_event.is_set():
        poll_once(delete=delete, context=context, wait_time_seconds=wait_time_seconds)


def process_normalized_event(
    event: dict[str, Any],
    context: WorkerContext,
    *,
    lease: MessageVisibilityLease | None = None,
) -> bool:
    job = context.job_store.get_job(event["job_id"])
    if job is None:
        print(f"[worker] missing job {event['job_id']}; skipping")
        return True

    if job.input_bucket != event["bucket"] or job.input_key != event["object_key"]:
        print(f"[worker] event key mismatch for job {job.job_id}; skipping")
        return True

    job_status = JobStatus(job.status)
    if job_status in {JobStatus.COMPLETED, JobStatus.FAILED}:
        print(f"[worker] job {job.job_id} already terminal; skipping")
        return True

    if job_status in {JobStatus.UPLOADING, JobStatus.INTERRUPTED}:
        queued_job = context.job_store.transition_job_state(
            job.job_id,
            allowed_current_statuses=(JobStatus.UPLOADING, JobStatus.INTERRUPTED),
            new_status=JobStatus.QUEUED,
            updated_at=utc_now(),
            last_error=None,
        )
        if queued_job is None:
            refreshed = context.job_store.get_job(job.job_id)
            if refreshed is None:
                return True
            job = refreshed
        else:
            job = queued_job
        job_status = JobStatus(job.status)

    if job_status == JobStatus.PROCESSING:
        print(f"[worker] job {job.job_id} already processing; skipping duplicate event")
        return True

    if context.shutdown_event.is_set():
        interrupted_job = context.job_store.transition_job_state(
            job.job_id,
            allowed_current_statuses=(JobStatus.QUEUED,),
            new_status=JobStatus.INTERRUPTED,
            updated_at=utc_now(),
            last_error="Worker shutdown requested before processing could start.",
        )
        if interrupted_job is None:
            print(f"[worker] could not mark job {job.job_id} interrupted before shutdown.")
        else:
            print(f"[worker] interrupted job {job.job_id} before processing started")
        if lease is not None:
            lease.stop()
            try:
                lease.release_now()
            except Exception as exc:
                print(f"[worker] failed to release message visibility for {job.job_id}: {exc}")
        return False

    claimed_job = context.job_store.transition_job_state(
        job.job_id,
        allowed_current_statuses=(JobStatus.QUEUED,),
        new_status=JobStatus.PROCESSING,
        updated_at=utc_now(),
        attempt_count_delta=1,
        last_error=None,
    )
    if claimed_job is None:
        print(f"[worker] could not claim job {job.job_id}; skipping")
        return True

    try:
        with tempfile.TemporaryDirectory(prefix=f"elastic-{job.job_id}-") as tmpdir:
            temp_dir = Path(tmpdir)
            source_path = temp_dir / "source"
            output_path = temp_dir / "output.mp4"
            _download_source(context.s3_client, claimed_job.input_bucket, claimed_job.input_key, source_path)
            if context.shutdown_event.is_set():
                raise InterruptedError("Worker shutdown requested before transcode could start.")
            _process_media(source_path, output_path, shutdown_event=context.shutdown_event)
            if context.shutdown_event.is_set():
                raise InterruptedError("Worker shutdown requested before output upload could complete.")
            _upload_output(context.s3_client, claimed_job.input_bucket, claimed_job.output_key, output_path)
            if context.shutdown_event.is_set():
                raise InterruptedError("Worker shutdown requested before job finalization.")

        completed_job = context.job_store.transition_job_state(
            job.job_id,
            allowed_current_statuses=(JobStatus.PROCESSING,),
            new_status=JobStatus.COMPLETED,
            updated_at=utc_now(),
            last_error=None,
        )
        if completed_job is None:
            raise RuntimeError(f"Could not finalize job {job.job_id}.")
        print(f"[worker] completed job {job.job_id}")
        return True
    except InterruptedError as exc:
        interrupted_job = context.job_store.transition_job_state(
            job.job_id,
            allowed_current_statuses=(JobStatus.PROCESSING,),
            new_status=JobStatus.INTERRUPTED,
            updated_at=utc_now(),
            last_error=str(exc),
        )
        if interrupted_job is None:
            print(f"[worker] failed to mark job {job.job_id} interrupted: {exc}")
        else:
            print(f"[worker] interrupted job {job.job_id}: {exc}")
        if lease is not None:
            lease.stop()
            try:
                lease.release_now()
            except Exception as exc:
                print(f"[worker] failed to release message visibility for {job.job_id}: {exc}")
        return False
    except Exception as exc:
        failed_job = context.job_store.transition_job_state(
            job.job_id,
            allowed_current_statuses=(JobStatus.PROCESSING,),
            new_status=JobStatus.FAILED,
            updated_at=utc_now(),
            last_error=str(exc),
        )
        if failed_job is None:
            print(f"[worker] failed to mark job {job.job_id} failed: {exc}")
        else:
            print(f"[worker] failed job {job.job_id}: {exc}")
        return True


def _download_source(s3_client: BaseClient, bucket: str, key: str, destination: Path) -> None:
    response = s3_client.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()
    destination.write_bytes(body)


def _build_ffmpeg_command(source_path: Path, output_path: Path, *, ffmpeg_path: str = "ffmpeg") -> list[str]:
    return [
        ffmpeg_path,
        "-y",
        "-i",
        str(source_path),
        "-vf",
        f"scale=-2:{FFMPEG_VIDEO_HEIGHT}:flags=lanczos,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        FFMPEG_VIDEO_PRESET,
        "-crf",
        FFMPEG_VIDEO_CRF,
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        "-pix_fmt",
        "yuv420p",
        str(output_path),
    ]


def _process_media(source_path: Path, output_path: Path, *, shutdown_event: threading.Event | None = None) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        print("[worker] ffmpeg not available, falling back to copy")
        shutil.copyfile(source_path, output_path)
        return

    print(f"[worker] transcoding {source_path.name} with ffmpeg")
    process = subprocess.Popen(
        _build_ffmpeg_command(source_path, output_path, ffmpeg_path=ffmpeg_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise InterruptedError("Worker shutdown requested during ffmpeg transcode.")

            return_code = process.poll()
            if return_code is not None:
                _stdout, stderr = process.communicate()
                if return_code != 0:
                    print(f"[worker] ffmpeg failed, falling back to copy: {stderr.strip() or return_code}")
                    shutil.copyfile(source_path, output_path)
                return

            time.sleep(0.5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _upload_output(s3_client: BaseClient, bucket: str, key: str, source_path: Path) -> None:
    s3_client.put_object(Bucket=bucket, Key=key, Body=source_path.read_bytes())


def poll_once(
    *,
    delete: bool = False,
    context: WorkerContext | None = None,
    wait_time_seconds: int = 1,
) -> list[dict[str, Any]]:
    context = context or build_worker_context()
    response = context.sqs_client.receive_message(
        QueueUrl=context.queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=wait_time_seconds,
    )
    messages = response.get("Messages", [])
    if not messages:
        print("No messages available.")
        return []

    message = messages[0]
    normalized = normalize_s3_event_message(message["Body"], sqs_message_id=message["MessageId"])
    print(json.dumps(normalized, indent=2))
    handle_sqs_message(message, context, acknowledge=delete)
    return normalized
