#!/usr/bin/env python3
"""Fault-injection demo: kill a worker mid-transcode and watch recovery.

Two modes:

- ``--mode sigterm`` (graceful): the worker traps the signal, stops ffmpeg,
  marks the job INTERRUPTED, and releases the SQS message immediately. This is
  the Spot-interruption path where the node gives notice.
- ``--mode sigkill`` (hard crash): the worker vanishes with no cleanup, the
  job is left PROCESSING with a live-looking lease. Recovery relies on the
  lease expiring and a second worker stealing the job with a fenced claim.
  This is the path that used to strand jobs forever.

Requirements: LocalStack and the API already running (``uv run python
scripts/dev_up.py`` and stop any workers it started, or run with
``--no-workers``), plus ffmpeg for the workers this script spawns.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT_DIR / "fixtures" / "media" / "file_example_MP4_1920_18MG.mp4"

# Short lease so the sigkill demo does not wait 90s for the steal.
CHAOS_LEASE_SECONDS = 15


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def api_request(base_url: str, path: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(f"{base_url}{path}")
    if payload is not None:
        request.data = json.dumps(payload).encode()
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def upload_fixture(upload: dict, fixture: Path) -> None:
    request = urllib.request.Request(upload["url"], data=fixture.read_bytes(), method=upload["method"])
    for header, value in upload["headers"].items():
        request.add_header(header, value)
    urllib.request.urlopen(request).read()


def worker_env(worker_id: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "ELASTIC_STORE_BACKEND": "dynamodb",
            "ELASTIC_AWS_ENDPOINT_URL": env.get("ELASTIC_AWS_ENDPOINT_URL", "http://localhost:4566"),
            "ELASTIC_AWS_ACCESS_KEY_ID": env.get("ELASTIC_AWS_ACCESS_KEY_ID", "test"),
            "ELASTIC_AWS_SECRET_ACCESS_KEY": env.get("ELASTIC_AWS_SECRET_ACCESS_KEY", "test"),
            "ELASTIC_AWS_REGION": env.get("ELASTIC_AWS_REGION", "us-east-1"),
            "ELASTIC_WORKER_ID": worker_id,
            "ELASTIC_LEASE_DURATION_SECONDS": str(CHAOS_LEASE_SECONDS),
        }
    )
    return env


def spawn_worker(worker_id: str) -> subprocess.Popen:
    process = subprocess.Popen(
        [sys.executable, "-m", "apps.worker.app.main", "--loop", "--wait-time-seconds", "2"],
        cwd=ROOT_DIR,
        env=worker_env(worker_id),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log(f"spawned worker '{worker_id}' (pid {process.pid})")
    return process


def watch_job(base_url: str, job_id: str, *, until: set[str], timeout_seconds: int) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_seen = None
    while time.monotonic() < deadline:
        job = api_request(base_url, f"/jobs/{job_id}")
        snapshot = (job["status"], job["attempt_count"], job.get("lease_owner"))
        if snapshot != last_seen:
            owner = f" lease_owner={job.get('lease_owner')}" if job.get("lease_owner") else ""
            log(f"job status={job['status']} attempt={job['attempt_count']}{owner}")
            last_seen = snapshot
        if job["status"] in until:
            return job
        time.sleep(0.5)
    raise TimeoutError(f"job {job_id} did not reach {until} within {timeout_seconds}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["sigterm", "sigkill"], default="sigkill")
    parser.add_argument("--api-base-url", default=os.environ.get("API_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args()

    if not args.fixture.is_file():
        log(f"fixture not found: {args.fixture}")
        return 1

    log(f"=== chaos demo: {args.mode} ===")
    log("creating job")
    created = api_request(
        args.api_base_url,
        "/jobs",
        {
            "filename": args.fixture.name,
            "content_type": "video/mp4",
            "size_bytes": args.fixture.stat().st_size,
            "preset": "1080p",
        },
    )
    job_id = created["job_id"]
    log(f"created job {job_id}")

    log("uploading fixture via presigned URL")
    upload_fixture(created["upload"], args.fixture)

    victim = spawn_worker("chaos-victim")
    rescuer = None
    try:
        watch_job(args.api_base_url, job_id, until={"PROCESSING"}, timeout_seconds=60)

        kill_signal = signal.SIGTERM if args.mode == "sigterm" else signal.SIGKILL
        log(f"sending {kill_signal.name} to chaos-victim mid-transcode")
        victim.send_signal(kill_signal)
        victim.wait(timeout=30)
        log(f"chaos-victim exited with code {victim.returncode}")

        if args.mode == "sigkill":
            log(
                f"job is stranded in PROCESSING with a dead owner; "
                f"waiting for the {CHAOS_LEASE_SECONDS}s lease to expire so it can be stolen"
            )

        rescuer = spawn_worker("chaos-rescuer")
        final = watch_job(args.api_base_url, job_id, until={"COMPLETED", "FAILED"}, timeout_seconds=300)

        log(f"=== final: {final['status']} after {final['attempt_count']} attempt(s) ===")
        if final["status"] == "COMPLETED":
            log(f"output object: {final['output_key']}")
            if args.mode == "sigkill" and final["attempt_count"] < 2:
                log("warning: expected at least 2 attempts in sigkill mode")
            return 0
        return 1
    finally:
        for process in (victim, rescuer):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
