resource "aws_sqs_queue" "ingestion_dlq" {
  name                       = "${var.project}-${var.environment}-ingestion-dlq"
  message_retention_seconds  = 1209600
  visibility_timeout_seconds = 900
}

resource "aws_sqs_queue" "ingestion" {
  name                       = "${var.project}-${var.environment}-ingestion"
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 900

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ingestion_dlq.arn
    maxReceiveCount     = 3
  })
}

output "ingestion_queue_url" {
  value = aws_sqs_queue.ingestion.url
}

output "ingestion_queue_arn" {
  value = aws_sqs_queue.ingestion.arn
}

output "ingestion_dlq_arn" {
  value = aws_sqs_queue.ingestion_dlq.arn
}