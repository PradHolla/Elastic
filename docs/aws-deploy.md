# AWS Deployment Runbook

This runbook deploys Elastic to the first AWS dev environment.

The goal is intentionally conservative:

- Terraform creates AWS infrastructure.
- Docker builds and pushes images to ECR.
- Helm installs Kubernetes add-ons that are not part of the app.
- Kustomize deploys the app manifests.

## What Gets Created

Terraform provisions:

- VPC, internet gateway, public subnets, route table, and subnet tags for ALB discovery.
- EKS control plane.
- One EKS managed node group using Spot capacity.
- ECR repositories for API, worker, and web images.
- S3 media bucket.
- SQS ingest queue and DLQ.
- DynamoDB jobs table.
- IAM roles for the EKS cluster, nodes, API, worker, KEDA, and AWS Load Balancer Controller.
- S3 object-created notifications into SQS.

Kubernetes deploys:

- API deployment and service.
- Worker deployment scaled by KEDA from SQS queue depth.
- Web dashboard deployment and service.
- Public `Ingress` for the web dashboard through AWS Load Balancer Controller.

## Tools Needed

Install these locally:

```bash
brew install terraform kubectl helm awscli
```

Docker Desktop should also be running.

Check tools:

```bash
aws sts get-caller-identity
terraform version
kubectl version --client=true
helm version
docker version
```

## Why Helm Is Needed

Helm is not used to deploy our app.

We use Helm to install third-party Kubernetes add-ons:

- `KEDA`: provides the `ScaledObject` custom resource that watches SQS and scales worker pods.
- `AWS Load Balancer Controller`: watches Kubernetes `Ingress` resources and creates AWS ALBs.

Our own application is still deployed with Kustomize:

```bash
kubectl apply -k infra/k8s/overlays/aws-generated
```

## Cost Warning

The EKS control plane starts hourly billing once created. Do not apply the dev stack unless you are ready to keep working or destroy it afterward.

The lowest-friction cleanup command is:

```bash
cd infra/terraform/envs/dev
terraform destroy
```

The bootstrap state bucket and lock table are separate. Usually keep them, but if you want a full cleanup:

```bash
cd ../bootstrap
terraform destroy
```

## 1. Bootstrap Terraform State

From repo root:

```bash
cd infra/terraform/bootstrap
terraform init
terraform apply
```

This creates:

- S3 bucket for Terraform state.
- DynamoDB table for Terraform state locking.

Copy the output values into `infra/terraform/envs/dev/backend.tf`.

Example:

```hcl
terraform {
  backend "s3" {
    bucket         = "elastic-dev-<ACCOUNT_ID>-us-east-1-tfstate"
    key            = "envs/dev/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "elastic-dev-terraform-locks"
    encrypt        = true
  }
}
```

`backend.tf` is gitignored because it is environment-specific.

## 2. Apply AWS Dev Infrastructure

```bash
cd /Users/pnh/Projects/Elastic/infra/terraform/envs/dev
terraform init
terraform plan
terraform apply
```

This step creates the expensive pieces, including EKS.

### Applying The Lease/Reconciler Update To An Existing Environment

If the environment was created before the lease-and-reconciler changes, a
fresh `terraform plan` will show three in-place, non-destructive updates:

- `aws_dynamodb_table.jobs`: adds the `status-updated_at-index` GSI (DynamoDB
  builds it online; existing job items are untouched).
- `aws_iam_role_policy.api`: swaps `dynamodb:Scan` for `dynamodb:Query` (table
  plus indexes), adds `s3:AbortMultipartUpload`, and adds
  `sqs:SendMessage`/`sqs:GetQueueUrl` so the reconciler can requeue jobs.
- No worker policy changes.

After applying, rebuild and push both images (step 4) and re-apply the
overlay (step 7) so pods pick up the new config keys
(`ELASTIC_RECONCILER_ENABLED`, `ELASTIC_LEASE_DURATION_SECONDS`,
`ELASTIC_LOG_JSON`, `ELASTIC_METRICS_ENABLED`, `ELASTIC_MAX_ATTEMPTS`).

If an apply is interrupted while EKS is creating, check status:

```bash
aws eks describe-cluster \
  --region us-east-1 \
  --name elastic-dev-eks \
  --query 'cluster.status' \
  --output text
```

If the cluster is `ACTIVE`, inspect Terraform's next plan:

```bash
terraform plan
```

If Terraform says `aws_eks_cluster.this is tainted` and wants to replace it, do not apply immediately. First run:

```bash
terraform untaint aws_eks_cluster.this
terraform plan
```

Only apply when the plan no longer replaces the active cluster:

```bash
terraform apply
```

## 3. Configure kubectl

```bash
aws eks update-kubeconfig \
  --region us-east-1 \
  --name elastic-dev-eks

kubectl get nodes
kubectl get pods -A
```

The Spot node group can take several minutes to show a ready node.

## 4. Build And Push Images

From repo root:

```bash
uv run python scripts/build_push_ecr.py --tag dev
```

This script reads `terraform output -json`, logs into ECR, builds API/worker/web images, and pushes them.

Preview without pushing:

```bash
uv run python scripts/build_push_ecr.py \
  --from-file infra/terraform/envs/dev/terraform-output.sample.json \
  --tag dev \
  --dry-run
```

## 5. Render AWS Kustomize Overlay

```bash
uv run python scripts/render_aws_overlay.py --image-tag dev
kubectl kustomize infra/k8s/overlays/aws-generated
```

This creates `infra/k8s/overlays/aws-generated/` from Terraform outputs.

That directory is gitignored because it contains account-specific values.

## 6. Install EKS Add-ons

```bash
uv run python scripts/install_eks_addons.py
```

This installs or upgrades:

- KEDA in the `keda` namespace.
- AWS Load Balancer Controller in `kube-system`.

Verify:

```bash
kubectl get pods -n keda
kubectl get deployment -n kube-system aws-load-balancer-controller
```

## 7. Deploy The App

```bash
kubectl apply -k infra/k8s/overlays/aws-generated
```

Watch rollout:

```bash
kubectl get pods -n elastic -w
kubectl get scaledobject -n elastic
kubectl get ingress -n elastic
```

## 8. Open The Dashboard

The ALB can take a few minutes to appear.

```bash
kubectl get ingress -n elastic elastic-web
```

Open:

```text
http://<ADDRESS>
```

## AWS Validation Checklist

Use this section after deployment to verify that the live AWS system is healthy.

Check EKS and Spot node group state:

```bash
aws eks describe-cluster \
  --region us-east-1 \
  --name elastic-dev-eks \
  --query '{name:cluster.name,status:cluster.status,version:cluster.version,endpoint:cluster.endpoint}' \
  --output json

aws eks describe-nodegroup \
  --region us-east-1 \
  --cluster-name elastic-dev-eks \
  --nodegroup-name elastic-dev-spot \
  --query '{status:nodegroup.status,capacityType:nodegroup.capacityType,instanceTypes:nodegroup.instanceTypes,scalingConfig:nodegroup.scalingConfig}' \
  --output json
```

Check Kubernetes app and add-on state:

```bash
kubectl get nodes -o wide
kubectl get pods -A -o wide
kubectl get deploy,svc,ingress,scaledobject,hpa -n elastic -o wide
kubectl get events -n elastic --sort-by=.lastTimestamp
```

Check the public ALB and API route. Quote URLs that contain `?`, otherwise zsh may treat them as glob patterns.

```bash
ALB_HOST="$(kubectl get ingress -n elastic elastic-web -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"

curl -sS -o /tmp/elastic-aws-index.html -w '%{http_code} %{size_download}\n' "http://${ALB_HOST}/"
curl -sS -i "http://${ALB_HOST}/api/healthz"
curl -sS "http://${ALB_HOST}/api/jobs?limit=5"
```

Check AWS backing services:

```bash
aws sqs get-queue-attributes \
  --queue-url "$(cd infra/terraform/envs/dev && terraform output -raw ingest_queue_url)" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible ApproximateNumberOfMessagesDelayed \
  --output json

aws dynamodb scan \
  --table-name "$(cd infra/terraform/envs/dev && terraform output -raw jobs_table_name)" \
  --select COUNT \
  --output json

aws s3 ls "s3://$(cd infra/terraform/envs/dev && terraform output -raw media_bucket_name)" --recursive
```

Check ECR images:

```bash
aws ecr describe-images \
  --repository-name elastic-dev/api \
  --query 'imageDetails[].{tags:imageTags,pushed:imagePushedAt,size:imageSizeInBytes}' \
  --output json

aws ecr describe-images \
  --repository-name elastic-dev/worker \
  --query 'imageDetails[].{tags:imageTags,pushed:imagePushedAt,size:imageSizeInBytes}' \
  --output json

aws ecr describe-images \
  --repository-name elastic-dev/web \
  --query 'imageDetails[].{tags:imageTags,pushed:imagePushedAt,size:imageSizeInBytes}' \
  --output json
```

Run a real AWS smoke job through the public ALB:

```bash
uv run python - <<'PY'
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

import boto3
import requests

alb_host = subprocess.check_output(
    ["kubectl", "get", "ingress", "-n", "elastic", "elastic-web", "-o", "jsonpath={.status.loadBalancer.ingress[0].hostname}"],
    text=True,
).strip()
api_base = f"http://{alb_host}/api"
video = Path("fixtures/media/file_example_MP4_1920_18MG.mp4")

create = requests.post(
    f"{api_base}/jobs",
    json={
        "filename": video.name,
        "content_type": "video/mp4",
        "size_bytes": video.stat().st_size,
        "preset": "1080p",
    },
    timeout=30,
)
print("POST /api/jobs", create.status_code)
create.raise_for_status()
payload = create.json()
print(json.dumps(payload, indent=2))

with video.open("rb") as fh:
    put = requests.put(
        payload["upload"]["url"],
        data=fh,
        headers=payload["upload"].get("headers", {}),
        timeout=120,
    )
print("PUT presigned S3", put.status_code)
put.raise_for_status()

job_id = payload["job_id"]
queue_url = subprocess.check_output(
    ["terraform", "output", "-raw", "ingest_queue_url"],
    cwd="infra/terraform/envs/dev",
    text=True,
).strip()
sqs = boto3.client("sqs", region_name="us-east-1")

for poll in range(1, 25):
    job = requests.get(f"{api_base}/jobs/{job_id}", timeout=20).json()
    attrs = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
        ],
    )["Attributes"]
    worker = subprocess.check_output(
        ["kubectl", "get", "deployment", "-n", "elastic", "elastic-worker", "-o", "jsonpath={.status.replicas}/{.status.readyReplicas}"],
        text=True,
    )
    print(json.dumps({
        "poll": poll,
        "status": job["status"],
        "attempt_count": job["attempt_count"],
        "sqs_visible": attrs.get("ApproximateNumberOfMessages"),
        "sqs_in_flight": attrs.get("ApproximateNumberOfMessagesNotVisible"),
        "worker_replicas_ready": worker,
    }))
    if job["status"] in {"COMPLETED", "FAILED"}:
        break
    time.sleep(10)
PY
```

After a successful smoke run, confirm the worker scales down after KEDA's cooldown:

```bash
sleep 75
kubectl get deployment -n elastic elastic-worker -o wide
kubectl get pods -n elastic -l app=elastic-worker -o wide
kubectl get scaledobject -n elastic elastic-worker-sqs
```

## Basic Management

Check app pods:

```bash
kubectl get pods -n elastic
```

Watch worker scaling:

```bash
kubectl get deployment -n elastic elastic-worker -w
kubectl get hpa -n elastic
kubectl get scaledobject -n elastic
```

Read logs:

```bash
kubectl logs -n elastic deployment/elastic-api
kubectl logs -n elastic deployment/elastic-web
kubectl logs -n elastic deployment/elastic-worker
```

Restart a deployment:

```bash
kubectl rollout restart deployment/elastic-api -n elastic
kubectl rollout restart deployment/elastic-web -n elastic
kubectl rollout restart deployment/elastic-worker -n elastic
```

See queue depth:

```bash
aws sqs get-queue-attributes \
  --queue-url "$(cd infra/terraform/envs/dev && terraform output -raw ingest_queue_url)" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```

See jobs table items:

```bash
aws dynamodb scan \
  --table-name "$(cd infra/terraform/envs/dev && terraform output -raw jobs_table_name)" \
  --max-items 10
```

List media objects:

```bash
aws s3 ls "s3://$(cd infra/terraform/envs/dev && terraform output -raw media_bucket_name)" --recursive
```

## Teardown

Delete app resources first:

```bash
kubectl delete -k infra/k8s/overlays/aws-generated
```

Destroy AWS infrastructure:

```bash
cd infra/terraform/envs/dev
terraform destroy
```

Optional full cleanup of Terraform state infrastructure:

```bash
cd ../bootstrap
terraform destroy
```

Do not manually delete Terraform-managed AWS resources in the console unless you are prepared to repair state.
