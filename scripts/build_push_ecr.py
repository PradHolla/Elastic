#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TERRAFORM_DIR = ROOT_DIR / "infra" / "terraform" / "envs" / "dev"


@dataclass(frozen=True)
class ImageTarget:
    name: str
    repository_output: str
    dockerfile: Path
    context: Path


IMAGE_TARGETS = (
    ImageTarget(
        name="api",
        repository_output="api_ecr_repository_url",
        dockerfile=ROOT_DIR / "apps" / "api" / "Dockerfile",
        context=ROOT_DIR,
    ),
    ImageTarget(
        name="worker",
        repository_output="worker_ecr_repository_url",
        dockerfile=ROOT_DIR / "apps" / "worker" / "Dockerfile",
        context=ROOT_DIR,
    ),
    ImageTarget(
        name="web",
        repository_output="web_ecr_repository_url",
        dockerfile=ROOT_DIR / "apps" / "web" / "Dockerfile",
        context=ROOT_DIR / "apps" / "web",
    ),
)


def terraform_value(outputs: dict[str, Any], name: str) -> Any:
    try:
        return outputs[name]["value"]
    except KeyError as exc:
        raise ValueError(f"Missing Terraform output: {name}") from exc


def load_outputs_from_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse Terraform output JSON from {path}: {exc}") from exc


def load_outputs_from_terraform(terraform_dir: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["terraform", "output", "-json"],
        cwd=str(terraform_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "terraform output failed"
        raise RuntimeError(message)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Terraform returned invalid JSON: {exc}") from exc


def ecr_registry(repository_url: str) -> str:
    return repository_url.split("/", 1)[0]


def ecr_region(repository_url: str) -> str | None:
    host = urlparse(f"https://{ecr_registry(repository_url)}").hostname or ""
    parts = host.split(".")
    if len(parts) >= 4 and parts[1] == "dkr" and parts[2] == "ecr":
        return parts[3]
    return None


def run_command(command: list[str], *, cwd: Path = ROOT_DIR, dry_run: bool = False, input_text: str | None = None) -> None:
    printable = " ".join(command)
    if dry_run:
        print(f"[dry-run] {printable}")
        return

    print(f"[run] {printable}")
    result = subprocess.run(
        command,
        cwd=str(cwd),
        input=input_text,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {printable}")


def ecr_login(*, registry: str, region: str, dry_run: bool) -> None:
    password_command = ["aws", "ecr", "get-login-password", "--region", region]
    login_command = ["docker", "login", "--username", "AWS", "--password-stdin", registry]

    if dry_run:
        print(f"[dry-run] {' '.join(password_command)} | {' '.join(login_command)}")
        return

    print(f"[run] {' '.join(password_command)} | {' '.join(login_command)}")
    password = subprocess.run(
        password_command,
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    if password.returncode != 0:
        message = password.stderr.strip() or password.stdout.strip() or "aws ecr get-login-password failed"
        raise RuntimeError(message)

    login = subprocess.run(
        login_command,
        cwd=str(ROOT_DIR),
        input=password.stdout,
        text=True,
        check=False,
    )
    if login.returncode != 0:
        raise RuntimeError(f"docker login failed with exit code {login.returncode}")


def build_image_commands(
    target: ImageTarget,
    *,
    repository_url: str,
    tag: str,
    platform: str,
) -> list[list[str]]:
    image = f"{repository_url}:{tag}"
    command = [
        "docker",
        "build",
        "--platform",
        platform,
        "-f",
        str(target.dockerfile.relative_to(ROOT_DIR)),
        "-t",
        image,
    ]
    command.append(str(target.context.relative_to(ROOT_DIR)))
    return [command]


def push_image_command(repository_url: str, tag: str) -> list[str]:
    return ["docker", "push", f"{repository_url}:{tag}"]


def selected_targets(raw_targets: list[str]) -> tuple[ImageTarget, ...]:
    if not raw_targets:
        return IMAGE_TARGETS

    names = set(raw_targets)
    unknown = names - {target.name for target in IMAGE_TARGETS}
    if unknown:
        raise ValueError(f"Unknown image target(s): {', '.join(sorted(unknown))}")
    return tuple(target for target in IMAGE_TARGETS if target.name in names)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Elastic app images and push them to ECR repositories from Terraform outputs.",
    )
    parser.add_argument(
        "--terraform-dir",
        type=Path,
        default=DEFAULT_TERRAFORM_DIR,
        help="Terraform environment directory used when --from-file is not provided.",
    )
    parser.add_argument(
        "--from-file",
        type=Path,
        help="Read a saved terraform output -json payload instead of invoking Terraform.",
    )
    parser.add_argument(
        "--tag",
        default="dev",
        help="Image tag to build and push.",
    )
    parser.add_argument(
        "--platform",
        default="linux/amd64",
        help="Docker build platform. Defaults to linux/amd64 for the current EKS node group.",
    )
    parser.add_argument(
        "--target",
        action="append",
        choices=[target.name for target in IMAGE_TARGETS],
        help="Build only one target. Repeat for multiple targets. Defaults to all images.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing Docker or AWS CLI calls.",
    )
    parser.add_argument(
        "--skip-login",
        action="store_true",
        help="Skip docker login to ECR.",
    )
    parser.add_argument(
        "--skip-push",
        action="store_true",
        help="Build images but do not push them.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        outputs = (
            load_outputs_from_file(args.from_file)
            if args.from_file
            else load_outputs_from_terraform(args.terraform_dir)
        )
        targets = selected_targets(args.target or [])
        repositories = {
            target.name: terraform_value(outputs, target.repository_output)
            for target in targets
        }

        if not args.skip_login and not args.skip_push:
            registries = sorted({ecr_registry(repository) for repository in repositories.values()})
            regions = {
                registry: ecr_region(registry)
                for registry in registries
            }
            default_region = terraform_value(outputs, "aws_region")
            for registry in registries:
                ecr_login(
                    registry=registry,
                    region=regions[registry] or default_region,
                    dry_run=args.dry_run,
                )

        for target in targets:
            repository_url = repositories[target.name]
            for command in build_image_commands(
                target,
                repository_url=repository_url,
                tag=args.tag,
                platform=args.platform,
            ):
                run_command(command, dry_run=args.dry_run)
            if not args.skip_push:
                run_command(push_image_command(repository_url, args.tag), dry_run=args.dry_run)

    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("Image workflow complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
