# ─────────────────────────────────────────────────────────────────────────────
# EVENTBRIDGE SCHEDULER — per-source schedules
#
# Each schedule fires on its cadence and starts the source's
# Step Functions state machine, which runs the Fetch Lambda.
#
# Uses EventBridge Scheduler (newer, serverless) rather than
# EventBridge Rules — Scheduler supports flexible time windows
# and has a simpler IAM model for Step Functions targets.
#
# All schedules are DISABLED by default.
# Enable them after terraform apply + end-to-end testing:
#   AWS Console → EventBridge → Schedules → select → Enable
# OR via CLI:
#   aws scheduler update-schedule --name whayne-dev-pubmed \
#     --state ENABLED --schedule-expression "cron(0 2 * * ? *)" \
#     --target ... --flexible-time-window ...
# ─────────────────────────────────────────────────────────────────────────────

locals {
  # Per-source schedule expressions and descriptions.
  # Schedule expressions use EventBridge cron syntax:
  #   cron(minutes hours day-of-month month day-of-week year)
  #   ? = no specific value (required when day-of-month or day-of-week is set)
  source_schedules = {
    pubmed = {
      schedule    = "cron(0 2 * * ? *)"
      description = "PubMed — daily at 02:00 UTC"
    }
    clinicaltrials = {
      schedule    = "cron(0 3 * * ? *)"
      description = "ClinicalTrials — daily at 03:00 UTC"
    }
    fda_510k = {
      schedule    = "cron(0 4 * * ? *)"
      description = "FDA 510k — daily at 04:00 UTC"
    }
    cdsco = {
      schedule    = "cron(0 5 * * ? *)"
      description = "CDSCO — daily at 05:00 UTC"
    }
    eudamed = {
      schedule    = "cron(0 6 * * ? *)"
      description = "EUDAMED — daily at 06:00 UTC"
    }
    cochrane = {
      schedule    = "cron(0 7 * * ? *)"
      description = "Cochrane — daily at 07:00 UTC"
    }
    comtrade = {
      schedule    = "cron(0 1 1 * ? *)"
      description = "UN Comtrade — monthly on the 1st at 01:00 UTC"
    }
    rss = {
      schedule    = "rate(1 hour)"
      description = "RSS feeds — every hour"
    }
  }
}


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULES
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_scheduler_schedule" "source_pipeline" {
  for_each = local.source_schedules

  name        = "${var.project}-${var.environment}-${each.key}"
  description = each.value.description
  group_name  = "default"

  # Disabled by default — enable after end-to-end testing per source
  state = "DISABLED"

  schedule_expression          = each.value.schedule
  schedule_expression_timezone = "UTC"

  # Flexible time window: EventBridge may fire up to 10 minutes late.
  # Prevents all schedules from hitting AWS APIs at the exact same second.
  # Use mode = "OFF" if exact firing time is required.
  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 10
  }

  target {
    # Start the source's Step Functions state machine
    arn      = aws_sfn_state_machine.source_pipeline[each.key].arn
    role_arn = aws_iam_role.eventbridge_scheduler.arn

    # Pass source_id so the state machine knows which source to run
    input = jsonencode({
      source_id   = each.key
      environment = var.environment
    })

    # Retry failed executions up to 3 times over 1 hour
    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 3
    }
  }
}


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUTS
# ─────────────────────────────────────────────────────────────────────────────

output "schedule_names" {
  description = "EventBridge schedule names — enable these after end-to-end testing"
  value       = { for k, v in aws_scheduler_schedule.source_pipeline : k => v.name }
}

output "schedule_states" {
  description = "Current state of each schedule (DISABLED until tested)"
  value       = { for k, v in aws_scheduler_schedule.source_pipeline : k => v.state }
}