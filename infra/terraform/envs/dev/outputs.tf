output "aws_region" {
  description = "AWS region for this environment."
  value       = var.aws_region
}

output "cluster_name" {
  description = "EKS cluster name."
  value       = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  description = "EKS cluster API endpoint."
  value       = aws_eks_cluster.this.endpoint
}

output "vpc_id" {
  description = "VPC ID used by the EKS cluster and AWS Load Balancer Controller."
  value       = aws_vpc.this.id
}

output "media_bucket_name" {
  description = "S3 bucket used for input and output media objects."
  value       = aws_s3_bucket.media.bucket
}

output "jobs_table_name" {
  description = "DynamoDB jobs table name."
  value       = aws_dynamodb_table.jobs.name
}

output "ingest_queue_name" {
  description = "SQS ingest queue name."
  value       = aws_sqs_queue.ingest.name
}

output "ingest_queue_url" {
  description = "SQS ingest queue URL."
  value       = aws_sqs_queue.ingest.url
}

output "api_ecr_repository_url" {
  description = "ECR repository URL for the API image."
  value       = aws_ecr_repository.api.repository_url
}

output "worker_ecr_repository_url" {
  description = "ECR repository URL for the worker image."
  value       = aws_ecr_repository.worker.repository_url
}

output "web_ecr_repository_url" {
  description = "ECR repository URL for the web image."
  value       = aws_ecr_repository.web.repository_url
}

output "api_role_arn" {
  description = "IRSA role ARN for the API service account."
  value       = aws_iam_role.api.arn
}

output "worker_role_arn" {
  description = "IRSA role ARN for the worker service account."
  value       = aws_iam_role.worker.arn
}

output "keda_role_arn" {
  description = "IRSA role ARN for the KEDA service account."
  value       = aws_iam_role.keda.arn
}

output "load_balancer_controller_role_arn" {
  description = "IRSA role ARN for the AWS Load Balancer Controller service account."
  value       = aws_iam_role.load_balancer_controller.arn
}

output "app_runtime_config" {
  description = "Runtime config values for the AWS Kustomize overlay."
  value = {
    ELASTIC_STORE_BACKEND                       = "dynamodb"
    ELASTIC_JOBS_TABLE_NAME                     = aws_dynamodb_table.jobs.name
    ELASTIC_INPUT_BUCKET_NAME                   = aws_s3_bucket.media.bucket
    ELASTIC_INGEST_QUEUE_NAME                   = aws_sqs_queue.ingest.name
    ELASTIC_AWS_REGION                          = var.aws_region
    ELASTIC_AUTO_CREATE_JOBS_TABLE              = "false"
    ELASTIC_AUTO_CREATE_INPUT_BUCKET            = "false"
    ELASTIC_AUTO_CREATE_INGEST_QUEUE            = "false"
    ELASTIC_AUTO_CONFIGURE_BUCKET_NOTIFICATIONS = "false"
  }
}
