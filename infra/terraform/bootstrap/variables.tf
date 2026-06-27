variable "aws_region" {
  description = "AWS region used for Terraform state resources."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project name used in bootstrap resource names."
  type        = string
  default     = "elastic"
}

variable "environment" {
  description = "Environment name used in bootstrap resource names."
  type        = string
  default     = "dev"
}
