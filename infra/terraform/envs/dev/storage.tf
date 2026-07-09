resource "aws_s3_bucket" "media" {
  bucket        = local.media_bucket_name
  force_destroy = var.force_destroy_buckets
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket = aws_s3_bucket.media.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "media" {
  bucket = aws_s3_bucket.media.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_cors_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["PUT", "GET", "HEAD"]
    allowed_origins = var.dashboard_allowed_origins
    expose_headers  = ["ETag", "x-amz-request-id", "x-amz-id-2"]
    max_age_seconds = 3000
  }
}

resource "aws_sqs_queue" "ingest_dlq" {
  name                      = "${local.ingest_queue_name}-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "ingest" {
  name                       = local.ingest_queue_name
  visibility_timeout_seconds = 120
  message_retention_seconds  = 1209600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ingest_dlq.arn
    maxReceiveCount     = 5
  })
}

data "aws_iam_policy_document" "s3_to_sqs" {
  statement {
    sid     = "AllowS3ObjectEvents"
    effect  = "Allow"
    actions = ["sqs:SendMessage"]

    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }

    resources = [aws_sqs_queue.ingest.arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.media.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "ingest" {
  queue_url = aws_sqs_queue.ingest.id
  policy    = data.aws_iam_policy_document.s3_to_sqs.json
}

resource "aws_s3_bucket_notification" "media" {
  bucket = aws_s3_bucket.media.id

  queue {
    id            = "elastic-ingest-events"
    queue_arn     = aws_sqs_queue.ingest.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "inputs/"
  }

  depends_on = [aws_sqs_queue_policy.ingest]
}

resource "aws_dynamodb_table" "jobs" {
  name         = local.jobs_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "job_id"

  attribute {
    name = "job_id"
    type = "S"
  }

  attribute {
    name = "status"
    type = "S"
  }

  attribute {
    name = "updated_at"
    type = "S"
  }

  # Serves "recent jobs by state" for the dashboard and the reconciler's
  # staleness sweeps without scanning the whole table.
  global_secondary_index {
    name            = "status-updated_at-index"
    hash_key        = "status"
    range_key       = "updated_at"
    projection_type = "ALL"
  }
}
