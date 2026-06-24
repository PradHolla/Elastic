import io
import json
from pathlib import Path
import threading

import pytest

from apps.api.app.models import JobRecord, JobStatus, utc_now
from apps.api.app.settings import Settings
from apps.api.app.store import InMemoryJobStore
from apps.worker.app.service import (
    MessageVisibilityLease,
    WorkerContext,
    _build_ffmpeg_command,
    handle_sqs_message,
    normalize_s3_event_message,
)


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, io.BytesIO]:
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict[str, str]:
        self.objects[(Bucket, Key)] = Body
        return {"ETag": "fake"}


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


def test_normalize_s3_event_message() -> None:
    body = json.dumps(
        {
            "Records": [
                {
                    "eventTime": "2026-05-25T12:00:00.000Z",
                    "eventName": "ObjectCreated:Put",
                    "s3": {
                        "bucket": {"name": "elastic-inputs"},
                        "object": {
                            "key": "inputs/job-123/source",
                            "size": 123,
                            "sequencer": "abc123",
                        },
                    },
                }
            ]
        }
    )

    normalized = normalize_s3_event_message(body, sqs_message_id="message-1")

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


def test_handle_sqs_message_processes_and_completes_job() -> None:
    store = InMemoryJobStore()
    timestamp = utc_now()
    job = JobRecord(
        job_id="job-123",
        status=JobStatus.UPLOADING,
        preset="1080p",
        input_bucket="elastic-inputs",
        input_key="inputs/job-123/source",
        output_key="outputs/job-123/final/1080p.mp4",
        created_at=timestamp,
        updated_at=timestamp,
    )
    store.create_job(job)

    s3_client = FakeS3Client()
    s3_client.objects[("elastic-inputs", "inputs/job-123/source")] = b"elastic test payload"
    sqs_client = FakeSqsClient()
    context = WorkerContext(
        settings=Settings(store_backend="memory"),
        job_store=store,
        s3_client=s3_client,
        sqs_client=sqs_client,
        queue_url="http://localhost:4566/000000000000/elastic-ingest",
    )
    message = {
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
                            "object": {
                                "key": "inputs/job-123/source",
                                "size": 123,
                                "sequencer": "abc123",
                            },
                        },
                    }
                ]
            }
        ),
    }

    handled = handle_sqs_message(message, context, acknowledge=True)

    updated_job = store.get_job("job-123")

    assert handled is True
    assert updated_job is not None
    assert updated_job.status == JobStatus.COMPLETED
    assert updated_job.attempt_count == 1
    assert updated_job.last_error is None
    assert s3_client.objects[("elastic-inputs", "outputs/job-123/final/1080p.mp4")] == b"elastic test payload"
    assert sqs_client.deleted_messages == [("http://localhost:4566/000000000000/elastic-ingest", "receipt-1")]


def test_visibility_lease_extends_visibility_once() -> None:
    sqs_client = FakeSqsClient()
    lease = MessageVisibilityLease(
        sqs_client=sqs_client,
        queue_url="http://localhost:4566/000000000000/elastic-ingest",
        receipt_handle="receipt-1",
        shutdown_event=threading.Event(),
        visibility_timeout_seconds=42,
        renewal_interval_seconds=1,
    )

    lease.extend_once()

    assert sqs_client.visibility_changes == [
        ("http://localhost:4566/000000000000/elastic-ingest", "receipt-1", 42)
    ]


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
    timestamp = utc_now()
    job = JobRecord(
        job_id="job-123",
        status=JobStatus.UPLOADING,
        preset="1080p",
        input_bucket="elastic-inputs",
        input_key="inputs/job-123/source",
        output_key="outputs/job-123/final/1080p.mp4",
        created_at=timestamp,
        updated_at=timestamp,
    )
    store.create_job(job)

    s3_client = FakeS3Client()
    s3_client.objects[("elastic-inputs", "inputs/job-123/source")] = b"elastic test payload"
    sqs_client = FakeSqsClient()
    context = WorkerContext(
        settings=Settings(store_backend="memory"),
        job_store=store,
        s3_client=s3_client,
        sqs_client=sqs_client,
        queue_url="http://localhost:4566/000000000000/elastic-ingest",
        shutdown_event=threading.Event(),
        visibility_timeout_seconds=5,
        visibility_renewal_interval_seconds=1,
    )

    def interrupted_process_media(source_path, output_path, *, shutdown_event=None) -> None:
        raise InterruptedError("Worker shutdown requested during ffmpeg transcode.")

    monkeypatch.setattr("apps.worker.app.service._process_media", interrupted_process_media)

    message = {
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
                            "object": {
                                "key": "inputs/job-123/source",
                                "size": 123,
                                "sequencer": "abc123",
                            },
                        },
                    }
                ]
            }
        ),
    }

    handled = handle_sqs_message(message, context, acknowledge=True)

    updated_job = store.get_job("job-123")

    assert handled is False
    assert updated_job is not None
    assert updated_job.status == JobStatus.INTERRUPTED
    assert updated_job.last_error == "Worker shutdown requested during ffmpeg transcode."
    assert sqs_client.deleted_messages == []
    assert (
        "http://localhost:4566/000000000000/elastic-ingest",
        "receipt-1",
        0,
    ) in sqs_client.visibility_changes
