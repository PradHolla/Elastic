#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
API_HEALTH_URL = "http://127.0.0.1:8000/healthz"
WEB_HEALTH_URL = "http://127.0.0.1:5173/"
LOCALSTACK_HEALTH_URLS = (
    "http://127.0.0.1:4566/_localstack/health",
    "http://127.0.0.1:4566/health",
)
LOCALSTACK_COMPOSE_FILE = ROOT_DIR / "infra" / "local" / "docker-compose.yml"


def load_dotenv_file(path: Path = ROOT_DIR / ".env") -> None:
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


def is_local_endpoint(raw_url: str | None) -> bool:
    if not raw_url:
        return False

    parsed = urlparse(raw_url)
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "localstack"} or parsed.port == 4566


def http_ok(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def wait_for_http(urls: tuple[str, ...], *, timeout_seconds: float, label: str) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        for url in urls:
            if http_ok(url):
                return
        time.sleep(0.5)

    raise RuntimeError(f"Timed out waiting for {label}. Last checked: {', '.join(urls)}")


def wait_for_process_or_http(
    process: subprocess.Popen[str],
    urls: tuple[str, ...],
    *,
    timeout_seconds: float,
    label: str,
) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"{label} exited before becoming ready with code {code}")

        for url in urls:
            if http_ok(url):
                return
        time.sleep(0.5)

    raise RuntimeError(f"Timed out waiting for {label}. Last checked: {', '.join(urls)}")


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[str]
    thread: threading.Thread = field(repr=False)

    def is_running(self) -> bool:
        return self.process.poll() is None

    def terminate(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.process.terminate()
        except ProcessLookupError:
            return

    def kill(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            self.process.kill()
        except ProcessLookupError:
            return


def stream_output(name: str, process: subprocess.Popen[str]) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        print(f"[{name}] {line.rstrip()}")
    code = process.wait()
    print(f"[{name}] exited with code {code}")


def spawn_process(name: str, command: list[str], *, cwd: Path, env: dict[str, str]) -> ManagedProcess:
    print(f"Starting {name}: {' '.join(command)}")
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    thread = threading.Thread(target=stream_output, args=(name, process), daemon=True)
    thread.start()
    return ManagedProcess(name=name, process=process, thread=thread)


def pgrep(pattern: str) -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-af", pattern],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return False
    return bool(result.stdout.strip())


def build_runtime_env(*, localstack_mode: bool, api_base_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("VITE_API_BASE_URL", api_base_url)

    if localstack_mode:
        env.setdefault("ELASTIC_STORE_BACKEND", "dynamodb")
        env.setdefault("ELASTIC_AWS_ENDPOINT_URL", "http://localhost:4566")
        env.setdefault("ELASTIC_AWS_ACCESS_KEY_ID", "test")
        env.setdefault("ELASTIC_AWS_SECRET_ACCESS_KEY", "test")
        env.setdefault("ELASTIC_AUTO_CREATE_JOBS_TABLE", "true")
        env.setdefault("ELASTIC_AUTO_CREATE_INPUT_BUCKET", "true")
        env.setdefault("ELASTIC_AUTO_CREATE_INGEST_QUEUE", "true")
        env.setdefault("ELASTIC_AUTO_CONFIGURE_BUCKET_NOTIFICATIONS", "true")
        env.setdefault("ELASTIC_AWS_REGION", "us-east-1")

    return env


def start_localstack() -> None:
    print("Starting LocalStack via Docker Compose...")
    result = subprocess.run(
        ["docker", "compose", "-f", str(LOCALSTACK_COMPOSE_FILE), "up", "-d"],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "docker compose up failed"
        raise RuntimeError(message)

    wait_for_http(LOCALSTACK_HEALTH_URLS, timeout_seconds=90.0, label="LocalStack")
    print("LocalStack is ready.")


def bootstrap_local_resources(env: dict[str, str]) -> None:
    print("Bootstrapping local AWS resources...")
    result = subprocess.run(
        [sys.executable, "-m", "infra.local.bootstrap_localstack"],
        cwd=str(ROOT_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "local resource bootstrap failed"
        raise RuntimeError(message)

    if result.stdout.strip():
        print(result.stdout.rstrip())
    print("Local AWS resources are ready.")


def terminate_processes(processes: list[ManagedProcess], *, timeout_seconds: float = 10.0) -> None:
    for item in processes:
        item.terminate()

    deadline = time.time() + timeout_seconds
    remaining = [item for item in processes if item.is_running()]
    while remaining and time.time() < deadline:
        time.sleep(0.2)
        remaining = [item for item in processes if item.is_running()]

    for item in remaining:
        item.kill()


def main() -> int:
    load_dotenv_file()

    parser = argparse.ArgumentParser(description="Start the Elastic local dev stack.")
    parser.add_argument("--no-localstack", action="store_true", help="Skip starting LocalStack.")
    parser.add_argument("--no-api", action="store_true", help="Skip starting the API.")
    parser.add_argument("--no-worker", action="store_true", help="Skip starting the worker.")
    parser.add_argument("--no-web", action="store_true", help="Skip starting the dashboard.")
    args = parser.parse_args()

    api_base_url = os.environ.get("ELASTIC_WEB_API_BASE_URL", "http://127.0.0.1:8000")
    localstack_mode = not args.no_localstack and is_local_endpoint(os.environ.get("ELASTIC_AWS_ENDPOINT_URL"))
    runtime_env = build_runtime_env(localstack_mode=localstack_mode, api_base_url=api_base_url)

    launched: list[ManagedProcess] = []

    if localstack_mode:
        start_localstack()
        bootstrap_local_resources(runtime_env)
    else:
        print("LocalStack bootstrap skipped.")

    if args.no_api:
        print("API start skipped by flag.")
    elif http_ok(API_HEALTH_URL):
        print("API already running at http://127.0.0.1:8000")
    else:
        if port_open("127.0.0.1", 8000):
            raise RuntimeError("Port 8000 is already in use, but the API health check is not responding.")
        launched.append(
            spawn_process(
                "api",
                [sys.executable, "-m", "uvicorn", "apps.api.app.main:app", "--host", "127.0.0.1", "--port", "8000", "--reload"],
                cwd=ROOT_DIR,
                env=runtime_env,
            )
        )
        wait_for_process_or_http(launched[-1].process, (API_HEALTH_URL,), timeout_seconds=90.0, label="API")
        print("API is ready.")

    if args.no_worker:
        print("Worker start skipped by flag.")
    elif pgrep(r"apps\.worker\.app\.main --loop"):
        print("Worker already running.")
    else:
        launched.append(
            spawn_process(
                "worker",
                [sys.executable, "-m", "apps.worker.app.main", "--loop"],
                cwd=ROOT_DIR,
                env=runtime_env,
            )
        )

    if args.no_web:
        print("Dashboard start skipped by flag.")
    elif http_ok(WEB_HEALTH_URL):
        print("Dashboard already running at http://127.0.0.1:5173")
    else:
        if port_open("127.0.0.1", 5173):
            raise RuntimeError("Port 5173 is already in use, but the dashboard health check is not responding.")
        launched.append(
            spawn_process(
                "web",
                ["npm", "run", "dev"],
                cwd=ROOT_DIR / "apps" / "web",
                env=runtime_env,
            )
        )
        wait_for_process_or_http(launched[-1].process, (WEB_HEALTH_URL,), timeout_seconds=90.0, label="dashboard")
        print("Dashboard is ready.")

    if not launched:
        print("\nEverything is already up. Nothing new was started.")
        print("API:        http://127.0.0.1:8000")
        print("Dashboard:  http://127.0.0.1:5173")
        print("LocalStack: http://127.0.0.1:4566")
        return 0

    print("\nElastic dev stack is running.")
    print("Press Ctrl-C to stop the processes started by this command.")
    print("API:        http://127.0.0.1:8000")
    print("Dashboard:  http://127.0.0.1:5173")
    print("LocalStack: http://127.0.0.1:4566")

    try:
        while True:
            for item in launched:
                code = item.process.poll()
                if code is not None:
                    print(f"{item.name} exited unexpectedly with code {code}; stopping the other launched processes.")
                    others = [candidate for candidate in launched if candidate is not item]
                    terminate_processes(others)
                    return code if code is not None else 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping launched processes...")
        terminate_processes(launched)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
