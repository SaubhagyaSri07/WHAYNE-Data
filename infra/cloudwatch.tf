# ─────────────────────────────────────────────────────────────────────────────
# LOCALS
# ─────────────────────────────────────────────────────────────────────────────

locals {
  # Sources with dedicated Fetch Lambdas.
  # Add new sources here as they are built.
  fetch_sources = [
    "pubmed",
    "clinicaltrials",
    "fda_510k",
    "cdsco",
    "eudamed",
    "cochrane",
    "comtrade",
    "rss",
  ]

  # Log retention — 30 days balances cost vs debuggability.
  # Increase to 90 for production if compliance requires it.
  log_retention_days = 30
}


# ─────────────────────────────────────────────────────────────────────────────
# SNS TOPIC — alarm notifications
# After terraform apply, subscribe your email:
#   AWS Console → SNS → Topics → whayne-dev-alerts → Create subscription
#   Protocol: Email, Endpoint: your@email.com
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_sns_topic" "alerts" {
  name = "${var.project}-${var.environment}-alerts"
}


# ─────────────────────────────────────────────────────────────────────────────
# LOG GROUPS — Lambda functions
# Pre-creating log groups lets us control retention.
# If not pre-created, Lambda auto-creates them with no retention (keeps forever).
# ─────────────────────────────────────────────────────────────────────────────

# One log group per Fetch Lambda (one per source)
resource "aws_cloudwatch_log_group" "fetch_lambda" {
  for_each          = toset(local.fetch_sources)
  name              = "/aws/lambda/${var.project}-${var.environment}-fetch-${each.key}"
  retention_in_days = local.log_retention_days
}

# Shared Normalise Lambda — routes raw content to the correct source map
resource "aws_cloudwatch_log_group" "normalise_lambda" {
  name              = "/aws/lambda/${var.project}-${var.environment}-normalise"
  retention_in_days = local.log_retention_days
}

# Shared Publish Lambda — writes canonical records to Silver
resource "aws_cloudwatch_log_group" "publish_lambda" {
  name              = "/aws/lambda/${var.project}-${var.environment}-publish"
  retention_in_days = local.log_retention_days
}


# ─────────────────────────────────────────────────────────────────────────────
# LOG GROUPS — Step Functions and ECS Fargate
# ─────────────────────────────────────────────────────────────────────────────

# Step Functions execution logs — one group covers all state machines
resource "aws_cloudwatch_log_group" "stepfunctions" {
  name              = "/aws/states/${var.project}-${var.environment}"
  retention_in_days = local.log_retention_days
}

# ECS Fargate — heavy adapters (comtrade, uspto bulk downloads)
resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/aws/ecs/${var.project}-${var.environment}"
  retention_in_days = local.log_retention_days
}


# ─────────────────────────────────────────────────────────────────────────────
# CLOUDWATCH ALARMS
# ─────────────────────────────────────────────────────────────────────────────

# DLQ depth — most critical alarm.
# Any message in the DLQ means a record failed normalisation 3 times.
# Should be zero at all times in a healthy pipeline.
resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name          = "${var.project}-${var.environment}-dlq-depth"
  alarm_description   = "Messages in DLQ — records that failed normalisation 3 times. Investigate immediately."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300 # check every 5 minutes
  statistic           = "Sum"
  threshold           = 0 # alarm on ANY message in DLQ
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.ingestion_dlq.name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# Ingestion queue backlog — normaliser falling behind.
# Normal depth is near zero (messages consumed quickly).
# Sustained depth over 1000 means Lambda throughput is insufficient.
resource "aws_cloudwatch_metric_alarm" "queue_backlog" {
  alarm_name          = "${var.project}-${var.environment}-queue-backlog"
  alarm_description   = "Ingestion queue depth is high — normaliser may be falling behind fetchers."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3 # sustained over 15 minutes before alarming
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 1000
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.ingestion.name
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# Normalise Lambda errors — schema mapping failures.
# Some errors are expected (malformed source records).
# A burst above 5 in 5 minutes suggests a systemic issue.
resource "aws_cloudwatch_metric_alarm" "normalise_errors" {
  alarm_name          = "${var.project}-${var.environment}-normalise-errors"
  alarm_description   = "Normalise Lambda error rate is elevated — check for source schema drift."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = "${var.project}-${var.environment}-normalise"
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# Publish Lambda errors — Silver write failures.
resource "aws_cloudwatch_metric_alarm" "publish_errors" {
  alarm_name          = "${var.project}-${var.environment}-publish-errors"
  alarm_description   = "Publish Lambda error rate is elevated — Silver writes failing."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = "${var.project}-${var.environment}-publish"
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
}


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUTS
# ─────────────────────────────────────────────────────────────────────────────

output "alerts_sns_topic_arn" {
  description = "Subscribe your email here after terraform apply to receive alarm notifications"
  value       = aws_sns_topic.alerts.arn
}

output "log_group_names" {
  description = "CloudWatch log group names for all WHAYNE components"
  value = {
    normalise     = aws_cloudwatch_log_group.normalise_lambda.name
    publish       = aws_cloudwatch_log_group.publish_lambda.name
    stepfunctions = aws_cloudwatch_log_group.stepfunctions.name
    ecs           = aws_cloudwatch_log_group.ecs.name
  }
}