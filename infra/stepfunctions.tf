# ─────────────────────────────────────────────────────────────────────────────
# STEP FUNCTIONS STATE MACHINES — one per source
#
# Each state machine orchestrates the fetch half of the pipeline:
#   Fetch → (success) → FetchSucceeded
#         → (failure) → FetchFailed
#
# The normalise + publish half runs automatically via the SQS event source
# mapping defined in lambda.tf — Step Functions does not need to manage it.
#
# Retry logic:
#   Lambda transient errors (throttles, timeouts, SDK errors) are retried
#   up to 3 times with exponential backoff before the execution fails.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_sfn_state_machine" "source_pipeline" {
  for_each = toset(local.fetch_sources)

  name     = "${var.project}-${var.environment}-${each.key}"
  role_arn = aws_iam_role.stepfunctions_exec.arn

  definition = jsonencode({
    Comment = "WHAYNE ingestion pipeline for ${each.key}"
    StartAt = "Fetch"

    States = {
      # ── Fetch ────────────────────────────────────────────────────────────
      # Invokes the per-source Fetch Lambda.
      # Lambda writes raw payload to Bronze (R2) and enqueues a
      # claim-check message to SQS. SQS triggers Normalise automatically.
      Fetch = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"

        Parameters = {
          FunctionName = aws_lambda_function.fetch[each.key].arn
          "Payload.$"  = "$"
        }

        # Retry transient Lambda errors before giving up.
        # BackoffRate: each retry waits 2x longer than the previous.
        Retry = [{
          ErrorEquals = [
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException",
          ]
          IntervalSeconds = 5
          MaxAttempts     = 3
          BackoffRate     = 2
        }]

        # All other errors go directly to FetchFailed.
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "FetchFailed"
          ResultPath  = "$.error"
        }]

        Next = "FetchSucceeded"
      }

      # ── FetchSucceeded ───────────────────────────────────────────────────
      # Execution ends here on success.
      # SQS has the message; Normalise Lambda will pick it up automatically.
      FetchSucceeded = {
        Type = "Succeed"
      }

      # ── FetchFailed ──────────────────────────────────────────────────────
      # Execution ends here after retries are exhausted.
      # CloudWatch alarm on Step Functions execution failures will fire.
      # Check CloudWatch Logs → /aws/states/whayne-dev for details.
      FetchFailed = {
        Type  = "Fail"
        Error = "FetchFailed"
        Cause = "Fetch Lambda failed after retries — check CloudWatch Logs"
      }
    }
  })

  # Send execution logs to CloudWatch.
  # level = ERROR means only failed executions are logged (cost-efficient).
  # Change to ALL during initial testing to see every execution.
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.stepfunctions.arn}:*"
    include_execution_data = true
    level                  = "ERROR"
  }

  # X-Ray tracing — links Step Functions execution to Lambda invocations
  tracing_configuration {
    enabled = true
  }

  tags = {
    Component = "orchestration"
    Source    = each.key
  }
}


# ─────────────────────────────────────────────────────────────────────────────
# CLOUDWATCH ALARM — Step Functions execution failures
#
# Fires when any state machine execution fails.
# Catches both Fetch Lambda failures and Step Functions internal errors.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "stepfunctions_failures" {
  alarm_name          = "${var.project}-${var.environment}-sfn-failures"
  alarm_description   = "One or more Step Functions executions failed — check CloudWatch Logs for details."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ExecutionsFailed"
  namespace           = "AWS/States"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUTS
# ─────────────────────────────────────────────────────────────────────────────

output "state_machine_arns" {
  description = "Step Functions state machine ARNs — one per source"
  value       = { for k, v in aws_sfn_state_machine.source_pipeline : k => v.arn }
}