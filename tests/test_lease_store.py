from datetime import timedelta

from apps.api.app.models import JobRecord, JobStatus, utc_now
from apps.api.app.store import InMemoryJobStore


def build_job(**overrides) -> JobRecord:
    timestamp = utc_now()
    defaults = dict(
        job_id="job-1",
        status=JobStatus.QUEUED,
        preset="1080p",
        input_bucket="elastic-inputs",
        input_key="inputs/job-1/source",
        output_key="outputs/job-1/final/1080p.mp4",
        created_at=timestamp,
        updated_at=timestamp,
    )
    defaults.update(overrides)
    return JobRecord(**defaults)


def test_claim_sets_lease_and_advances_attempt() -> None:
    store = InMemoryJobStore()
    store.create_job(build_job())
    now = utc_now()

    claimed = store.claim_job("job-1", worker_id="worker-a", now=now, lease_duration_seconds=90)

    assert claimed is not None
    assert claimed.status == JobStatus.PROCESSING
    assert claimed.lease_owner == "worker-a"
    assert claimed.lease_expires_at == now + timedelta(seconds=90)
    assert claimed.attempt_count == 1


def test_claim_rejected_while_lease_is_live() -> None:
    store = InMemoryJobStore()
    store.create_job(build_job())
    store.claim_job("job-1", worker_id="worker-a", now=utc_now(), lease_duration_seconds=90)

    stolen = store.claim_job("job-1", worker_id="worker-b", now=utc_now(), lease_duration_seconds=90)

    assert stolen is None


def test_claim_steals_expired_lease_and_advances_fencing_token() -> None:
    store = InMemoryJobStore()
    store.create_job(build_job())
    claim_time = utc_now() - timedelta(seconds=600)
    store.claim_job("job-1", worker_id="worker-a", now=claim_time, lease_duration_seconds=90)

    stolen = store.claim_job("job-1", worker_id="worker-b", now=utc_now(), lease_duration_seconds=90)

    assert stolen is not None
    assert stolen.lease_owner == "worker-b"
    assert stolen.attempt_count == 2


def test_renew_lease_rejects_stale_owner() -> None:
    store = InMemoryJobStore()
    store.create_job(build_job())
    claim_time = utc_now() - timedelta(seconds=600)
    store.claim_job("job-1", worker_id="worker-a", now=claim_time, lease_duration_seconds=90)
    store.claim_job("job-1", worker_id="worker-b", now=utc_now(), lease_duration_seconds=90)

    # The zombie original owner tries to heartbeat with its stale attempt.
    renewed = store.renew_lease(
        "job-1", worker_id="worker-a", expected_attempt=1, now=utc_now(), lease_duration_seconds=90
    )

    assert renewed is False
    assert (
        store.renew_lease(
            "job-1", worker_id="worker-b", expected_attempt=2, now=utc_now(), lease_duration_seconds=90
        )
        is True
    )


def test_fenced_transition_rejects_zombie_completion() -> None:
    store = InMemoryJobStore()
    store.create_job(build_job())
    claim_time = utc_now() - timedelta(seconds=600)
    store.claim_job("job-1", worker_id="worker-a", now=claim_time, lease_duration_seconds=90)
    store.claim_job("job-1", worker_id="worker-b", now=utc_now(), lease_duration_seconds=90)

    # Zombie worker-a finishes late and tries to complete with attempt 1.
    zombie_completion = store.transition_job_state(
        "job-1",
        allowed_current_statuses=(JobStatus.PROCESSING,),
        new_status=JobStatus.COMPLETED,
        updated_at=utc_now(),
        expected_attempt=1,
    )
    assert zombie_completion is None

    # The rightful owner completes with attempt 2.
    completion = store.transition_job_state(
        "job-1",
        allowed_current_statuses=(JobStatus.PROCESSING,),
        new_status=JobStatus.COMPLETED,
        updated_at=utc_now(),
        expected_attempt=2,
    )
    assert completion is not None
    assert completion.status == JobStatus.COMPLETED
    assert completion.lease_owner is None
    assert completion.lease_expires_at is None


def test_transition_with_lease_guard_protects_live_worker() -> None:
    store = InMemoryJobStore()
    store.create_job(build_job())
    store.claim_job("job-1", worker_id="worker-a", now=utc_now(), lease_duration_seconds=90)

    # Reconciler-style reset must not fire while the lease is live.
    reset = store.transition_job_state(
        "job-1",
        allowed_current_statuses=(JobStatus.PROCESSING,),
        new_status=JobStatus.INTERRUPTED,
        updated_at=utc_now(),
        require_lease_expired_before=utc_now(),
    )
    assert reset is None


def test_find_stale_jobs_filters_by_status_and_age() -> None:
    store = InMemoryJobStore()
    old = utc_now() - timedelta(seconds=3600)
    store.create_job(build_job(job_id="old-job", status=JobStatus.UPLOADING, updated_at=old))
    store.create_job(build_job(job_id="new-job", status=JobStatus.UPLOADING))
    store.create_job(build_job(job_id="queued-job", status=JobStatus.QUEUED, updated_at=old))

    stale = store.find_stale_jobs(
        status=JobStatus.UPLOADING, updated_before=utc_now() - timedelta(seconds=900)
    )

    assert [job.job_id for job in stale] == ["old-job"]


def test_list_jobs_filters_by_status() -> None:
    store = InMemoryJobStore()
    store.create_job(build_job(job_id="a", status=JobStatus.QUEUED))
    store.create_job(build_job(job_id="b", status=JobStatus.COMPLETED))

    queued = store.list_jobs(status=JobStatus.QUEUED)

    assert [job.job_id for job in queued] == ["a"]
