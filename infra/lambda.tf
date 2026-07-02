# ─────────────────────────────────────────────────────────────────────────────
# PLACEHOLDER LAMBDA PACKAGE
#
# Lambda functions must point at a deployment package to be created.
# This zips a minimal Python handler as a placeholder.
#
# After credentials arrive and code is refactored, update each function with:
#   aws lambda update-function-code \
#     --function-name whayne-dev-fetch-pubmed \
#     --zip-file fileb://real_code.zip
# ─────────────────────────────────────────────────────────────────────────────

data "archive_file" "placeholder" {
  type        = "zip"
  output_path = "${path.module}/placeholder.zip"

  source {
    content  = "def handler(event, context):\n    return {'statusCode': 200, 'body': 'placeholder'}\n"
    filename = "handler.py"
  }
}


# ─────────────────────────────────────────────────────────────────────────────
# FETCH LAMBDAS — one per source
#
# Each Fetch Lambda runs exactly one adapter:
#   1. Reads watermark from SSM to know what's new
#   2. Calls source API / scraper
#   3. Writes raw payload to Bronze (R2)
#   4. Enqueues a claim-check message to SQS
#
# local.fetch_sources is defined in cloudwatch.tf — shared across the module.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_lambda_function" "fetch" {
  for_each = toset(local.fetch_sources)

  function_name = "${var.project}-${var.environment}-fetch-${each.key}"
  description   = "WHAYNE fetch adapter for ${each.key}"
  role          = aws_iam_role.lambda_exec.arn

  filename         = data.archive_file.placeholder.output_path
  source_code_hash = data.archive_file.placeholder.output_base64sha256

  handler     = "handler.handler"
  runtime     = "python3.11"
  timeout     = 900 # 15 minutes — max Lambda allows
  memory_size = 512 # MB — sufficient for HTTP calls + R2 writes

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  # Non-secret config passed directly as env vars.
  # Secrets (R2 keys, Neon URI) are read from SSM at runtime using SSM_PREFIX.
  environment {
    variables = {
      SOURCE_ID     = each.key
      PROJECT       = var.project
      ENVIRONMENT   = var.environment
      SSM_PREFIX    = "/${var.project}"
      SQS_QUEUE_URL = aws_sqs_queue.ingestion.url
    }
  }

  # Ensure log group with retention exists before Lambda creates it without one
  depends_on = [aws_cloudwatch_log_group.fetch_lambda]

  tags = { Component = "fetch", Source = each.key }
}


# ─────────────────────────────────────────────────────────────────────────────
# NORMALISE LAMBDA — shared across all sources
#
# Triggered by SQS (see event source mapping below).
# For each claim-check message:
#   1. Reads raw payload from Bronze (R2) using the S3 key in the message
#   2. Routes to the correct source map via NormaliserEngine
#   3. Validates output against CanonicalRecord Pydantic schema
#   4. Passes validated records to Publish Lambda
#
# Higher memory than Fetch — PyIceberg + PyArrow are memory-hungry.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_lambda_function" "normalise" {
  function_name = "${var.project}-${var.environment}-normalise"
  description   = "WHAYNE normaliser — routes raw Bronze content to canonical records"
  role          = aws_iam_role.lambda_exec.arn

  filename         = data.archive_file.placeholder.output_path
  source_code_hash = data.archive_file.placeholder.output_base64sha256

  handler     = "handler.handler"
  runtime     = "python3.11"
  timeout     = 900
  memory_size = 1024 # PyIceberg + PyArrow need headroom

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      PROJECT       = var.project
      ENVIRONMENT   = var.environment
      SSM_PREFIX    = "/${var.project}"
      SQS_QUEUE_URL = aws_sqs_queue.ingestion.url
    }
  }

  depends_on = [aws_cloudwatch_log_group.normalise_lambda]

  tags = { Component = "normalise" }
}


# ─────────────────────────────────────────────────────────────────────────────
# PUBLISH LAMBDA — shared across all sources
#
# Receives validated CanonicalRecord objects from Normalise Lambda.
# Writes them to Silver (Apache Iceberg on R2, catalog on Neon Postgres).
# Handles deduplication via content_hash before writing.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_lambda_function" "publish" {
  function_name = "${var.project}-${var.environment}-publish"
  description   = "WHAYNE publish — writes canonical records to Silver (Iceberg on R2)"
  role          = aws_iam_role.lambda_exec.arn

  filename         = data.archive_file.placeholder.output_path
  source_code_hash = data.archive_file.placeholder.output_base64sha256

  handler     = "handler.handler"
  runtime     = "python3.11"
  timeout     = 900
  memory_size = 1024

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      PROJECT     = var.project
      ENVIRONMENT = var.environment
      SSM_PREFIX  = "/${var.project}"
    }
  }

  depends_on = [aws_cloudwatch_log_group.publish_lambda]

  tags = { Component = "publish" }
}


# ─────────────────────────────────────────────────────────────────────────────
# SQS → NORMALISE EVENT SOURCE MAPPING
#
# Wires SQS ingestion queue to the Normalise Lambda.
# SQS delivers up to 10 messages per invocation (batch_size).
# Lambda waits up to 30s to accumulate a full batch before invoking
# (maximum_batching_window_in_seconds) — reduces Lambda invocation count.
#
# On Lambda failure, SQS retries up to maxReceiveCount (3, defined in sqs.tf)
# then moves the message to the DLQ automatically.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_lambda_event_source_mapping" "sqs_to_normalise" {
  event_source_arn                   = aws_sqs_queue.ingestion.arn
  function_name                      = aws_lambda_function.normalise.arn
  batch_size                         = 10
  maximum_batching_window_in_seconds = 30
  enabled                            = true
}


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUTS
# ─────────────────────────────────────────────────────────────────────────────

output "normalise_lambda_arn" {
  value = aws_lambda_function.normalise.arn
}

output "publish_lambda_arn" {
  value = aws_lambda_function.publish.arn
}

output "fetch_lambda_arns" {
  value = { for k, v in aws_lambda_function.fetch : k => v.arn }
}