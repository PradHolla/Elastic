from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Optional

from botocore.client import BaseClient
from botocore.exceptions import ClientError

from apps.common import metrics
from apps.common.obs import get_logger

from .models import JobRecord, JobStatus, utc_now
from .settings import Settings
from .store import JobStore

logger = get_logger("elastic.reconciler")


class Reconciler:
    """Periodic sweep that makes stuck jobs self-healing.

    Queues guarantee delivery of messages that exist, not that a message was
    ever produced. If an S3 notification is lost, a message is dead-lettered,
    or a worker dies without a trace, some job sits in a non-terminal state
    forever. This sweep finds those jobs and either re-queues or expires them.
    Every transition goes through the same conditional writes the workers use,
    so a racing worker always wins over the reconciler.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        job_store: JobStore,
        s3_client: BaseClient,
        sqs_client: BaseClient,
        queue_url: str,
    ) -> None:
        self._settings = settings
        self._job_store = job_store
        self._s3_client = s3_client
        self._sqs_client = sqs_client
        self._queue_url = queue_url
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="elastic-reconciler", daemon=True)
        self._thread.start()
        logger.info(
            "reconciler started",
            extra={"interval_seconds": self._settings.reconciler_interval_seconds},
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop_event.wait(self._settings.reconciler_interval_seconds):
            try:
                self.run_once()
            except Exception as exc:
                logger.error("reconciler sweep failed", extra={"error": str(exc)})

    def run_once(self, now: Optional[datetime] = None) -> dict[str, int]:
        now = now or utc_now()
        summary = {"requeued": 0, "expired": 0, "reset": 0}
        self._sweep_uploading(now, summary)
        self._sweep_processing(now, summary)
        for status in (JobStatus.QUEUED, JobStatus.INTERRUPTED):
            self._sweep_requeueable(status, now, summary)
        if any(summary.values()):
            logger.info("reconciler sweep finished", extra=summary)
        return summary

    def _sweep_uploading(self, now: datetime, summary: dict[str, int]) -> None:
        cutoff = now - timedelta(seconds=self._settings.stale_uploading_seconds)
        for job in self._job_store.find_stale_jobs(status=JobStatus.UPLOADING, updated_before=cutoff):
            if self._input_object_exists(job):
                # Upload finished but the event never made it to a worker.
                queued = self._job_store.transition_job_state(
                    job.job_id,
                    allowed_current_statuses=(JobStatus.UPLOADING,),
                    new_status=JobStatus.QUEUED,
                    updated_at=now,
                    last_error=None,
                )
                if queued is not None:
                    self._send_requeue(job, reason="uploading-object-present")
                    metrics.RECONCILER_REQUEUED.labels(reason="uploading-object-present").inc()
                    summary["requeued"] += 1
                    logger.warning(
                        "requeued job whose upload event was lost",
                        extra={"job_id": job.job_id},
                    )
            elif job.created_at <= now - timedelta(seconds=self._settings.upload_expiry_seconds):
                expired = self._job_store.transition_job_state(
                    job.job_id,
                    allowed_current_statuses=(JobStatus.UPLOADING,),
                    new_status=JobStatus.FAILED,
                    updated_at=now,
                    last_error="Source object never arrived before the upload window expired.",
                )
                if expired is not None:
                    metrics.RECONCILER_EXPIRED.inc()
                    summary["expired"] += 1
                    logger.warning("expired job with no upload", extra={"job_id": job.job_id})

    def _sweep_processing(self, now: datetime, summary: dict[str, int]) -> None:
        # Anything past one lease duration with no updated_at movement is a
        # candidate, but the conditional write below only fires if the lease
        # really has expired — an active worker renews it and is untouchable.
        cutoff = now - timedelta(seconds=self._settings.lease_duration_seconds)
        for job in self._job_store.find_stale_jobs(status=JobStatus.PROCESSING, updated_before=cutoff):
            interrupted = self._job_store.transition_job_state(
                job.job_id,
                allowed_current_statuses=(JobStatus.PROCESSING,),
                new_status=JobStatus.INTERRUPTED,
                updated_at=now,
                last_error="Lease expired without completion; reset by reconciler.",
                require_lease_expired_before=now,
            )
            if interrupted is not None:
                self._send_requeue(job, reason="lease-expired")
                metrics.RECONCILER_REQUEUED.labels(reason="lease-expired").inc()
                summary["requeued"] += 1
                logger.warning(
                    "reset job with expired lease",
                    extra={"job_id": job.job_id, "previous_owner": job.lease_owner},
                )

    def _sweep_requeueable(self, status: JobStatus, now: datetime, summary: dict[str, int]) -> None:
        cutoff = now - timedelta(seconds=self._settings.stale_queued_seconds)
        for job in self._job_store.find_stale_jobs(status=status, updated_before=cutoff):
            # Same-status transition bumps updated_at so one sweep does not
            # resend the same job every cycle.
            reset = self._job_store.transition_job_state(
                job.job_id,
                allowed_current_statuses=(status,),
                new_status=status,
                updated_at=now,
                last_error=job.last_error,
            )
            if reset is not None:
                self._send_requeue(job, reason=f"stale-{status.value.lower()}")
                metrics.RECONCILER_REQUEUED.labels(reason=f"stale-{status.value.lower()}").inc()
                summary["reset"] += 1
                logger.warning(
                    "requeued stale job",
                    extra={"job_id": job.job_id, "status": status.value},
                )

    def _input_object_exists(self, job: JobRecord) -> bool:
        try:
            self._s3_client.head_object(Bucket=job.input_bucket, Key=job.input_key)
            return True
        except ClientError as exc:
            status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            error_code = exc.response.get("Error", {}).get("Code")
            if status_code == 404 or error_code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def _send_requeue(self, job: JobRecord, *, reason: str) -> None:
        self._sqs_client.send_message(
            QueueUrl=self._queue_url,
            MessageBody=json.dumps(
                {
                    "elastic_requeue": {
                        "job_id": job.job_id,
                        "bucket": job.input_bucket,
                        "object_key": job.input_key,
                        "reason": reason,
                        "requeued_at": utc_now().isoformat(),
                    }
                }
            ),
        )
