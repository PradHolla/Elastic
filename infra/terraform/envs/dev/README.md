# Elastic Dev Environment

This Terraform stack creates the first AWS environment for Elastic.

It intentionally uses public subnets for the first EKS slice to avoid NAT gateway cost during the portfolio build. The API stays internal to the cluster; the web service will become the public entrypoint through the AWS Kustomize overlay.

## Validate Locally

```bash
terraform init -backend=false
terraform validate
```

## Deploy

After running `../bootstrap`, configure remote state for this directory, then:

```bash
cp backend.tf.example backend.tf
terraform init
terraform plan
terraform apply
```

Then update local kubeconfig:

```bash
aws eks update-kubeconfig --region us-east-1 --name elastic-dev-eks
```

The outputs from this stack are the source of truth for the AWS Kustomize overlay.

## Build And Push Images

After Terraform creates the ECR repositories, build and push all app images:

```bash
uv run python scripts/build_push_ecr.py --tag dev
```

Then render the Kubernetes overlay with the same tag:

```bash
uv run python scripts/render_aws_overlay.py --image-tag dev
kubectl kustomize infra/k8s/overlays/aws-generated
```

## Install EKS Add-ons

After updating kubeconfig, install the cluster add-ons required by the generated overlay:

```bash
uv run python scripts/install_eks_addons.py
```

This installs:

- KEDA, used by `ScaledObject` to scale workers from SQS queue depth.
- AWS Load Balancer Controller, used by the dashboard `Ingress` to create an internet-facing ALB.

You can preview the image workflow before applying Terraform:

```bash
uv run python scripts/build_push_ecr.py \
  --from-file infra/terraform/envs/dev/terraform-output.sample.json \
  --tag dev \
  --dry-run
```

You can also preview the add-on workflow before applying Terraform:

```bash
uv run python scripts/install_eks_addons.py \
  --from-file infra/terraform/envs/dev/terraform-output.sample.json \
  --dry-run
```
