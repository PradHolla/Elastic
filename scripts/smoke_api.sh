#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:8000}"
SMOKE_FILE="${SMOKE_FILE:-${ROOT_DIR}/fixtures/media/file_example_MP4_1920_18MG.mp4}"
SMOKE_OUTPUT_FILE="${SMOKE_OUTPUT_FILE:-/tmp/elastic-smoke-output.mp4}"
SMOKE_CONTENT_TYPE="${SMOKE_CONTENT_TYPE:-video/mp4}"
AWS_REGION="${ELASTIC_AWS_REGION:-us-east-1}"
AWS_ENDPOINT_URL="${ELASTIC_AWS_ENDPOINT_URL:-http://localhost:4566}"
AWS_ACCESS_KEY_ID="${ELASTIC_AWS_ACCESS_KEY_ID:-test}"
AWS_SECRET_ACCESS_KEY="${ELASTIC_AWS_SECRET_ACCESS_KEY:-test}"

cleanup() {
  rm -f "${SMOKE_OUTPUT_FILE}"
}

trap cleanup EXIT

if [ ! -f "${SMOKE_FILE}" ]; then
  echo "Smoke fixture not found: ${SMOKE_FILE}" >&2
  exit 1
fi

for binary in ffmpeg ffprobe; do
  if ! command -v "${binary}" >/dev/null 2>&1; then
    echo "${binary} is required for the smoke test." >&2
    exit 1
  fi
done

file_size_bytes() {
  if stat -f %z "$1" >/dev/null 2>&1; then
    stat -f %z "$1"
  else
    stat -c %s "$1"
  fi
}

SMOKE_FILE_SIZE="$(file_size_bytes "${SMOKE_FILE}")"
SMOKE_FILE_NAME="$(basename "${SMOKE_FILE}")"
REQUEST_BODY="$(cat <<EOF
{
  "filename": "${SMOKE_FILE_NAME}",
  "content_type": "${SMOKE_CONTENT_TYPE}",
  "size_bytes": ${SMOKE_FILE_SIZE},
  "preset": "1080p"
}
EOF
)"

create_response="$(curl -sS -X POST "${API_BASE_URL}/jobs" \
  -H "Content-Type: application/json" \
  -d "${REQUEST_BODY}")"

echo "Create response:"
echo "${create_response}"

parsed_fields="$(CREATE_RESPONSE="${create_response}" python3 - <<'PY'
import json
import os

payload = json.loads(os.environ["CREATE_RESPONSE"])
print(payload["job_id"])
print(payload["upload"]["url"])
print(payload["upload"]["headers"]["Content-Type"])
print(payload["input_bucket"])
print(payload["output_key"])
PY
)"

job_id="$(printf '%s\n' "${parsed_fields}" | sed -n '1p')"
upload_url="$(printf '%s\n' "${parsed_fields}" | sed -n '2p')"
content_type="$(printf '%s\n' "${parsed_fields}" | sed -n '3p')"
input_bucket="$(printf '%s\n' "${parsed_fields}" | sed -n '4p')"
output_key="$(printf '%s\n' "${parsed_fields}" | sed -n '5p')"

echo
echo "Uploading sample file to presigned URL..."
curl -sS -X PUT "${upload_url}" \
  -H "Content-Type: ${content_type}" \
  --upload-file "${SMOKE_FILE}" >/dev/null
echo "Upload complete: ${SMOKE_FILE}"

sleep 2

echo
echo "Fetch response before worker:"
curl -sS "${API_BASE_URL}/jobs/${job_id}"
echo

echo
echo "Worker processing result:"
job_status() {
  curl -sS "${API_BASE_URL}/jobs/${job_id}" | python3 -c 'import json, sys; print(json.load(sys.stdin)["status"])'
}

worker_output=""
for attempt in $(seq 1 20); do
  current_status="$(job_status)"
  if [ "${current_status}" = "COMPLETED" ]; then
    break
  fi

  worker_output="$(uv run python -m apps.worker.app.main --once --delete 2>&1 || true)"
  printf '%s\n' "${worker_output}"
  sleep 2
done

final_status="$(job_status)"
if [ "${final_status}" != "COMPLETED" ]; then
  echo "Job did not reach COMPLETED within the retry window." >&2
  exit 1
fi

uv run python - "${AWS_REGION}" "${AWS_ENDPOINT_URL}" "${AWS_ACCESS_KEY_ID}" "${AWS_SECRET_ACCESS_KEY}" "${input_bucket}" "${output_key}" "${SMOKE_OUTPUT_FILE}" <<'PY'
import sys

import boto3
from botocore.config import Config

region, endpoint_url, access_key, secret_key, bucket, key, destination = sys.argv[1:]
client = boto3.client(
    "s3",
    region_name=region,
    endpoint_url=endpoint_url,
    aws_access_key_id=access_key,
    aws_secret_access_key=secret_key,
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)
body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
with open(destination, "wb") as handle:
    handle.write(body)
PY

echo
echo "ffprobe output:"
ffprobe -v error -show_entries format=duration:stream=index,codec_name,codec_type,width,height -of json "${SMOKE_OUTPUT_FILE}"

echo
echo "Fetch response after worker:"
curl -sS "${API_BASE_URL}/jobs/${job_id}"
echo
