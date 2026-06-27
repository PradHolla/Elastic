data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name_prefix  = "${var.project}-${var.environment}"
  cluster_name = "${local.name_prefix}-eks"

  media_bucket_name = "${local.name_prefix}-${data.aws_caller_identity.current.account_id}-${var.aws_region}-media"
  jobs_table_name   = "${local.name_prefix}-jobs"
  ingest_queue_name = "${local.name_prefix}-ingest"

  availability_zones = slice(data.aws_availability_zones.available.names, 0, length(var.public_subnet_cidrs))

  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
