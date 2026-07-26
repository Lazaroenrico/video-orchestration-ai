output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "immutable_image" {
  value = local.immutable_image
}

output "api_origin" {
  value = "https://${aws_lb.api.dns_name}"
}

output "sqs_queue_url" {
  value = aws_sqs_queue.wake.url
}

output "s3_media_bucket" {
  value = aws_s3_bucket.media.id
}
