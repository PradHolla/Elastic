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


def run_command(command: list[str], *, dry_run: bool) -> None:
    printable = " ".join(command)
    if dry_run:
        print(f"[dry-run] {printable}")
        return

    print(f"[run] {printable}")
    result = subprocess.run(command, cwd=str(ROOT_DIR), check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}: {printable}")


def add_repos(*, dry_run: bool) -> None:
    run_command(["helm", "repo", "add", "eks", "https://aws.github.io/eks-charts"], dry_run=dry_run)
    run_command(["helm", "repo", "add", "kedacore", "https://kedacore.github.io/charts"], dry_run=dry_run)
    run_command(["helm", "repo", "update"], dry_run=dry_run)


def install_keda(outputs: dict[str, Any], *, chart_version: str | None, dry_run: bool) -> None:
    command = [
        "helm",
        "upgrade",
        "--install",
        "keda",
        "kedacore/keda",
        "--namespace",
        "keda",
        "--create-namespace",
        "--set",
        "podIdentity.aws.irsa.enabled=true",
        "--set-string",
        f"podIdentity.aws.irsa.roleArn={terraform_value(outputs, 'keda_role_arn')}",
    ]
    if chart_version:
        command.extend(["--version", chart_version])
    run_command(command, dry_run=dry_run)


def install_load_balancer_controller(
    outputs: dict[str, Any],
    *,
    chart_version: str | None,
    dry_run: bool,
) -> None:
    command = [
        "helm",
        "upgrade",
        "--install",
        "aws-load-balancer-controller",
        "eks/aws-load-balancer-controller",
        "--namespace",
        "kube-system",
        "--set",
        f"clusterName={terraform_value(outputs, 'cluster_name')}",
        "--set",
        f"region={terraform_value(outputs, 'aws_region')}",
        "--set",
        f"vpcId={terraform_value(outputs, 'vpc_id')}",
        "--set",
        "serviceAccount.create=true",
        "--set",
        "serviceAccount.name=aws-load-balancer-controller",
        "--set-string",
        "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn="
        f"{terraform_value(outputs, 'load_balancer_controller_role_arn')}",
        "--set-string",
        'serviceAccount.annotations.eks\\.amazonaws\\.com/sts-regional-endpoints=true',
    ]
    if chart_version:
        command.extend(["--version", chart_version])
    run_command(command, dry_run=dry_run)


def verify_addons(*, dry_run: bool) -> None:
    run_command(["kubectl", "get", "pods", "-n", "keda"], dry_run=dry_run)
    run_command(
        ["kubectl", "get", "deployment", "-n", "kube-system", "aws-load-balancer-controller"],
        dry_run=dry_run,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install EKS add-ons required by Elastic: KEDA and AWS Load Balancer Controller.",
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
        "--dry-run",
        action="store_true",
        help="Print commands without executing Helm or kubectl.",
    )
    parser.add_argument(
        "--skip-repo-update",
        action="store_true",
        help="Do not add/update Helm repositories.",
    )
    parser.add_argument(
        "--skip-verify",
        action="store_true",
        help="Do not run kubectl verification commands after Helm installs.",
    )
    parser.add_argument(
        "--keda-chart-version",
        help="Optional KEDA Helm chart version pin.",
    )
    parser.add_argument(
        "--load-balancer-controller-chart-version",
        default="1.14.0",
        help="AWS Load Balancer Controller Helm chart version. Defaults to the AWS docs version for controller v2.14.1.",
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
        if not args.skip_repo_update:
            add_repos(dry_run=args.dry_run)
        install_keda(outputs, chart_version=args.keda_chart_version, dry_run=args.dry_run)
        install_load_balancer_controller(
            outputs,
            chart_version=args.load_balancer_controller_chart_version,
            dry_run=args.dry_run,
        )
        if not args.skip_verify:
            verify_addons(dry_run=args.dry_run)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("EKS add-on workflow complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
