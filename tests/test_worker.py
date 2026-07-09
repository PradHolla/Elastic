import json
import shutil
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from apps.api.app.models import JobRecord, JobStatus, utc_now
from apps.api.app.settings import Settings
from apps.api.app.store import InMemoryJobStore
from apps.worker.app.service import (
    MessageOutcome,
    MessageVisibilityLease,
    WorkerContext,
    _build_ffmpeg_command,
    handle_sqs_message,
    normalize_s3_event_message,
)

QUEUE_URL = "http://localhost:4566/000000000000/elastic-ingest"


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        Path(filename).write_bytes(self.objects[(bucket, key)])

    def upload_file(self, filename: str, bucket: str, key: str, ExtraArgs: dict | None = None) -> None:
        self.objects[(bucket, key)] = Path(filename).read_bytes()


class FakeSqsClient:
    def __init__(self) -> None:
        self.deleted_messages: list[tuple[str, str]] = []
        self.visibility_changes: list[tuple[str, str, int]] = []

    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> dict[str, str]:
        self.deleted_messages.append((QueueUrl, ReceiptHandle))
        return {}

    def change_message_visibility(
        self,
        *,
        QueueUrl: str,
        ReceiptHandle: str,
        VisibilityTimeout: int,
    ) -> dict[str, str]:
        self.visibility_changes.append((QueueUrl, ReceiptHandle, VisibilityTimeout))
        return {}


def build_job(**overrides) -> JobRecord:
    timestamp = utc_now()
    defaults = dict(
        job_id="job-123",
        status=JobStatus.UPLOADING,
        preset="1080p",
        input_bucket="elastic-inputs",
        input_key="inputs/job-123/source",
        output_key="outputs/job-123/final/1080p.mp4",
        created_at=timestamp,
        updated_at=timestamp,
    )
    defaults.update(overrides)
    return JobRecord(**defaults)


def build_context(store: InMemoryJobStore, s3_client: FakeS3Client, sqs_client: FakeSqsClient) -> WorkerContext:
    return WorkerContext(
        settings=Settings(store_backend="memory"),
        job_store=store,
        s3_client=s3_client,
        sqs_client=sqs_client,
        queue_url=QUEUE_URL,
        worker_id="test-worker",
    )


def build_s3_event_message(key: str = "inputs/job-123/source") -> dict:
    return {
        "MessageId": "message-1",
        "ReceiptHandle": "receipt-1",
        "Body": json.dumps(
            {
                "Records": [
                    {
                        "eventTime": "2026-05-25T12:00:00.000Z",
                        "eventName": "ObjectCreated:Put",
                        "s3": {
                            "bucket": {"name": "elastic-inputs"},
                            "object": {"key": key, "size": 123, "sequencer": "abc123"},
                        },
                    }
                ]
            }
        ),
    }


def fake_transcode(source_path, output_path, *, cancel_events=None) -> None:
    shutil.copyfile(source_path, output_path)


def test_normalize_s3_event_message() -> None:
    normalized = normalize_s3_event_message(
        build_s3_event_message()["Body"], sqs_message_id="message-1"
    )

    assert normalized == [
        {
            "job_id": "job-123",
            "bucket": "elastic-inputs",
            "object_key": "inputs/job-123/source",
            "size_bytes": 123,
            "preset": "1080p",
            "attempt": 1,
            "event_metadata": {
                "sqs_message_id": "message-1",
                "s3_event_name": "ObjectCreated:Put",
                "s3_sequencer": "abc123",
                "event_time": "2026-05-25T12:00:00.000Z",
            },
        }
    ]


def test_normalize_url_decodes_object_keys() -> None:
    # S3 URL-encodes keys in event payloads; spaces arrive as "+".
    normalized = normalize_s3_event_message(
        build_s3_event_message(key="inputs/job-123/my+source%20video")["Body"],
        sqs_message_id="message-1",
    )

    assert normalized[0]["object_key"] == "inputs/job-123/my source video"
    assert normalized[0]["job_id"] == "job-123"


def test_normalize_handles_reconciler_requeue_message() -> None:
    body = json.dumps(
        {
            "elastic_requeue": {
                "job_id": "job-9",
                "bucket": "elastic-inputs",
                "object_key": "inputs/job-9/source",
                "reason": "lease-expired",
                "requeued_at": "2026-07-06T00:00:00+00:00",
            }
        }
    )

    normalized = normalize_s3_event_message(body, sqs_message_id="message-2")

    assert normalized[0]["job_id"] == "job-9"
    assert normalized[0]["event_metadata"]["s3_event_name"] == "elastic:Requeue"
    assert normalized[0]["event_metadata"]["requeue_reason"] == "lease-expired"


def test_handle_sqs_message_processes_and_completes_job(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryJobStore()
    store.create_job(build_job())
    s3_client = FakeS3Client()
    s3_client.objects[("elastic-inputs", "inputs/job-123/source")] = b"elastic test payload"
    sqs_client = FakeSqsClient()
    context = build_context(store, s3_client, sqs_client)
    monkeypatch.setattr("apps.worker.app.service._process_media", fake_transcode)

    outcome = handle_sqs_message(build_s3_event_message(), context, acknowledge=True)

    updated_job = store.get_job("job-123")
    assert outcome is MessageOutcome.DELETE
    assert updated_job is not None
    assert updated_job.status == JobStatus.COMPLETED
    assert updated_job.attempt_count == 1
    assert updated_job.last_error is None
    assert updated_job.lease_owner is None
    assert s3_client.objects[("elastic-inputs", "outputs/job-123/final/1080p.mp4")] == b"elastic test payload"
    assert sqs_client.deleted_messages == [(QUEUE_URL, "receipt-1")]


def test_visibility_lease_extends_visibility_once() -> None:
    sqs_client = FakeSqsClient()
    lease = MessageVisibilityLease(
        sqs_client=sqs_client,
        queue_url=QUEUE_URL,
        receipt_handle="receipt-1",
        shutdown_event=threading.Event(),
        visibility_timeout_seconds=42,
        renewal_interval_seconds=1,
    )

    lease.extend_once()

    assert sqs_client.visibility_changes == [(QUEUE_URL, "receipt-1", 42)]


def test_build_ffmpeg_command_uses_production_shaped_profile() -> None:
    command = _build_ffmpeg_command(Path("/tmp/input.mp4"), Path("/tmp/output.mp4"))

    assert command == [
        "ffmpeg",
        "-y",
        "-i",
        "/tmp/input.mp4",
        "-vf",
        "scale=-2:1080:flags=lanczos,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        "-pix_fmt",
        "yuv420p",
        "/tmp/output.mp4",
    ]


def test_handle_sqs_message_interrupts_job_and_releases_message(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryJobStore()
    store.create_job(build_job())
    s3_client = FakeS3Client()
    s3_client.objects[("elastic-inputs", "inputs/job-123/source")] = b"elastic test payload"
    sqs_client = FakeSqsClient()
    context = build_context(store, s3_client, sqs_client)

    def interrupted_process_media(source_path, output_path, *, cancel_events=None) -> None:
        raise InterruptedError("Worker shutdown requested during ffmpeg transcode.")

    monkeypatch.setattr("apps.worker.app.service._process_media", interrupted_process_media)

    outcome = handle_sqs_message(build_s3_event_message(), context, acknowledge=True)

    updated_job = store.get_job("job-123")
    assert outcome is MessageOutcome.RELEASE
    assert updated_job is not None
    assert updated_job.status == JobStatus.INTERRUPTED
    assert updated_job.last_error == "Worker shutdown requested during ffmpeg transcode."
    assert updated_job.lease_owner is None
    assert sqs_client.deleted_messages == []
    assert (QUEUE_URL, "receipt-1", 0) in sqs_client.visibility_changes


def test_duplicate_delivery_leaves_actively_leased_job_alone() -> None:
    store = InMemoryJobStore()
    store.create_job(
        build_job(
            status=JobStatus.PROCESSING,
            attempt_count=1,
            lease_owner="other-worker",
            lease_expires_at=utc_now() + timedelta(seconds=300),
        )
    )
    sqs_client = FakeSqsClient()
    context = build_context(store, FakeS3Client(), sqs_client)

    outcome = handle_sqs_message(build_s3_event_message(), context, acknowledge=True)

    job = store.get_job("job-123")
    assert outcome is MessageOutcome.LEAVE
    # The message must NOT be deleted: it is the crashed-owner recovery path.
    assert sqs_client.deleted_messages == []
    # And must not be released either, or duplicates would hot-loop.
    assert (QUEUE_URL, "receipt-1", 0) not in sqs_client.visibility_changes
    assert job is not None
    assert job.status == JobStatus.PROCESSING
    assert job.lease_owner == "other-worker"
    assert job.attempt_count == 1


def test_worker_steals_job_with_expired_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryJobStore()
    store.create_job(
        build_job(
            status=JobStatus.PROCESSING,
            attempt_count=1,
            lease_owner="dead-worker",
            lease_expires_at=utc_now() - timedelta(seconds=60),
        )
    )
    s3_client = FakeS3Client()
    s3_client.objects[("elastic-inputs", "inputs/job-123/source")] = b"payload"
    sqs_client = FakeSqsClient()
    context = build_context(store, s3_client, sqs_client)
    monkeypatch.setattr("apps.worker.app.service._process_media", fake_transcode)

    outcome = handle_sqs_message(build_s3_event_message(), context, acknowledge=True)

    job = store.get_job("job-123")
    assert outcome is MessageOutcome.DELETE
    assert job is not None
    assert job.status == JobStatus.COMPLETED
    # The steal advanced the fencing token.
    assert job.attempt_count == 2
    assert sqs_client.deleted_messages == [(QUEUE_URL, "receipt-1")]


def test_transient_failure_is_retried_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryJobStore()
    store.create_job(build_job())
    s3_client = FakeS3Client()
    s3_client.objects[("elastic-inputs", "inputs/job-123/source")] = b"payload"
    sqs_client = FakeSqsClient()
    context = build_context(store, s3_client, sqs_client)

    def broken_process_media(source_path, output_path, *, cancel_events=None) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("apps.worker.app.service._process_media", broken_process_media)

    outcome = handle_sqs_message(build_s3_event_message(), context, acknowledge=True)

    job = store.get_job("job-123")
    assert outcome is MessageOutcome.RELEASE
    assert job is not None
    assert job.status == JobStatus.INTERRUPTED
    assert job.last_error == "boom"
    assert sqs_client.deleted_messages == []


def test_failure_on_final_attempt_marks_job_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryJobStore()
    # Two attempts already burned; the claim makes it three (== max_attempts).
    store.create_job(build_job(status=JobStatus.QUEUED, attempt_count=2))
    s3_client = FakeS3Client()
    s3_client.objects[("elastic-inputs", "inputs/job-123/source")] = b"payload"
    sqs_client = FakeSqsClient()
    context = build_context(store, s3_client, sqs_client)

    def broken_process_media(source_path, output_path, *, cancel_events=None) -> None:
        raise RuntimeError("ffmpeg exited with 1")

    monkeypatch.setattr("apps.worker.app.service._process_media", broken_process_media)

    outcome = handle_sqs_message(build_s3_event_message(), context, acknowledge=True)

    job = store.get_job("job-123")
    assert outcome is MessageOutcome.DELETE
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert job.attempt_count == 3
    assert sqs_client.deleted_messages == [(QUEUE_URL, "receipt-1")]
