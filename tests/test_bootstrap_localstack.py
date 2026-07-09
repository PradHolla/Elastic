from botocore.stub import ANY, Stubber
import boto3

from infra.local.bootstrap_localstack import (
    configure_bucket_notifications,
    configure_input_bucket_cors,
    ensure_ingest_queue,
    ensure_input_bucket,
    ensure_jobs_table,
)

JOBS_ATTRIBUTE_DEFINITIONS = [
    {"AttributeName": "job_id", "AttributeType": "S"},
    {"AttributeName": "status", "AttributeType": "S"},
    {"AttributeName": "updated_at", "AttributeType": "S"},
]

STATUS_INDEX = {
    "IndexName": "status-updated_at-index",
    "KeySchema": [
        {"AttributeName": "status", "KeyType": "HASH"},
        {"AttributeName": "updated_at", "KeyType": "RANGE"},
    ],
    "Projection": {"ProjectionType": "ALL"},
}


def test_ensure_jobs_table_creates_table_when_missing(monkeypatch) -> None:
    client = boto3.client("dynamodb", region_name="us-east-1")
    stubber = Stubber(client)

    stubber.add_client_error(
        "describe_table",
        service_error_code="ResourceNotFoundException",
        expected_params={"TableName": "elastic-jobs"},
    )
    stubber.add_response(
        "create_table",
        {
            "TableDescription": {
                "TableName": "elastic-jobs",
                "TableStatus": "CREATING",
                "KeySchema": [{"AttributeName": "job_id", "KeyType": "HASH"}],
                "AttributeDefinitions": [{"AttributeName": "job_id", "AttributeType": "S"}],
                "ProvisionedThroughput": {
                    "NumberOfDecreasesToday": 1,
                    "ReadCapacityUnits": 1,
                    "WriteCapacityUnits": 1,
                },
                "TableSizeBytes": 0,
                "ItemCount": 0,
                "TableArn": "arn:aws:dynamodb:us-east-1:000000000000:table/elastic-jobs",
                "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
                "DeletionProtectionEnabled": False,
            }
        },
        {
            "TableName": "elastic-jobs",
            "KeySchema": [{"AttributeName": "job_id", "KeyType": "HASH"}],
            "AttributeDefinitions": JOBS_ATTRIBUTE_DEFINITIONS,
            "GlobalSecondaryIndexes": [STATUS_INDEX],
            "BillingMode": "PAY_PER_REQUEST",
        },
    )
    stubber.add_response(
        "describe_table",
        {
            "Table": {
                "TableName": "elastic-jobs",
                "TableStatus": "ACTIVE",
                "KeySchema": [{"AttributeName": "job_id", "KeyType": "HASH"}],
                "AttributeDefinitions": [{"AttributeName": "job_id", "AttributeType": "S"}],
                "ProvisionedThroughput": {
                    "NumberOfDecreasesToday": 1,
                    "ReadCapacityUnits": 1,
                    "WriteCapacityUnits": 1,
                },
                "TableSizeBytes": 0,
                "ItemCount": 0,
                "TableArn": "arn:aws:dynamodb:us-east-1:000000000000:table/elastic-jobs",
                "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
                "DeletionProtectionEnabled": False,
            }
        },
        {"TableName": "elastic-jobs"},
    )

    stubber.activate()

    monkeypatch.setattr("infra.local.bootstrap_localstack.boto3.client", lambda *args, **kwargs: client)
    ensure_jobs_table()

    stubber.assert_no_pending_responses()
    stubber.deactivate()


def test_ensure_jobs_table_skips_creation_when_table_exists(monkeypatch) -> None:
    client = boto3.client("dynamodb", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "describe_table",
        {
            "Table": {
                "TableName": "elastic-jobs",
                "TableStatus": "ACTIVE",
                "KeySchema": [{"AttributeName": "job_id", "KeyType": "HASH"}],
                "AttributeDefinitions": JOBS_ATTRIBUTE_DEFINITIONS,
                "GlobalSecondaryIndexes": [
                    {
                        "IndexName": "status-updated_at-index",
                        "KeySchema": STATUS_INDEX["KeySchema"],
                        "Projection": {"ProjectionType": "ALL"},
                        "IndexStatus": "ACTIVE",
                    }
                ],
                "ProvisionedThroughput": {
                    "NumberOfDecreasesToday": 1,
                    "ReadCapacityUnits": 1,
                    "WriteCapacityUnits": 1,
                },
                "TableSizeBytes": 0,
                "ItemCount": 0,
                "TableArn": "arn:aws:dynamodb:us-east-1:000000000000:table/elastic-jobs",
                "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
                "DeletionProtectionEnabled": False,
            }
        },
        {"TableName": "elastic-jobs"},
    )

    stubber.activate()

    monkeypatch.setattr("infra.local.bootstrap_localstack.boto3.client", lambda *args, **kwargs: client)
    ensure_jobs_table()

    stubber.assert_no_pending_responses()
    stubber.deactivate()


def test_ensure_jobs_table_adds_index_to_existing_table(monkeypatch) -> None:
    client = boto3.client("dynamodb", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "describe_table",
        {
            "Table": {
                "TableName": "elastic-jobs",
                "TableStatus": "ACTIVE",
                "KeySchema": [{"AttributeName": "job_id", "KeyType": "HASH"}],
                "AttributeDefinitions": [{"AttributeName": "job_id", "AttributeType": "S"}],
                "ProvisionedThroughput": {
                    "NumberOfDecreasesToday": 1,
                    "ReadCapacityUnits": 1,
                    "WriteCapacityUnits": 1,
                },
                "TableSizeBytes": 0,
                "ItemCount": 0,
                "TableArn": "arn:aws:dynamodb:us-east-1:000000000000:table/elastic-jobs",
                "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
                "DeletionProtectionEnabled": False,
            }
        },
        {"TableName": "elastic-jobs"},
    )
    stubber.add_response(
        "update_table",
        {},
        {
            "TableName": "elastic-jobs",
            "AttributeDefinitions": JOBS_ATTRIBUTE_DEFINITIONS,
            "GlobalSecondaryIndexUpdates": [{"Create": STATUS_INDEX}],
        },
    )

    stubber.activate()

    monkeypatch.setattr("infra.local.bootstrap_localstack.boto3.client", lambda *args, **kwargs: client)
    ensure_jobs_table()

    stubber.assert_no_pending_responses()
    stubber.deactivate()


def test_ensure_input_bucket_creates_bucket_when_missing(monkeypatch) -> None:
    client = boto3.client("s3", region_name="us-east-1")
    stubber = Stubber(client)

    stubber.add_client_error(
        "head_bucket",
        service_error_code="404",
        service_message="Not Found",
        http_status_code=404,
        expected_params={"Bucket": "elastic-inputs"},
    )
    stubber.add_response(
        "create_bucket",
        {"Location": "/elastic-inputs"},
        {"Bucket": "elastic-inputs"},
    )

    stubber.activate()

    monkeypatch.setattr("infra.local.bootstrap_localstack.boto3.client", lambda *args, **kwargs: client)
    ensure_input_bucket()

    stubber.assert_no_pending_responses()
    stubber.deactivate()


def test_ensure_input_bucket_skips_when_bucket_exists(monkeypatch) -> None:
    client = boto3.client("s3", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response("head_bucket", {}, {"Bucket": "elastic-inputs"})

    stubber.activate()

    monkeypatch.setattr("infra.local.bootstrap_localstack.boto3.client", lambda *args, **kwargs: client)
    ensure_input_bucket()

    stubber.assert_no_pending_responses()
    stubber.deactivate()


def test_configure_input_bucket_cors(monkeypatch) -> None:
    client = boto3.client("s3", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "put_bucket_cors",
        {},
        {
            "Bucket": "elastic-inputs",
            "CORSConfiguration": {
                "CORSRules": [
                    {
                        "AllowedOrigins": ["*"],
                        "AllowedMethods": ["PUT", "GET", "HEAD"],
                        "AllowedHeaders": ["*"],
                        "ExposeHeaders": ["ETag", "x-amz-request-id", "x-amz-id-2"],
                        "MaxAgeSeconds": 3000,
                    }
                ]
            },
        },
    )

    stubber.activate()

    monkeypatch.setattr("infra.local.bootstrap_localstack.boto3.client", lambda *args, **kwargs: client)
    configure_input_bucket_cors()

    stubber.assert_no_pending_responses()
    stubber.deactivate()


def test_ensure_ingest_queue_returns_url_and_arn(monkeypatch) -> None:
    client = boto3.client("sqs", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "create_queue",
        {"QueueUrl": "http://localhost:4566/000000000000/elastic-ingest"},
        {"QueueName": "elastic-ingest"},
    )
    stubber.add_response(
        "get_queue_attributes",
        {"Attributes": {"QueueArn": "arn:aws:sqs:us-east-1:000000000000:elastic-ingest"}},
        {
            "QueueUrl": "http://localhost:4566/000000000000/elastic-ingest",
            "AttributeNames": ["QueueArn"],
        },
    )

    stubber.activate()
    monkeypatch.setattr("infra.local.bootstrap_localstack.boto3.client", lambda *args, **kwargs: client)

    queue_url, queue_arn = ensure_ingest_queue()

    assert queue_url == "http://localhost:4566/000000000000/elastic-ingest"
    assert queue_arn == "arn:aws:sqs:us-east-1:000000000000:elastic-ingest"
    stubber.assert_no_pending_responses()
    stubber.deactivate()


def test_configure_bucket_notifications(monkeypatch) -> None:
    s3_client = boto3.client("s3", region_name="us-east-1")
    sqs_client = boto3.client("sqs", region_name="us-east-1")
    s3_stubber = Stubber(s3_client)
    sqs_stubber = Stubber(sqs_client)

    sqs_stubber.add_response(
        "set_queue_attributes",
        {},
        {
            "QueueUrl": "http://localhost:4566/000000000000/elastic-ingest",
            "Attributes": {
                "Policy": ANY,
            },
        },
    )
    s3_stubber.add_response(
        "put_bucket_notification_configuration",
        {},
        {
            "Bucket": "elastic-inputs",
            "NotificationConfiguration": {
                "QueueConfigurations": [
                    {
                        "Id": "elastic-ingest-events",
                        "QueueArn": "arn:aws:sqs:us-east-1:000000000000:elastic-ingest",
                        "Events": ["s3:ObjectCreated:*"],
                        "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "inputs/"}]}},
                    }
                ]
            },
        },
    )

    sqs_stubber.activate()
    s3_stubber.activate()

    def fake_client(service_name: str, *args, **kwargs):
        if service_name == "s3":
            return s3_client
        if service_name == "sqs":
            return sqs_client
        raise AssertionError(service_name)

    monkeypatch.setattr("infra.local.bootstrap_localstack.boto3.client", fake_client)
    configure_bucket_notifications(
        queue_url="http://localhost:4566/000000000000/elastic-ingest",
        queue_arn="arn:aws:sqs:us-east-1:000000000000:elastic-ingest",
    )

    sqs_stubber.assert_no_pending_responses()
    s3_stubber.assert_no_pending_responses()
    sqs_stubber.deactivate()
    s3_stubber.deactivate()
