#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import signal
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config


DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_AWS_ENDPOINT_URL = "http://localhost:4566"
DEFAULT_INPUT_FILE = Path("fixtures/media/file_example_MP4_1920_18MG.mp4")
DEFAULT_OUTPUT_PRESET = "1080p"
TERMINAL_STATUSES = {"FAILED", "COMPLETED"}


def load_dotenv_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def getenv(name: str, default: str) -> str:
    return os.environ.get(name, default)


def http_json(method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: {exc.code} {body}") from exc


def upload_presigned(url: str, source_path: Path, content_type: str) -> None:
    data = source_path.read_bytes()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": content_type},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request):
            return
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"PUT {url} failed: {exc.code} {body}") from exc


def build_clients(
    *,
    aws_region: str,
    aws_endpoint_url: str,
    aws_access_key_id: str,
    aws_secret_access_key: str,
) -> tuple[boto3.client, boto3.client, boto3.client]:
    common_kwargs = {
        "region_name": aws_region,
        "endpoint_url": aws_endpoint_url,
        "aws_access_key_id": aws_access_key_id,
        "aws_secret_access_key": aws_secret_access_key,
    }
    s3 = boto3.client(
        "s3",
        **common_kwargs,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    sqs = boto3.client("sqs", **common_kwargs)
    ddb = boto3.client("dynamodb", **common_kwargs)
    return s3, sqs, ddb


def ddb_item_to_plain(item: dict[str, dict[str, str]]) -> dict[str, Any]:
    plain: dict[str, Any] = {}
    for key, value in item.items():
        type_key, type_value = next(iter(value.items()))
        if type_key == "S":
            plain[key] = type_value
        elif type_key == "N":
            plain[key] = int(type_value) if type_value.isdigit() else float(type_value)
        else:
            plain[key] = value
    return plain


def get_job_state(api_base_url: str, job_id: str) -> dict[str, Any]:
    return http_json("GET", f"{api_base_url}/jobs/{job_id}")


def get_ddb_snapshot(ddb_client: boto3.client, table_name: str, job_id: str) -> dict[str, Any] | None:
    response = ddb_client.get_item(
        TableName=table_name,
        Key={"job_id": {"S": job_id}},
        ConsistentRead=True,
    )
    item = response.get("Item")
    if item is None:
        return None
    return ddb_item_to_plain(item)


def get_s3_objects(s3_client: boto3.client, bucket: str, prefix: str) -> list[dict[str, Any]]:
    response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    contents = response.get("Contents", [])
    return [{"key": entry["Key"], "size": entry["Size"]} for entry in contents]


def get_queue_snapshot(sqs_client: boto3.client, queue_url: str) -> dict[str, str]:
    response = sqs_client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
        ],
    )
    return response.get("Attributes", {})


def purge_queue(sqs_client: boto3.client, queue_url: str) -> None:
    sqs_client.purge_queue(QueueUrl=queue_url)


def print_block(title: str, payload: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, indent=2, default=str))


def print_snapshot(
    *,
    api_base_url: str,
    job_id: str,
    input_bucket: str,
    input_key: str,
    output_key: str,
    table_name: str,
    s3_client: boto3.client,
    sqs_client: boto3.client,
    ddb_client: boto3.client,
    queue_url: str,
) -> dict[str, Any]:
    api_job = get_job_state(api_base_url, job_id)
    ddb_job = get_ddb_snapshot(ddb_client, table_name, job_id)
    queue = get_queue_snapshot(sqs_client, queue_url)
    input_objects = get_s3_objects(s3_client, input_bucket, input_key)
    output_objects = get_s3_objects(s3_client, input_bucket, output_key)

    snapshot = {
        "api_job": api_job,
        "ddb_job": ddb_job,
        "queue": queue,
        "s3_input_objects": input_objects,
        "s3_output_objects": output_objects,
    }
    print_block("API job", api_job)
    print_block("DynamoDB job", ddb_job)
    print_block("SQS queue", queue)
    print_block("S3 input objects", input_objects)
    print_block("S3 output objects", output_objects)
    return snapshot


def start_worker_process(env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "apps.worker.app.main",
            "--once",
            "--delete",
        ],
        env=env,
    )


def request_worker_stop(worker_process: subprocess.Popen[str]) -> None:
    if worker_process.poll() is not None:
        return
    print(f"\nSending SIGTERM to worker pid {worker_process.pid}...")
    worker_process.send_signal(signal.SIGTERM)


def main() -> int:
    load_dotenv_file()

    parser = argparse.ArgumentParser(description="Elastic local lab monitor")
    parser.add_argument("--api-base-url", default=getenv("API_BASE_URL", DEFAULT_API_BASE_URL))
    parser.add_argument("--input-file", default=str(DEFAULT_INPUT_FILE))
    parser.add_argument("--content-type", default=None)
    parser.add_argument("--poll-interval", type=float, default=1.5)
    parser.add_argument("--max-seconds", type=float, default=180.0)
    parser.add_argument("--max-worker-runs", type=int, default=10)
    parser.add_argument("--purge-queue", action="store_true", help="Clear the ingest queue before starting.")
    parser.add_argument(
        "--kill-after-seconds",
        type=float,
        default=0.0,
        help="Send SIGTERM to the active worker after this many seconds to simulate interruption.",
    )
    args = parser.parse_args()

    input_file = Path(args.input_file)
    if not input_file.exists():
        raise FileNotFoundError(f"Sample file not found: {input_file}")

    content_type = args.content_type or mimetypes.guess_type(input_file.name)[0] or "video/mp4"
    size_bytes = input_file.stat().st_size

    aws_region = getenv("ELASTIC_AWS_REGION", "us-east-1")
    aws_endpoint_url = getenv("ELASTIC_AWS_ENDPOINT_URL", DEFAULT_AWS_ENDPOINT_URL)
    aws_access_key_id = getenv("ELASTIC_AWS_ACCESS_KEY_ID", "test")
    aws_secret_access_key = getenv("ELASTIC_AWS_SECRET_ACCESS_KEY", "test")
    jobs_table_name = getenv("ELASTIC_JOBS_TABLE_NAME", "elastic-jobs")
    input_bucket_name = getenv("ELASTIC_INPUT_BUCKET_NAME", "elastic-inputs")
    ingest_queue_name = getenv("ELASTIC_INGEST_QUEUE_NAME", "elastic-ingest")

    s3_client, sqs_client, ddb_client = build_clients(
        aws_region=aws_region,
        aws_endpoint_url=aws_endpoint_url,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )
    queue_url = sqs_client.get_queue_url(QueueName=ingest_queue_name)["QueueUrl"]

    if args.purge_queue:
        print(f"Purging queue {queue_url}...")
        purge_queue(sqs_client, queue_url)
        print("Queue purge requested.")

    create_payload = {
        "filename": input_file.name,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "preset": DEFAULT_OUTPUT_PRESET,
    }

    print("Creating job...")
    create_response = http_json("POST", f"{args.api_base_url}/jobs", create_payload)
    print_block("Create response", create_response)

    job_id = create_response["job_id"]
    upload = create_response["upload"]
    input_bucket = create_response.get("input_bucket", input_bucket_name)
    input_key = create_response["input_key"]
    output_key = create_response["output_key"]

    print("\nUploading source file...")
    upload_presigned(upload["url"], input_file, upload["headers"]["Content-Type"])
    print(f"Uploaded {input_file} -> s3://{input_bucket}/{input_key}")

    print("\nStarting worker process...")
    worker_env = os.environ.copy()
    worker_env["PYTHONUNBUFFERED"] = "1"
    worker_process: subprocess.Popen[str] | None = None
    worker_runs = 0
    kill_deadline = time.time() + args.kill_after_seconds if args.kill_after_seconds > 0 else None
    kill_sent = False

    deadline = time.time() + args.max_seconds
    last_status = None
    try:
        while True:
            snapshot = print_snapshot(
                api_base_url=args.api_base_url,
                job_id=job_id,
                input_bucket=input_bucket,
                input_key=input_key,
                output_key=output_key,
                table_name=jobs_table_name,
                s3_client=s3_client,
                sqs_client=sqs_client,
                ddb_client=ddb_client,
                queue_url=queue_url,
            )
            last_status = snapshot["api_job"]["status"]

            if last_status in TERMINAL_STATUSES and worker_process is None:
                print("\nJob reached a terminal state while worker is still running.")
                break

            if worker_process is not None and worker_process.poll() is not None:
                print(f"\nWorker exited with code {worker_process.returncode}")
                worker_process = None

            if worker_process is None and last_status not in TERMINAL_STATUSES and worker_runs < args.max_worker_runs:
                worker_runs += 1
                print(f"\nStarting worker run #{worker_runs}...")
                worker_process = start_worker_process(worker_env)
                if kill_deadline is not None and not kill_sent and time.time() >= kill_deadline:
                    request_worker_stop(worker_process)
                    kill_sent = True

            if worker_process is not None and kill_deadline is not None and not kill_sent and time.time() >= kill_deadline:
                request_worker_stop(worker_process)
                kill_sent = True

            if time.time() >= deadline:
                print("\nTimed out waiting for the job to finish.")
                break

            time.sleep(args.poll_interval)
    finally:
        if worker_process is not None and worker_process.poll() is None:
            worker_process.terminate()
            try:
                worker_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker_process.kill()
                worker_process.wait()

    final_api_job = get_job_state(args.api_base_url, job_id)
    final_ddb_job = get_ddb_snapshot(ddb_client, jobs_table_name, job_id)
    print_block("Final API job", final_api_job)
    print_block("Final DynamoDB job", final_ddb_job)

    return 0 if final_api_job["status"] in TERMINAL_STATUSES else 1


if __name__ == "__main__":
    raise SystemExit(main())
