import json
from datetime import timedelta

from botocore.exceptions import ClientError

from apps.api.app.models import JobRecord, JobStatus, utc_now
from apps.api.app.reconciler import Reconciler
from apps.api.app.settings import Settings
from apps.api.app.store import InMemoryJobStore

QUEUE_URL = "http://localhost:4566/000000000000/elastic-ingest"


class FakeS3Client:
    def __init__(self, existing_keys: set[tuple[str, str]] | None = None) -> None:
        self.existing_keys = existing_keys or set()

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        if (Bucket, Key) in self.existing_keys:
            return {"ContentLength": 1}
        raise ClientError(
            {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadObject",
        )


class FakeSqsClient:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    def send_message(self, *, QueueUrl: str, MessageBody: str) -> dict:
        self.sent_messages.append({"queue_url": QueueUrl, "body": json.loads(MessageBody)})
        return {}


def build_job(**overrides) -> JobRecord:
    timestamp = utc_now()
    defaults = dict(
        job_id="job-1",
        status=JobStatus.UPLOADING,
        preset="1080p",
        input_bucket="elastic-inputs",
        input_key="inputs/job-1/source",
        output_key="outputs/job-1/final/1080p.mp4",
        created_at=timestamp,
        updated_at=timestamp,
    )
    defaults.update(overrides)
    return JobRecord(**defaults)


def build_reconciler(store: InMemoryJobStore, s3_client: FakeS3Client, sqs_client: FakeSqsClient) -> Reconciler:
    return Reconciler(
        settings=Settings(
            store_backend="memory",
            stale_uploading_seconds=900,
            stale_queued_seconds=300,
            upload_expiry_seconds=86400,
            lease_duration_seconds=90,
        ),
        job_store=store,
        s3_client=s3_client,
        sqs_client=sqs_client,
        queue_url=QUEUE_URL,
    )


def test_requeues_uploading_job_whose_event_was_lost() -> None:
    store = InMemoryJobStore()
    old = utc_now() - timedelta(seconds=1800)
    store.create_job(build_job(created_at=old, updated_at=old))
    s3_client = FakeS3Client(existing_keys={("elastic-inputs", "inputs/job-1/source")})
    sqs_client = FakeSqsClient()

    summary = build_reconciler(store, s3_client, sqs_client).run_once()

    job = store.get_job("job-1")
    assert summary["requeued"] == 1
    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert sqs_client.sent_messages[0]["body"]["elastic_requeue"]["job_id"] == "job-1"
    assert sqs_client.sent_messages[0]["body"]["elastic_requeue"]["reason"] == "uploading-object-present"


def test_expires_uploading_job_with_no_object_after_window() -> None:
    store = InMemoryJobStore()
    old = utc_now() - timedelta(days=2)
    store.create_job(build_job(created_at=old, updated_at=old))
    sqs_client = FakeSqsClient()

    summary = build_reconciler(store, FakeS3Client(), sqs_client).run_once()

    job = store.get_job("job-1")
    assert summary["expired"] == 1
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert sqs_client.sent_messages == []


def test_recent_uploading_job_is_untouched() -> None:
    store = InMemoryJobStore()
    store.create_job(build_job())

    summary = build_reconciler(store, FakeS3Client(), FakeSqsClient()).run_once()

    job = store.get_job("job-1")
    assert summary == {"requeued": 0, "expired": 0, "reset": 0}
    assert job is not None
    assert job.status == JobStatus.UPLOADING


def test_resets_processing_job_with_expired_lease() -> None:
    store = InMemoryJobStore()
    old = utc_now() - timedelta(seconds=600)
    store.create_job(
        build_job(
            status=JobStatus.PROCESSING,
            attempt_count=1,
            lease_owner="dead-worker",
            lease_expires_at=old + timedelta(seconds=90),
            updated_at=old,
        )
    )
    sqs_client = FakeSqsClient()

    summary = build_reconciler(store, FakeS3Client(), sqs_client).run_once()

    job = store.get_job("job-1")
    assert summary["requeued"] == 1
    assert job is not None
    assert job.status == JobStatus.INTERRUPTED
    assert job.lease_owner is None
    assert sqs_client.sent_messages[0]["body"]["elastic_requeue"]["reason"] == "lease-expired"


def test_leaves_processing_job_with_live_lease_alone() -> None:
    store = InMemoryJobStore()
    old = utc_now() - timedelta(seconds=600)
    store.create_job(
        build_job(
            status=JobStatus.PROCESSING,
            attempt_count=1,
            lease_owner="busy-worker",
            # Stale updated_at but a freshly renewed lease: a long transcode.
            lease_expires_at=utc_now() + timedelta(seconds=60),
            updated_at=old,
        )
    )
    sqs_client = FakeSqsClient()

    build_reconciler(store, FakeS3Client(), sqs_client).run_once()

    job = store.get_job("job-1")
    assert job is not None
    assert job.status == JobStatus.PROCESSING
    assert job.lease_owner == "busy-worker"
    assert sqs_client.sent_messages == []


def test_requeues_stale_queued_job_and_bumps_updated_at() -> None:
    store = InMemoryJobStore()
    old = utc_now() - timedelta(seconds=1200)
    store.create_job(build_job(status=JobStatus.QUEUED, updated_at=old))
    sqs_client = FakeSqsClient()
    reconciler = build_reconciler(store, FakeS3Client(), sqs_client)

    summary = reconciler.run_once()
    # Second sweep right away must not resend: updated_at was bumped.
    second_summary = reconciler.run_once()

    job = store.get_job("job-1")
    assert summary["reset"] == 1
    assert second_summary["reset"] == 0
    assert len(sqs_client.sent_messages) == 1
    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert job.updated_at > old
