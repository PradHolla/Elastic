from __future__ import annotations

from datetime import datetime
from threading import Lock
from typing import Dict, Optional, Protocol, Sequence

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from .models import JobStatus
from .models import JobRecord


class JobStore(Protocol):
    def create_job(self, job: JobRecord) -> JobRecord: ...

    def get_job(self, job_id: str) -> Optional[JobRecord]: ...

    def list_jobs(self, *, limit: int = 20) -> list[JobRecord]: ...

    def transition_job_state(
        self,
        job_id: str,
        *,
        allowed_current_statuses: Sequence[JobStatus],
        new_status: JobStatus,
        updated_at: datetime,
        attempt_count_delta: int = 0,
        last_error: Optional[str] = None,
    ) -> Optional[JobRecord]: ...


class JobAlreadyExistsError(Exception):
    pass


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

    def list_jobs(self, *, limit: int = 20) -> list[JobRecord]:
        with self._lock:
            jobs = sorted(
                self._jobs.values(),
                key=lambda job: job.created_at,
                reverse=True,
            )
            return jobs[:limit]

    def transition_job_state(
        self,
        job_id: str,
        *,
        allowed_current_statuses: Sequence[JobStatus],
        new_status: JobStatus,
        updated_at: datetime,
        attempt_count_delta: int = 0,
        last_error: Optional[str] = None,
    ) -> Optional[JobRecord]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in allowed_current_statuses:
                return None

            updated_job = job.model_copy(
                update={
                    "status": new_status,
                    "attempt_count": job.attempt_count + attempt_count_delta,
                    "last_error": last_error,
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

    def list_jobs(self, *, limit: int = 20) -> list[JobRecord]:
        response = self._client.scan(TableName=self._table_name, ConsistentRead=True)
        items = response.get("Items", [])
        jobs = sorted((_deserialize_job(item) for item in items), key=lambda job: job.created_at, reverse=True)
        return jobs[:limit]

    def transition_job_state(
        self,
        job_id: str,
        *,
        allowed_current_statuses: Sequence[JobStatus],
        new_status: JobStatus,
        updated_at: datetime,
        attempt_count_delta: int = 0,
        last_error: Optional[str] = None,
    ) -> Optional[JobRecord]:
        if not allowed_current_statuses:
            raise ValueError("allowed_current_statuses must not be empty.")

        expression_names = {
            "#status": "status",
            "#updated_at": "updated_at",
            "#attempt_count": "attempt_count",
            "#last_error": "last_error",
        }
        expression_values = {
            ":new_status": {"S": new_status.value if isinstance(new_status, JobStatus) else str(new_status)},
            ":updated_at": {"S": _serialize_datetime(updated_at)},
            ":attempt_delta": {"N": str(attempt_count_delta)},
        }
        if last_error is not None:
            expression_names["#last_error"] = "last_error"
            expression_values[":last_error"] = {"S": last_error}

        expected_placeholders = []
        for index, status in enumerate(allowed_current_statuses):
            placeholder = f":expected_{index}"
            expression_values[placeholder] = {"S": status.value if isinstance(status, JobStatus) else str(status)}
            expected_placeholders.append(f"#status = {placeholder}")

        set_clauses = [
            "#status = :new_status",
            "#updated_at = :updated_at",
            "#attempt_count = #attempt_count + :attempt_delta",
        ]
        if last_error is not None:
            set_clauses.append("#last_error = :last_error")
            remove_clauses: list[str] = []
        else:
            remove_clauses = ["#last_error"]

        update_expression_parts = ["SET " + ", ".join(set_clauses)]
        if remove_clauses:
            update_expression_parts.append("REMOVE " + ", ".join(remove_clauses))

        try:
            response = self._client.update_item(
                TableName=self._table_name,
                Key={"job_id": {"S": job_id}},
                ConditionExpression="(" + " OR ".join(expected_placeholders) + ")",
                UpdateExpression=" ".join(update_expression_parts),
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
    return item


def _deserialize_job(item: dict[str, dict[str, str]]) -> JobRecord:
    return JobRecord(
        job_id=item["job_id"]["S"],
        status=JobStatus(item["status"]["S"]),
        preset=item["preset"]["S"],
        input_bucket=item["input_bucket"]["S"],
        input_key=item["input_key"]["S"],
        output_key=item["output_key"]["S"],
        attempt_count=int(item["attempt_count"]["N"]),
        last_error=item.get("last_error", {}).get("S"),
        created_at=_deserialize_datetime(item["created_at"]["S"]),
        updated_at=_deserialize_datetime(item["updated_at"]["S"]),
    )


def _serialize_datetime(value: datetime) -> str:
    return value.isoformat()


def _deserialize_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
