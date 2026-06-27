# Terraform Infrastructure

Terraform owns the AWS foundation for Elastic. Kustomize owns the Kubernetes app layer.

For the complete AWS deployment sequence, including Helm/KEDA/Load Balancer Controller setup and day-2 commands, see [`../../docs/aws-deploy.md`](../../docs/aws-deploy.md).

## Layout

- `bootstrap/`: creates the S3 bucket and DynamoDB table used for Terraform remote state.
- `envs/dev/`: creates the first AWS development environment: VPC, EKS, ECR, S3, SQS, DynamoDB, and IAM roles.

## First Run

```bash
cd infra/terraform/bootstrap
terraform init
terraform apply
```

Use the bootstrap outputs to configure the backend in `envs/dev/backend.tf`, then run:

```bash
cd ../envs/dev
terraform init
terraform plan
terraform apply
```

The `dev` environment outputs the values that the AWS Kustomize overlay will consume next.

## App Deployment Flow

After `envs/dev` has been applied and `aws eks update-kubeconfig` points kubectl at the new cluster:

```bash
uv run python scripts/build_push_ecr.py --tag dev
uv run python scripts/render_aws_overlay.py --image-tag dev
uv run python scripts/install_eks_addons.py
kubectl apply -k infra/k8s/overlays/aws-generated
```
