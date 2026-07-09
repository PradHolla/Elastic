from __future__ import annotations

import boto3
import json
from botocore.exceptions import ClientError


DEFAULT_REGION = "us-east-1"
DEFAULT_ENDPOINT_URL = "http://localhost:4566"
DEFAULT_TABLE_NAME = "elastic-jobs"
DEFAULT_INPUT_BUCKET = "elastic-inputs"
DEFAULT_INGEST_QUEUE = "elastic-ingest"


def ensure_jobs_table(
    *,
    table_name: str = DEFAULT_TABLE_NAME,
    endpoint_url: str = DEFAULT_ENDPOINT_URL,
    region_name: str = DEFAULT_REGION,
    aws_access_key_id: str = "test",
    aws_secret_access_key: str = "test",
) -> None:
    client = boto3.client(
        "dynamodb",
        region_name=region_name,
        endpoint_url=endpoint_url,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )

    status_index = {
        "IndexName": "status-updated_at-index",
        "KeySchema": [
            {"AttributeName": "status", "KeyType": "HASH"},
            {"AttributeName": "updated_at", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    }
    attribute_definitions = [
        {"AttributeName": "job_id", "AttributeType": "S"},
        {"AttributeName": "status", "AttributeType": "S"},
        {"AttributeName": "updated_at", "AttributeType": "S"},
    ]

    try:
        description = client.describe_table(TableName=table_name)["Table"]
        existing_indexes = {
            index["IndexName"] for index in description.get("GlobalSecondaryIndexes", [])
        }
        if status_index["IndexName"] in existing_indexes:
            print(f"DynamoDB table already exists: {table_name}")
            return
        client.update_table(
            TableName=table_name,
            AttributeDefinitions=attribute_definitions,
            GlobalSecondaryIndexUpdates=[{"Create": status_index}],
        )
        print(f"Added {status_index['IndexName']} to existing table: {table_name}")
        return
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code != "ResourceNotFoundException":
            raise

    client.create_table(
        TableName=table_name,
        KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
        AttributeDefinitions=attribute_definitions,
        GlobalSecondaryIndexes=[status_index],
        BillingMode="PAY_PER_REQUEST",
    )
    waiter = client.get_waiter("table_exists")
    waiter.wait(TableName=table_name)
    print(f"Created DynamoDB table: {table_name}")


def ensure_input_bucket(
    *,
    bucket_name: str = DEFAULT_INPUT_BUCKET,
    endpoint_url: str = DEFAULT_ENDPOINT_URL,
    region_name: str = DEFAULT_REGION,
    aws_access_key_id: str = "test",
    aws_secret_access_key: str = "test",
) -> None:
    client = boto3.client(
        "s3",
        region_name=region_name,
        endpoint_url=endpoint_url,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )

    try:
        client.head_bucket(Bucket=bucket_name)
        print(f"S3 bucket already exists: {bucket_name}")
        return
    except ClientError as exc:
        status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        error_code = exc.response.get("Error", {}).get("Code")
        if status_code not in {400, 404} and error_code not in {"404", "NoSuchBucket", "NotFound"}:
            raise

    create_kwargs = {"Bucket": bucket_name}
    if region_name != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region_name}
    client.create_bucket(**create_kwargs)
    print(f"Created S3 bucket: {bucket_name}")


def configure_input_bucket_cors(
    *,
    bucket_name: str = DEFAULT_INPUT_BUCKET,
    endpoint_url: str = DEFAULT_ENDPOINT_URL,
    region_name: str = DEFAULT_REGION,
    aws_access_key_id: str = "test",
    aws_secret_access_key: str = "test",
) -> None:
    client = boto3.client(
        "s3",
        region_name=region_name,
        endpoint_url=endpoint_url,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )
    client.put_bucket_cors(
        Bucket=bucket_name,
        CORSConfiguration={
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
    )
    print(f"Configured S3 CORS for bucket: {bucket_name}")


def ensure_ingest_queue(
    *,
    queue_name: str = DEFAULT_INGEST_QUEUE,
    endpoint_url: str = DEFAULT_ENDPOINT_URL,
    region_name: str = DEFAULT_REGION,
    aws_access_key_id: str = "test",
    aws_secret_access_key: str = "test",
) -> tuple[str, str]:
    client = boto3.client(
        "sqs",
        region_name=region_name,
        endpoint_url=endpoint_url,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )

    queue_url = client.create_queue(QueueName=queue_name)["QueueUrl"]
    attrs = client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]
    queue_arn = attrs["QueueArn"]
    print(f"Ensured SQS queue: {queue_name}")
    return queue_url, queue_arn


def configure_bucket_notifications(
    *,
    bucket_name: str = DEFAULT_INPUT_BUCKET,
    queue_arn: str,
    queue_url: str,
    endpoint_url: str = DEFAULT_ENDPOINT_URL,
    region_name: str = DEFAULT_REGION,
    aws_access_key_id: str = "test",
    aws_secret_access_key: str = "test",
) -> None:
    s3_client = boto3.client(
        "s3",
        region_name=region_name,
        endpoint_url=endpoint_url,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )
    sqs_client = boto3.client(
        "sqs",
        region_name=region_name,
        endpoint_url=endpoint_url,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowS3SendMessage",
                "Effect": "Allow",
                "Principal": {"Service": "s3.amazonaws.com"},
                "Action": "SQS:SendMessage",
                "Resource": queue_arn,
                "Condition": {"ArnLike": {"aws:SourceArn": f"arn:aws:s3:::{bucket_name}"}},
            }
        ],
    }
    sqs_client.set_queue_attributes(
        QueueUrl=queue_url,
        Attributes={"Policy": json.dumps(policy)},
    )

    s3_client.put_bucket_notification_configuration(
        Bucket=bucket_name,
        NotificationConfiguration={
            "QueueConfigurations": [
                {
                    "Id": "elastic-ingest-events",
                    "QueueArn": queue_arn,
                    "Events": ["s3:ObjectCreated:*"],
                    "Filter": {"Key": {"FilterRules": [{"Name": "prefix", "Value": "inputs/"}]}},
                }
            ]
        },
    )
    print(f"Configured S3 notifications from {bucket_name} to {queue_arn}")


if __name__ == "__main__":
    ensure_jobs_table()
    ensure_input_bucket()
    configure_input_bucket_cors()
    queue_url, queue_arn = ensure_ingest_queue()
    configure_bucket_notifications(queue_url=queue_url, queue_arn=queue_arn)
