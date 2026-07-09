from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote_plus

import boto3
from botocore.client import BaseClient
from botocore.config import Config

from apps.api.app.models import JobStatus, utc_now
from apps.api.app.settings import Settings, get_settings
from apps.api.app.store import DynamoDbJobStore, JobStore, InMemoryJobStore
from apps.common import metrics
from apps.common.obs import default_worker_id, get_logger

logger = get_logger("elastic.worker")

DEFAULT_OUTPUT_PRESET = "1080p"
FFMPEG_VIDEO_PRESET = "medium"
FFMPEG_VIDEO_CRF = "23"
FFMPEG_VIDEO_HEIGHT = "1080"


class TranscodeError(RuntimeError):
    pass


class MessageOutcome(Enum):
    # Job is finished or the event is provably stale; consume the message.
    DELETE = "delete"
    # This attempt ended without a verdict; make the message visible now so
    # another worker retries immediately.
    RELEASE = "release"
    # Another worker actively owns the job; keep our hands off the message and
    # let the visibility timeout re-surface it later as a natural re-check.
    LEAVE = "leave"


@dataclass(frozen=True)
class WorkerContext:
    settings: Settings
    job_store: JobStore
    s3_client: BaseClient
    sqs_client: BaseClient
    queue_url: str
    shutdown_event: threading.Event = field(default_factory=threading.Event)
    worker_id: str = field(default_factory=default_worker_id)
    visibility_timeout_seconds: int = 60
    visibility_renewal_interval_seconds: int = 15
    lease_duration_seconds: int = 90
    max_attempts: int = 3


@dataclass
class MessageVisibilityLease:
    """Heartbeat for one in-flight message.

    Renews two things on the same cadence: the SQS visibility timeout (keeps
    the message hidden from other consumers) and, once a job is bound, the
    DynamoDB lease (proves to other workers that we are still alive). If the
    lease renewal is rejected the job was stolen, and `lease_lost` tells the
    processing loop to cancel work.
    """

    sqs_client: BaseClient
    queue_url: str
    receipt_handle: str
    shutdown_event: threading.Event
    visibility_timeout_seconds: int = 60
    renewal_interval_seconds: int = 15
    lease_duration_seconds: int = 90
    lease_lost: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _job_store: JobStore | None = field(default=None, init=False, repr=False)
    _job_id: str | None = field(default=None, init=False, repr=False)
    _worker_id: str | None = field(default=None, init=False, repr=False)
    _expected_attempt: int | None = field(default=None, init=False, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="elastic-attempt-lease", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.renewal_interval_seconds + 1)

    def bind_job(self, *, job_store: JobStore, job_id: str, worker_id: str, expected_attempt: int) -> None:
        self._job_store = job_store
        self._job_id = job_id
        self._worker_id = worker_id
        self._expected_attempt = expected_attempt

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
                metrics.VISIBILITY_EXTENSIONS.inc()
            except Exception as exc:
                logger.warning(
                    "failed to extend message visibility",
                    extra={"receipt_handle": self.receipt_handle[:24], "error": str(exc)},
                )
            if self._job_store is None or self._job_id is None:
                continue
            try:
                renewed = self._job_store.renew_lease(
                    self._job_id,
                    worker_id=self._worker_id or "",
                    expected_attempt=self._expected_attempt or 0,
                    now=utc_now(),
                    lease_duration_seconds=self.lease_duration_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "lease renewal errored; will retry",
                    extra={"job_id": self._job_id, "error": str(exc)},
                )
                continue
            if not renewed:
                # Someone else fenced us out; stop heartbeating and tell the
                # processing loop to abandon the attempt.
                metrics.LEASE_RENEWAL_FAILURES.inc()
                logger.warning(
                    "lost job lease to another worker; cancelling attempt",
                    extra={"job_id": self._job_id, "worker_id": self._worker_id},
                )
                self.lease_lost.set()
                return


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
        worker_id=settings.worker_id or default_worker_id(),
        lease_duration_seconds=settings.lease_duration_seconds,
        max_attempts=settings.max_attempts,
    )


def _get_queue_url(settings: Settings) -> str:
    sqs_client = build_sqs_client(settings)
    return sqs_client.get_queue_url(QueueName=settings.ingest_queue_name)["QueueUrl"]


def normalize_s3_event_message(message_body: str, *, sqs_message_id: str) -> list[dict[str, Any]]:
    payload = json.loads(message_body)

    # Synthetic requeue messages injected by the reconciler when the original
    # S3 event was lost or already consumed.
    requeue = payload.get("elastic_requeue")
    if requeue is not None:
        return [
            {
                "job_id": requeue["job_id"],
                "bucket": requeue["bucket"],
                "object_key": requeue["object_key"],
                "size_bytes": None,
                "preset": DEFAULT_OUTPUT_PRESET,
                "attempt": 1,
                "event_metadata": {
                    "sqs_message_id": sqs_message_id,
                    "s3_event_name": "elastic:Requeue",
                    "s3_sequencer": None,
                    "event_time": requeue.get("requeued_at"),
                    "requeue_reason": requeue.get("reason"),
                },
            }
        ]

    records = payload.get("Records", [])
    normalized: list[dict[str, Any]] = []
    for record in records:
        bucket = record["s3"]["bucket"]["name"]
        # S3 URL-encodes object keys in event payloads (spaces become "+").
        object_key = unquote_plus(record["s3"]["object"]["key"])
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


def handle_sqs_message(
    message: dict[str, Any],
    context: WorkerContext,
    *,
    acknowledge: bool = True,
) -> MessageOutcome:
    normalized_records = normalize_s3_event_message(message["Body"], sqs_message_id=message["MessageId"])
    lease = MessageVisibilityLease(
        sqs_client=context.sqs_client,
        queue_url=context.queue_url,
        receipt_handle=message["ReceiptHandle"],
        shutdown_event=context.shutdown_event,
        visibility_timeout_seconds=context.visibility_timeout_seconds,
        renewal_interval_seconds=context.visibility_renewal_interval_seconds,
        lease_duration_seconds=context.lease_duration_seconds,
    )
    lease.start()
    outcome = MessageOutcome.DELETE
    try:
        for event in normalized_records:
            result = process_normalized_event(event, context, lease=lease)
            if result is not MessageOutcome.DELETE:
                outcome = result
                break
    finally:
        lease.stop()

    if outcome is MessageOutcome.DELETE:
        if acknowledge:
            context.sqs_client.delete_message(
                QueueUrl=context.queue_url, ReceiptHandle=message["ReceiptHandle"]
            )
    elif outcome is MessageOutcome.RELEASE:
        try:
            lease.release_now()
        except Exception as exc:
            logger.warning("failed to release message visibility", extra={"error": str(exc)})
    return outcome


def run_worker_loop(
    *,
    context: WorkerContext | None = None,
    delete: bool = True,
    wait_time_seconds: int = 5,
) -> None:
    context = context or build_worker_context()
    logger.info("starting poll loop", extra={"worker_id": context.worker_id})
    while not context.shutdown_event.is_set():
        poll_once(delete=delete, context=context, wait_time_seconds=wait_time_seconds)


def _attempt_cancelled(context: WorkerContext, lease: MessageVisibilityLease | None) -> bool:
    if context.shutdown_event.is_set():
        return True
    return lease is not None and lease.lease_lost.is_set()


def _outcome_for_lost_job(job_store: JobStore, job_id: str) -> MessageOutcome:
    refreshed = job_store.get_job(job_id)
    if refreshed is None or JobStatus(refreshed.status) in {JobStatus.COMPLETED, JobStatus.FAILED}:
        return MessageOutcome.DELETE
    return MessageOutcome.LEAVE


def process_normalized_event(
    event: dict[str, Any],
    context: WorkerContext,
    *,
    lease: MessageVisibilityLease | None = None,
) -> MessageOutcome:
    log_fields = {
        "job_id": event["job_id"],
        "worker_id": context.worker_id,
        "sqs_message_id": event["event_metadata"].get("sqs_message_id"),
    }
    job = context.job_store.get_job(event["job_id"])
    if job is None:
        logger.warning("missing job for event; dropping message", extra=log_fields)
        return MessageOutcome.DELETE

    if job.input_bucket != event["bucket"] or job.input_key != event["object_key"]:
        logger.warning("event key mismatch; dropping message", extra=log_fields)
        return MessageOutcome.DELETE

    job_status = JobStatus(job.status)
    if job_status in {JobStatus.COMPLETED, JobStatus.FAILED}:
        logger.info("job already terminal; dropping duplicate event", extra=log_fields)
        return MessageOutcome.DELETE

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
                return MessageOutcome.DELETE
            job = refreshed
        else:
            job = queued_job
        job_status = JobStatus(job.status)

    now = utc_now()
    if job_status == JobStatus.PROCESSING and job.lease_expires_at is not None and job.lease_expires_at > now:
        logger.info(
            "job actively leased by another worker; leaving message alone",
            extra={**log_fields, "lease_owner": job.lease_owner},
        )
        return MessageOutcome.LEAVE

    if context.shutdown_event.is_set():
        # Not claimed yet, so there is nothing to unwind: hand the message
        # straight back for another worker.
        logger.info("shutdown requested before claim; releasing message", extra=log_fields)
        return MessageOutcome.RELEASE

    lease_was_expired = job_status == JobStatus.PROCESSING
    claimed_job = context.job_store.claim_job(
        job.job_id,
        worker_id=context.worker_id,
        now=now,
        lease_duration_seconds=context.lease_duration_seconds,
    )
    if claimed_job is None:
        logger.info("lost claim race for job", extra=log_fields)
        return _outcome_for_lost_job(context.job_store, job.job_id)

    metrics.LEASE_CLAIMS.inc()
    if lease_was_expired:
        metrics.LEASE_STEALS.inc()
        logger.warning(
            "stole job from expired lease",
            extra={**log_fields, "previous_owner": job.lease_owner, "attempt": claimed_job.attempt_count},
        )

    if lease is not None:
        lease.bind_job(
            job_store=context.job_store,
            job_id=job.job_id,
            worker_id=context.worker_id,
            expected_attempt=claimed_job.attempt_count,
        )
    attempt_fields = {**log_fields, "attempt": claimed_job.attempt_count}
    logger.info("claimed job", extra=attempt_fields)

    try:
        with tempfile.TemporaryDirectory(prefix=f"elastic-{job.job_id}-") as tmpdir:
            temp_dir = Path(tmpdir)
            source_path = temp_dir / "source"
            output_path = temp_dir / "output.mp4"
            _download_source(context.s3_client, claimed_job.input_bucket, claimed_job.input_key, source_path)
            if _attempt_cancelled(context, lease):
                raise InterruptedError("Attempt cancelled before transcode could start.")
            cancel_events = [context.shutdown_event]
            if lease is not None:
                cancel_events.append(lease.lease_lost)
            _process_media(source_path, output_path, cancel_events=cancel_events)
            if _attempt_cancelled(context, lease):
                raise InterruptedError("Attempt cancelled before output upload could complete.")
            _upload_output(context.s3_client, claimed_job.input_bucket, claimed_job.output_key, output_path)
            if _attempt_cancelled(context, lease):
                raise InterruptedError("Attempt cancelled before job finalization.")

        # Fenced finalization: only the attempt that claimed the job may
        # complete it. A zombie past its lease fails this condition.
        completed_job = context.job_store.transition_job_state(
            job.job_id,
            allowed_current_statuses=(JobStatus.PROCESSING,),
            new_status=JobStatus.COMPLETED,
            updated_at=utc_now(),
            last_error=None,
            expected_attempt=claimed_job.attempt_count,
        )
        if completed_job is None:
            logger.warning("fencing rejected completion; job was taken over", extra=attempt_fields)
            return _outcome_for_lost_job(context.job_store, job.job_id)
        metrics.JOBS_COMPLETED.inc()
        logger.info("completed job", extra=attempt_fields)
        return MessageOutcome.DELETE
    except InterruptedError as exc:
        interrupted_job = context.job_store.transition_job_state(
            job.job_id,
            allowed_current_statuses=(JobStatus.PROCESSING,),
            new_status=JobStatus.INTERRUPTED,
            updated_at=utc_now(),
            last_error=str(exc),
            expected_attempt=claimed_job.attempt_count,
        )
        metrics.JOBS_INTERRUPTED.inc()
        if interrupted_job is None:
            logger.warning(
                "could not mark job interrupted; attempt was fenced out",
                extra={**attempt_fields, "error": str(exc)},
            )
            return _outcome_for_lost_job(context.job_store, job.job_id)
        logger.info("interrupted job", extra={**attempt_fields, "error": str(exc)})
        return MessageOutcome.RELEASE
    except Exception as exc:
        if claimed_job.attempt_count >= context.max_attempts:
            failed_job = context.job_store.transition_job_state(
                job.job_id,
                allowed_current_statuses=(JobStatus.PROCESSING,),
                new_status=JobStatus.FAILED,
                updated_at=utc_now(),
                last_error=str(exc),
                expected_attempt=claimed_job.attempt_count,
            )
            metrics.JOBS_FAILED.inc()
            if failed_job is None:
                logger.warning("could not mark job failed; attempt was fenced out", extra=attempt_fields)
                return _outcome_for_lost_job(context.job_store, job.job_id)
            logger.error(
                "failed job after exhausting attempts",
                extra={**attempt_fields, "error": str(exc), "max_attempts": context.max_attempts},
            )
            return MessageOutcome.DELETE

        retried_job = context.job_store.transition_job_state(
            job.job_id,
            allowed_current_statuses=(JobStatus.PROCESSING,),
            new_status=JobStatus.INTERRUPTED,
            updated_at=utc_now(),
            last_error=str(exc),
            expected_attempt=claimed_job.attempt_count,
        )
        metrics.JOBS_INTERRUPTED.inc()
        if retried_job is None:
            logger.warning("could not mark job for retry; attempt was fenced out", extra=attempt_fields)
            return _outcome_for_lost_job(context.job_store, job.job_id)
        logger.warning(
            "attempt failed; releasing for retry",
            extra={**attempt_fields, "error": str(exc), "max_attempts": context.max_attempts},
        )
        return MessageOutcome.RELEASE


def _download_source(s3_client: BaseClient, bucket: str, key: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Managed transfer: streams to disk in chunks (multipart for large
    # objects) instead of buffering the whole video in memory.
    s3_client.download_file(bucket, key, str(destination))


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


def _process_media(
    source_path: Path,
    output_path: Path,
    *,
    cancel_events: list[threading.Event] | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        raise TranscodeError("ffmpeg is not installed in the worker environment.")

    logger.info("transcoding with ffmpeg", extra={"source": source_path.name})
    started_at = time.monotonic()
    process = subprocess.Popen(
        _build_ffmpeg_command(source_path, output_path, ffmpeg_path=ffmpeg_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        while True:
            if cancel_events and any(event.is_set() for event in cancel_events):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise InterruptedError("Attempt cancelled during ffmpeg transcode.")

            return_code = process.poll()
            if return_code is not None:
                _stdout, stderr = process.communicate()
                if return_code != 0:
                    tail = (stderr or "").strip().splitlines()[-3:]
                    raise TranscodeError(f"ffmpeg exited with {return_code}: {' | '.join(tail)}")
                metrics.TRANSCODE_SECONDS.observe(time.monotonic() - started_at)
                return

            time.sleep(0.5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _upload_output(s3_client: BaseClient, bucket: str, key: str, source_path: Path) -> None:
    # Managed transfer handles multipart chunking for large outputs.
    s3_client.upload_file(str(source_path), bucket, key, ExtraArgs={"ContentType": "video/mp4"})


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
        return []

    message = messages[0]
    normalized = normalize_s3_event_message(message["Body"], sqs_message_id=message["MessageId"])
    handle_sqs_message(message, context, acknowledge=delete)
    return normalized
