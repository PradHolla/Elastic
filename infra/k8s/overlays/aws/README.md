# AWS Kubernetes Overlay

This overlay adapts the local Kubernetes base for EKS.

It is intentionally small:

- Uses the same `elastic` namespace and app manifests from `infra/k8s/base`.
- Replaces local image names with ECR image placeholders.
- Annotates the API and worker service accounts for IRSA.
- Keeps the API as an internal `ClusterIP` service.
- Exposes only the web dashboard through an AWS Load Balancer Controller `Ingress`.
- Adds KEDA autoscaling for the worker deployment from SQS queue depth.

## Prerequisites

Install these cluster add-ons before applying this overlay:

- AWS Load Balancer Controller, because `ingress.yaml` uses `ingressClassName: alb`.
- KEDA, because `keda-scaledobject.yaml` uses `ScaledObject` and `TriggerAuthentication` CRDs.
- KEDA's operator service account must be annotated with the Terraform `keda_role_arn` output.

## Generate Account-Specific Overlay

The checked-in overlay renders with safe placeholder values. For a real AWS deploy, generate an account-specific overlay from Terraform outputs instead of manually editing the placeholder YAML:

```bash
uv run python scripts/render_aws_overlay.py --image-tag dev
```

That command runs `terraform output -json` from `infra/terraform/envs/dev` and writes:

```text
infra/k8s/overlays/aws-generated/
```

The generated directory is gitignored because it contains account-specific values and can be recreated at any time. It composes directly from `infra/k8s/base`; the checked-in `aws` overlay remains a readable template/reference.

You can test the renderer before applying Terraform with the sample fixture:

```bash
uv run python scripts/render_aws_overlay.py \
  --from-file infra/terraform/envs/dev/terraform-output.sample.json \
  --image-tag dev
```

## Render Locally

Rendering does not create cloud resources:

```bash
kubectl kustomize infra/k8s/overlays/aws
kubectl kustomize infra/k8s/overlays/aws-generated
```

## Apply Later

Apply the generated overlay only after Terraform has created the AWS resources and the images have been pushed to ECR:

```bash
uv run python scripts/build_push_ecr.py --tag dev
uv run python scripts/render_aws_overlay.py --image-tag dev
uv run python scripts/install_eks_addons.py
kubectl apply -k infra/k8s/overlays/aws-generated
```

## KEDA Notes

The SQS scaler uses `TriggerAuthentication` with `podIdentity.provider: aws`, matching KEDA's current AWS IRSA authentication pattern. The scaler is configured with `scaleOnInFlight: "true"` so long-running, already-claimed FFmpeg jobs still count toward desired worker capacity instead of being mistaken for an empty queue.
