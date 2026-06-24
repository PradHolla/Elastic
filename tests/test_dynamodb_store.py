from botocore.stub import Stubber
import boto3

from apps.api.app.models import JobRecord, JobStatus, utc_now
from apps.api.app.store import DynamoDbJobStore


def test_dynamodb_store_creates_and_reads_jobs() -> None:
    client = boto3.client("dynamodb", region_name="us-east-1")
    stubber = Stubber(client)
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

    stubber.add_response(
        "put_item",
        {},
        {
            "TableName": "elastic-jobs",
            "Item": {
                "job_id": {"S": "job-123"},
                "status": {"S": "UPLOADING"},
                "preset": {"S": "1080p"},
                "input_bucket": {"S": "elastic-inputs"},
                "input_key": {"S": "inputs/job-123/source"},
                "output_key": {"S": "outputs/job-123/final/1080p.mp4"},
                "attempt_count": {"N": "0"},
                "created_at": {"S": timestamp.isoformat()},
                "updated_at": {"S": timestamp.isoformat()},
            },
            "ConditionExpression": "attribute_not_exists(job_id)",
        },
    )
    stubber.add_response(
        "get_item",
        {
            "Item": {
                "job_id": {"S": "job-123"},
                "status": {"S": "UPLOADING"},
                "preset": {"S": "1080p"},
                "input_bucket": {"S": "elastic-inputs"},
                "input_key": {"S": "inputs/job-123/source"},
                "output_key": {"S": "outputs/job-123/final/1080p.mp4"},
                "attempt_count": {"N": "0"},
                "created_at": {"S": timestamp.isoformat()},
                "updated_at": {"S": timestamp.isoformat()},
            }
        },
        {
            "TableName": "elastic-jobs",
            "Key": {"job_id": {"S": "job-123"}},
            "ConsistentRead": True,
        },
    )

    stubber.activate()
    store = DynamoDbJobStore(table_name="elastic-jobs", client=client)

    created = store.create_job(job)
    fetched = store.get_job("job-123")

    assert created == job
    assert fetched == job

    stubber.assert_no_pending_responses()
    stubber.deactivate()


def test_dynamodb_store_transitions_jobs_conditionally() -> None:
    client = boto3.client("dynamodb", region_name="us-east-1")
    stubber = Stubber(client)
    timestamp = utc_now()

    stubber.add_response(
        "update_item",
        {
            "Attributes": {
                "job_id": {"S": "job-123"},
                "status": {"S": "PROCESSING"},
                "preset": {"S": "1080p"},
                "input_bucket": {"S": "elastic-inputs"},
                "input_key": {"S": "inputs/job-123/source"},
                "output_key": {"S": "outputs/job-123/final/1080p.mp4"},
                "attempt_count": {"N": "1"},
                "created_at": {"S": timestamp.isoformat()},
                "updated_at": {"S": timestamp.isoformat()},
            }
        },
        {
            "TableName": "elastic-jobs",
            "Key": {"job_id": {"S": "job-123"}},
            "ConditionExpression": "(#status = :expected_0)",
            "UpdateExpression": "SET #status = :new_status, #updated_at = :updated_at, #attempt_count = #attempt_count + :attempt_delta REMOVE #last_error",
            "ExpressionAttributeNames": {
                "#status": "status",
                "#updated_at": "updated_at",
                "#attempt_count": "attempt_count",
                "#last_error": "last_error",
            },
            "ExpressionAttributeValues": {
                ":new_status": {"S": "PROCESSING"},
                ":updated_at": {"S": timestamp.isoformat()},
                ":attempt_delta": {"N": "1"},
                ":expected_0": {"S": "QUEUED"},
            },
            "ReturnValues": "ALL_NEW",
        },
    )

    stubber.activate()
    store = DynamoDbJobStore(table_name="elastic-jobs", client=client)

    updated = store.transition_job_state(
        "job-123",
        allowed_current_statuses=(JobStatus.QUEUED,),
        new_status=JobStatus.PROCESSING,
        updated_at=timestamp,
        attempt_count_delta=1,
        last_error=None,
    )

    assert updated is not None
    assert updated.status == JobStatus.PROCESSING
    assert updated.attempt_count == 1

    stubber.assert_no_pending_responses()
    stubber.deactivate()
