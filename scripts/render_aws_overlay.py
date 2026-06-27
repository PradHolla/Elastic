#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TERRAFORM_DIR = ROOT_DIR / "infra" / "terraform" / "envs" / "dev"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "infra" / "k8s" / "overlays" / "aws-generated"
BASE_OVERLAY_RELATIVE_PATH = "../../base"


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


def yaml_quote(value: Any) -> str:
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_runtime_config(runtime_config: dict[str, Any]) -> str:
    lines = [
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        "  name: elastic-runtime-config",
        "data:",
    ]
    for key in sorted(runtime_config):
        lines.append(f"  {key}: {yaml_quote(runtime_config[key])}")
    return "\n".join(lines) + "\n"


def render_service_account_patch(name: str, role_arn: str) -> str:
    return "\n".join(
        [
            "apiVersion: v1",
            "kind: ServiceAccount",
            "metadata:",
            f"  name: {name}",
            "  annotations:",
            f"    eks.amazonaws.com/role-arn: {role_arn}",
            '    eks.amazonaws.com/sts-regional-endpoints: "true"',
            "",
        ]
    )


def render_worker_replicas_patch() -> str:
    return "\n".join(
        [
            "apiVersion: apps/v1",
            "kind: Deployment",
            "metadata:",
            "  name: elastic-worker",
            "spec:",
            "  replicas: 0",
            "",
        ]
    )


def render_ingress() -> str:
    return "\n".join(
        [
            "apiVersion: networking.k8s.io/v1",
            "kind: Ingress",
            "metadata:",
            "  name: elastic-web",
            "  annotations:",
            "    alb.ingress.kubernetes.io/scheme: internet-facing",
            "    alb.ingress.kubernetes.io/target-type: ip",
            """    alb.ingress.kubernetes.io/listen-ports: '[{"HTTP":80}]'""",
            "    alb.ingress.kubernetes.io/healthcheck-path: /",
            "spec:",
            "  ingressClassName: alb",
            "  rules:",
            "    - http:",
            "        paths:",
            "          - path: /",
            "            pathType: Prefix",
            "            backend:",
            "              service:",
            "                name: elastic-web",
            "                port:",
            "                  name: http",
            "",
        ]
    )


def render_keda_scaledobject(queue_url: str, aws_region: str) -> str:
    return "\n".join(
        [
            "apiVersion: keda.sh/v1alpha1",
            "kind: TriggerAuthentication",
            "metadata:",
            "  name: elastic-worker-aws-auth",
            "spec:",
            "  podIdentity:",
            "    provider: aws",
            "---",
            "apiVersion: keda.sh/v1alpha1",
            "kind: ScaledObject",
            "metadata:",
            "  name: elastic-worker-sqs",
            "spec:",
            "  scaleTargetRef:",
            "    name: elastic-worker",
            "  minReplicaCount: 0",
            "  maxReplicaCount: 5",
            "  pollingInterval: 10",
            "  cooldownPeriod: 60",
            "  triggers:",
            "    - type: aws-sqs-queue",
            "      authenticationRef:",
            "        name: elastic-worker-aws-auth",
            "      metadata:",
            f"        queueURL: {queue_url}",
            '        queueLength: "1"',
            f"        awsRegion: {aws_region}",
            '        scaleOnInFlight: "true"',
            "",
        ]
    )


def render_kustomization(values: dict[str, str]) -> str:
    return "\n".join(
        [
            "apiVersion: kustomize.config.k8s.io/v1beta1",
            "kind: Kustomization",
            "",
            "namespace: elastic",
            "",
            "resources:",
            f"  - {BASE_OVERLAY_RELATIVE_PATH}",
            "  - ingress.yaml",
            "  - keda-scaledobject.yaml",
            "",
            "images:",
            "  - name: elastic-api",
            f"    newName: {values['api_ecr_repository_url']}",
            f"    newTag: {values['image_tag']}",
            "  - name: elastic-worker",
            f"    newName: {values['worker_ecr_repository_url']}",
            f"    newTag: {values['image_tag']}",
            "  - name: elastic-web",
            f"    newName: {values['web_ecr_repository_url']}",
            f"    newTag: {values['image_tag']}",
            "",
            "patches:",
            "  - path: patches/runtime-config.yaml",
            "  - path: patches/api-serviceaccount-irsa.yaml",
            "  - path: patches/worker-serviceaccount-irsa.yaml",
            "  - path: patches/worker-autoscaled-deployment.yaml",
            "",
        ]
    )


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def render_overlay(outputs: dict[str, Any], output_dir: Path, image_tag: str) -> list[Path]:
    runtime_config = terraform_value(outputs, "app_runtime_config")
    if not isinstance(runtime_config, dict):
        raise ValueError("Terraform output app_runtime_config must be an object")

    values = {
        "api_ecr_repository_url": terraform_value(outputs, "api_ecr_repository_url"),
        "worker_ecr_repository_url": terraform_value(outputs, "worker_ecr_repository_url"),
        "web_ecr_repository_url": terraform_value(outputs, "web_ecr_repository_url"),
        "image_tag": image_tag,
    }
    aws_region = terraform_value(outputs, "aws_region")
    queue_url = terraform_value(outputs, "ingest_queue_url")
    api_role_arn = terraform_value(outputs, "api_role_arn")
    worker_role_arn = terraform_value(outputs, "worker_role_arn")

    files = {
        output_dir / "kustomization.yaml": render_kustomization(values),
        output_dir / "ingress.yaml": render_ingress(),
        output_dir / "keda-scaledobject.yaml": render_keda_scaledobject(queue_url, aws_region),
        output_dir / "patches" / "runtime-config.yaml": render_runtime_config(runtime_config),
        output_dir / "patches" / "api-serviceaccount-irsa.yaml": render_service_account_patch(
            "elastic-api",
            api_role_arn,
        ),
        output_dir / "patches" / "worker-serviceaccount-irsa.yaml": render_service_account_patch(
            "elastic-worker",
            worker_role_arn,
        ),
        output_dir / "patches" / "worker-autoscaled-deployment.yaml": render_worker_replicas_patch(),
    }

    for path, content in files.items():
        write_file(path, content)

    return sorted(files)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render the AWS Kustomize overlay from Terraform outputs.",
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
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where the generated Kustomize overlay should be written.",
    )
    parser.add_argument(
        "--image-tag",
        default="dev",
        help="Container image tag to inject for API, worker, and web images.",
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
        written = render_overlay(outputs, args.output_dir, args.image_tag)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Rendered AWS overlay: {args.output_dir}")
    for path in written:
        print(f"  {path.relative_to(ROOT_DIR)}")
    print("\nValidate with:")
    print(f"  kubectl kustomize {args.output_dir.relative_to(ROOT_DIR)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
