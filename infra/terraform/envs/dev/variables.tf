variable "aws_region" {
  description = "AWS region for the development environment."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name used in resource names and tags."
  type        = string
  default     = "elastic"
}

variable "environment" {
  description = "Environment name used in resource names and tags."
  type        = string
  default     = "dev"
}

variable "vpc_cidr" {
  description = "CIDR block for the EKS VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "Public subnet CIDR blocks for the first EKS slice."
  type        = list(string)
  default     = ["10.42.0.0/20", "10.42.16.0/20"]
}

variable "kubernetes_version" {
  description = "EKS Kubernetes control plane version."
  type        = string
  default     = "1.34"
}

variable "node_instance_types" {
  description = "Spot instance types for the worker node group."
  type        = list(string)
  default     = ["m7i.large", "m6i.large", "m5.large"]
}

variable "node_desired_size" {
  description = "Initial desired size for the Spot node group."
  type        = number
  default     = 1
}

variable "node_min_size" {
  description = "Minimum size for the Spot node group."
  type        = number
  default     = 0
}

variable "node_max_size" {
  description = "Maximum size for the Spot node group."
  type        = number
  default     = 3
}

variable "dashboard_allowed_origins" {
  description = "Origins allowed to upload directly to the S3 media bucket."
  type        = list(string)
  default     = ["*"]
}

variable "force_destroy_buckets" {
  description = "Allow Terraform to delete non-empty buckets in development."
  type        = bool
  default     = true
}

variable "force_delete_ecr_repositories" {
  description = "Allow Terraform to delete non-empty ECR repositories in development."
  type        = bool
  default     = true
}
