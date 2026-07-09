from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Dict, Optional, Protocol, Sequence

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from .models import JobStatus
from .models import JobRecord

# GSI that serves "recent jobs by state" for the dashboard and the
# reconciler's staleness sweeps, replacing full-table Scans.
STATUS_UPDATED_AT_INDEX = "status-updated_at-index"


class JobStore(Protocol):
    def create_job(self, job: JobRecord) -> JobRecord: ...

    def get_job(self, job_id: str) -> Optional[JobRecord]: ...

    def list_jobs(self, *, limit: int = 20, status: Optional[JobStatus] = None) -> list[JobRecord]: ...

    def find_stale_jobs(
        self,
        *,
        status: JobStatus,
        updated_before: datetime,
        limit: int = 25,
    ) -> list[JobRecord]: ...

    def claim_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration_seconds: int,
    ) -> Optional[JobRecord]: ...

    def renew_lease(
        self,
        job_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        lease_duration_seconds: int,
    ) -> bool: ...

    def transition_job_state(
        self,
        job_id: str,
        *,
        allowed_current_statuses: Sequence[JobStatus],
        new_status: JobStatus,
        updated_at: datetime,
        attempt_count_delta: int = 0,
        last_error: Optional[str] = None,
        expected_attempt: Optional[int] = None,
        require_lease_expired_before: Optional[datetime] = None,
    ) -> Optional[JobRecord]: ...


class JobAlreadyExistsError(Exception):
    pass


def _lease_expired(job: JobRecord, now: datetime) -> bool:
    return job.lease_expires_at is None or job.lease_expires_at <= now


class InMemoryJobStore(JobStore):
    def __init__(self) -> None:
        self._jobs: Dict[str, JobRecord] = {}
        self._lock = Lock()

    def create_job(self, job: JobRecord) -> JobRecord:
        with self._lock:
            if job.job_id in self._jobs:
                raise JobAlreadyExistsError(f"Job {job.job_id} already exists.")
            self._jobs[job.job_id] = job
            return job

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, *, limit: int = 20, status: Optional[JobStatus] = None) -> list[JobRecord]:
        with self._lock:
            jobs = [
                job
                for job in self._jobs.values()
                if status is None or JobStatus(job.status) == status
            ]
            jobs.sort(key=lambda job: job.updated_at, reverse=True)
            return jobs[:limit]

    def find_stale_jobs(
        self,
        *,
        status: JobStatus,
        updated_before: datetime,
        limit: int = 25,
    ) -> list[JobRecord]:
        with self._lock:
            jobs = [
                job
                for job in self._jobs.values()
                if JobStatus(job.status) == status and job.updated_at <= updated_before
            ]
            jobs.sort(key=lambda job: job.updated_at)
            return jobs[:limit]

    def claim_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration_seconds: int,
    ) -> Optional[JobRecord]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job_status = JobStatus(job.status)
            claimable = job_status == JobStatus.QUEUED or (
                job_status == JobStatus.PROCESSING and _lease_expired(job, now)
            )
            if not claimable:
                return None

            claimed = job.model_copy(
                update={
                    "status": JobStatus.PROCESSING,
                    "attempt_count": job.attempt_count + 1,
                    "lease_owner": worker_id,
                    "lease_expires_at": now + timedelta(seconds=lease_duration_seconds),
                    "last_error": None,
                    "updated_at": now,
                }
            )
            self._jobs[job_id] = claimed
            return claimed

    def renew_lease(
        self,
        job_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        lease_duration_seconds: int,
    ) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if (
                job is None
                or JobStatus(job.status) != JobStatus.PROCESSING
                or job.lease_owner != worker_id
                or job.attempt_count != expected_attempt
            ):
                return False
            self._jobs[job_id] = job.model_copy(
                update={"lease_expires_at": now + timedelta(seconds=lease_duration_seconds)}
            )
            return True

    def transition_job_state(
        self,
        job_id: str,
        *,
        allowed_current_statuses: Sequence[JobStatus],
        new_status: JobStatus,
        updated_at: datetime,
        attempt_count_delta: int = 0,
        last_error: Optional[str] = None,
        expected_attempt: Optional[int] = None,
        require_lease_expired_before: Optional[datetime] = None,
    ) -> Optional[JobRecord]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in allowed_current_statuses:
                return None
            if expected_attempt is not None and job.attempt_count != expected_attempt:
                return None
            if require_lease_expired_before is not None and not _lease_expired(
                job, require_lease_expired_before
            ):
                return None

            updated_job = job.model_copy(
                update={
                    "status": new_status,
                    "attempt_count": job.attempt_count + attempt_count_delta,
                    "last_error": last_error,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "updated_at": updated_at,
                }
            )
            self._jobs[job_id] = updated_job
            return updated_job


class DynamoDbJobStore(JobStore):
    def __init__(self, table_name: str, client: Optional[BaseClient] = None) -> None:
        self._table_name = table_name
        self._client = client or boto3.client("dynamodb")

    def create_job(self, job: JobRecord) -> JobRecord:
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=_serialize_job(job),
                ConditionExpression="attribute_not_exists(job_id)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise JobAlreadyExistsError(f"Job {job.job_id} already exists.") from exc
            raise

        return job

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"job_id": {"S": job_id}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if item is None:
            return None
        return _deserialize_job(item)

    def list_jobs(self, *, limit: int = 20, status: Optional[JobStatus] = None) -> list[JobRecord]:
        statuses = [status] if status is not None else list(JobStatus)
        jobs: list[JobRecord] = []
        for candidate in statuses:
            response = self._client.query(
                TableName=self._table_name,
                IndexName=STATUS_UPDATED_AT_INDEX,
                KeyConditionExpression="#status = :status",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":status": {"S": candidate.value}},
                ScanIndexForward=False,
                Limit=limit,
            )
            jobs.extend(_deserialize_job(item) for item in response.get("Items", []))
        jobs.sort(key=lambda job: job.updated_at, reverse=True)
        return jobs[:limit]

    def find_stale_jobs(
        self,
        *,
        status: JobStatus,
        updated_before: datetime,
        limit: int = 25,
    ) -> list[JobRecord]:
        response = self._client.query(
            TableName=self._table_name,
            IndexName=STATUS_UPDATED_AT_INDEX,
            KeyConditionExpression="#status = :status AND #updated_at <= :cutoff",
            ExpressionAttributeNames={"#status": "status", "#updated_at": "updated_at"},
            ExpressionAttributeValues={
                ":status": {"S": status.value},
                ":cutoff": {"S": _serialize_datetime(updated_before)},
            },
            ScanIndexForward=True,
            Limit=limit,
        )
        return [_deserialize_job(item) for item in response.get("Items", [])]

    def claim_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration_seconds: int,
    ) -> Optional[JobRecord]:
        lease_expires_at = now + timedelta(seconds=lease_duration_seconds)
        try:
            response = self._client.update_item(
                TableName=self._table_name,
                Key={"job_id": {"S": job_id}},
                # Claimable if QUEUED, or if a previous PROCESSING owner let its
                # lease lapse (crashed pod, lost node). Evaluated atomically with
                # the write, so concurrent claimers cannot both win.
                ConditionExpression=(
                    "#status = :queued OR (#status = :processing AND "
                    "(attribute_not_exists(#lease_expires_at) OR #lease_expires_at <= :now_epoch))"
                ),
                UpdateExpression=(
                    "SET #status = :processing, #lease_owner = :owner, "
                    "#lease_expires_at = :lease_expires_at, #updated_at = :updated_at, "
                    "#attempt_count = #attempt_count + :one "
                    "REMOVE #last_error"
                ),
                ExpressionAttributeNames={
                    "#status": "status",
                    "#lease_owner": "lease_owner",
                    "#lease_expires_at": "lease_expires_at",
                    "#updated_at": "updated_at",
                    "#attempt_count": "attempt_count",
                    "#last_error": "last_error",
                },
                ExpressionAttributeValues={
                    ":queued": {"S": JobStatus.QUEUED.value},
                    ":processing": {"S": JobStatus.PROCESSING.value},
                    ":now_epoch": {"N": _serialize_epoch(now)},
                    ":owner": {"S": worker_id},
                    ":lease_expires_at": {"N": _serialize_epoch(lease_expires_at)},
                    ":updated_at": {"S": _serialize_datetime(now)},
                    ":one": {"N": "1"},
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise
        item = response.get("Attributes")
        if item is None:
            return None
        return _deserialize_job(item)

    def renew_lease(
        self,
        job_id: str,
        *,
        worker_id: str,
        expected_attempt: int,
        now: datetime,
        lease_duration_seconds: int,
    ) -> bool:
        lease_expires_at = now + timedelta(seconds=lease_duration_seconds)
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={"job_id": {"S": job_id}},
                # attempt_count is the fencing token: if another worker stole the
                # job, the attempt advanced and this renewal must fail.
                ConditionExpression=(
                    "#status = :processing AND #lease_owner = :owner "
                    "AND #attempt_count = :attempt"
                ),
                UpdateExpression="SET #lease_expires_at = :lease_expires_at",
                ExpressionAttributeNames={
                    "#status": "status",
                    "#lease_owner": "lease_owner",
                    "#lease_expires_at": "lease_expires_at",
                    "#attempt_count": "attempt_count",
                },
                ExpressionAttributeValues={
                    ":processing": {"S": JobStatus.PROCESSING.value},
                    ":owner": {"S": worker_id},
                    ":attempt": {"N": str(expected_attempt)},
                    ":lease_expires_at": {"N": _serialize_epoch(lease_expires_at)},
                },
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def transition_job_state(
        self,
        job_id: str,
        *,
        allowed_current_statuses: Sequence[JobStatus],
        new_status: JobStatus,
        updated_at: datetime,
        attempt_count_delta: int = 0,
        last_error: Optional[str] = None,
        expected_attempt: Optional[int] = None,
        require_lease_expired_before: Optional[datetime] = None,
    ) -> Optional[JobRecord]:
        if not allowed_current_statuses:
            raise ValueError("allowed_current_statuses must not be empty.")

        expression_names = {
            "#status": "status",
            "#updated_at": "updated_at",
            "#attempt_count": "attempt_count",
            "#last_error": "last_error",
            "#lease_owner": "lease_owner",
            "#lease_expires_at": "lease_expires_at",
        }
        expression_values = {
            ":new_status": {"S": new_status.value if isinstance(new_status, JobStatus) else str(new_status)},
            ":updated_at": {"S": _serialize_datetime(updated_at)},
            ":attempt_delta": {"N": str(attempt_count_delta)},
        }
        if last_error is not None:
            expression_values[":last_error"] = {"S": last_error}

        expected_placeholders = []
        for index, status in enumerate(allowed_current_statuses):
            placeholder = f":expected_{index}"
            expression_values[placeholder] = {"S": status.value if isinstance(status, JobStatus) else str(status)}
            expected_placeholders.append(f"#status = {placeholder}")

        condition_parts = ["(" + " OR ".join(expected_placeholders) + ")"]
        if expected_attempt is not None:
            expression_values[":expected_attempt"] = {"N": str(expected_attempt)}
            condition_parts.append("#attempt_count = :expected_attempt")
        if require_lease_expired_before is not None:
            expression_values[":lease_cutoff"] = {"N": _serialize_epoch(require_lease_expired_before)}
            condition_parts.append(
                "(attribute_not_exists(#lease_expires_at) OR #lease_expires_at <= :lease_cutoff)"
            )

        set_clauses = [
            "#status = :new_status",
            "#updated_at = :updated_at",
            "#attempt_count = #attempt_count + :attempt_delta",
        ]
        # Every transition ends the current attempt, so the lease always comes
        # off; PROCESSING is only ever entered through claim_job.
        remove_clauses = ["#lease_owner", "#lease_expires_at"]
        if last_error is not None:
            set_clauses.append("#last_error = :last_error")
        else:
            remove_clauses.append("#last_error")

        update_expression = "SET " + ", ".join(set_clauses) + " REMOVE " + ", ".join(remove_clauses)

        try:
            response = self._client.update_item(
                TableName=self._table_name,
                Key={"job_id": {"S": job_id}},
                ConditionExpression=" AND ".join(condition_parts),
                UpdateExpression=update_expression,
                ExpressionAttributeNames=expression_names,
                ExpressionAttributeValues=expression_values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

        item = response.get("Attributes")
        if item is None:
            return None
        return _deserialize_job(item)


def _serialize_job(job: JobRecord) -> dict[str, dict[str, str]]:
    status_value = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
    item: dict[str, dict[str, str]] = {
        "job_id": {"S": job.job_id},
        "status": {"S": status_value},
        "preset": {"S": job.preset},
        "input_bucket": {"S": job.input_bucket},
        "input_key": {"S": job.input_key},
        "output_key": {"S": job.output_key},
        "attempt_count": {"N": str(job.attempt_count)},
        "created_at": {"S": _serialize_datetime(job.created_at)},
        "updated_at": {"S": _serialize_datetime(job.updated_at)},
    }
    if job.last_error is not None:
        item["last_error"] = {"S": job.last_error}
    if job.lease_owner is not None:
        item["lease_owner"] = {"S": job.lease_owner}
    if job.lease_expires_at is not None:
        item["lease_expires_at"] = {"N": _serialize_epoch(job.lease_expires_at)}
    return item


def _deserialize_job(item: dict[str, dict[str, str]]) -> JobRecord:
    lease_expires_raw = item.get("lease_expires_at", {}).get("N")
    return JobRecord(
        job_id=item["job_id"]["S"],
        status=JobStatus(item["status"]["S"]),
        preset=item["preset"]["S"],
        input_bucket=item["input_bucket"]["S"],
        input_key=item["input_key"]["S"],
        output_key=item["output_key"]["S"],
        attempt_count=int(item["attempt_count"]["N"]),
        last_error=item.get("last_error", {}).get("S"),
        lease_owner=item.get("lease_owner", {}).get("S"),
        lease_expires_at=_deserialize_epoch(lease_expires_raw) if lease_expires_raw else None,
        created_at=_deserialize_datetime(item["created_at"]["S"]),
        updated_at=_deserialize_datetime(item["updated_at"]["S"]),
    )


def _serialize_datetime(value: datetime) -> str:
    return value.isoformat()


def _deserialize_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _serialize_epoch(value: datetime) -> str:
    # Stored as a number so DynamoDB condition expressions compare it
    # numerically rather than lexicographically.
    return str(value.timestamp())


def _deserialize_epoch(value: str) -> datetime:
    return datetime.fromtimestamp(float(value), tz=timezone.utc)
