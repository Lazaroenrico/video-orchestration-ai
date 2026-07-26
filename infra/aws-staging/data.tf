resource "aws_s3_bucket" "media" {
  bucket = var.media_bucket_name

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket = aws_s3_bucket.media.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "media" {
  bucket = aws_s3_bucket.media.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_cors_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  cors_rule {
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = [var.app_origin]
    allowed_headers = ["Range"]
    expose_headers  = ["Content-Length", "Content-Range", "ETag"]
    max_age_seconds = 3600
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
}

resource "aws_sqs_queue" "wake_dlq" {
  name                      = "ugc-wake-dlq-staging"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "wake" {
  name                       = "ugc-wake-staging"
  message_retention_seconds  = 345600
  receive_wait_time_seconds  = 20
  visibility_timeout_seconds = 180
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.wake_dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "wake_dlq" {
  queue_url = aws_sqs_queue.wake_dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.wake.arn]
  })
}
