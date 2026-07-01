# ─────────────────────────────────────────────────────────────────────────────
# LAMBDA EXECUTION ROLE
# Attached to every Lambda function in WHAYNE.
# Grants permissions to: write logs, read/write SQS, read SSM secrets,
# invoke Bedrock (LLM fallback), and send X-Ray traces.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_iam_role" "lambda_exec" {
  name = "${var.project}-${var.environment}-lambda-exec"

  # Trust policy — only Lambda service can assume this role
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Basic Lambda permissions — write logs to CloudWatch
resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# VPC access — needed if Lambda runs inside a VPC (e.g. to access RDS)
resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# X-Ray tracing
resource "aws_iam_role_policy_attachment" "lambda_xray" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

# Custom policy — SQS, SSM, and Bedrock access
resource "aws_iam_role_policy" "lambda_custom" {
  name = "${var.project}-${var.environment}-lambda-custom"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # SQS — send messages (adapters enqueue after Bronze write)
        # and receive/delete messages (normaliser Lambda polls queue)
        Sid    = "SQSAccess"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
        ]
        Resource = [
          aws_sqs_queue.ingestion.arn,
          aws_sqs_queue.ingestion_dlq.arn,
        ]
      },
      {
        # SSM Parameter Store — read R2 credentials, Neon connection string
        # All WHAYNE secrets live under /whayne/ prefix
        Sid    = "SSMReadAccess"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",
        ]
        Resource = "arn:aws:ssm:${var.aws_region}:*:parameter/${var.project}/*"
      },
      {
        # Bedrock — invoke Claude Haiku for LLM fallback and enrichment
        Sid      = "BedrockInvoke"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "arn:aws:bedrock:${var.aws_region}::foundation-model/anthropic.claude-haiku-*"
      },
    ]
  })
}


# ─────────────────────────────────────────────────────────────────────────────
# STEP FUNCTIONS EXECUTION ROLE
# Attached to every Step Functions state machine.
# Grants permissions to: invoke Lambda, send to SQS, write logs.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_iam_role" "stepfunctions_exec" {
  name = "${var.project}-${var.environment}-stepfunctions-exec"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "stepfunctions_custom" {
  name = "${var.project}-${var.environment}-stepfunctions-custom"
  role = aws_iam_role.stepfunctions_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Invoke any WHAYNE Lambda function
        Sid      = "InvokeLambda"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = "arn:aws:lambda:${var.aws_region}:*:function:${var.project}-${var.environment}-*"
      },
      {
        # Write execution logs to CloudWatch
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogDelivery",
          "logs:GetLogDelivery",
          "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery",
          "logs:ListLogDeliveries",
          "logs:PutResourcePolicy",
          "logs:DescribeResourcePolicies",
          "logs:DescribeLogGroups",
        ]
        Resource = "*"
      },
      {
        # X-Ray tracing for state machine executions
        Sid    = "XRayAccess"
        Effect = "Allow"
        Action = [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets",
        ]
        Resource = "*"
      },
    ]
  })
}


# ─────────────────────────────────────────────────────────────────────────────
# ECS FARGATE TASK ROLES
# Two roles required by Fargate (AWS enforces this split):
#   task_execution_role — used by ECS agent to pull image, write logs
#   task_role           — used by your actual container code at runtime
# ─────────────────────────────────────────────────────────────────────────────

# Task execution role — ECS agent pulls container image and writes logs
resource "aws_iam_role" "ecs_task_execution" {
  name = "${var.project}-${var.environment}-ecs-task-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_managed" {
  role       = aws_iam_role.ecs_task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task role — your container code (USPTO/Comtrade adapters) uses this at runtime
resource "aws_iam_role" "ecs_task" {
  name = "${var.project}-${var.environment}-ecs-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "ecs_task_custom" {
  name = "${var.project}-${var.environment}-ecs-task-custom"
  role = aws_iam_role.ecs_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SQSAccess"
        Effect = "Allow"
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
        ]
        Resource = [
          aws_sqs_queue.ingestion.arn,
          aws_sqs_queue.ingestion_dlq.arn,
        ]
      },
      {
        Sid    = "SSMReadAccess"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
          "ssm:GetParametersByPath",
        ]
        Resource = "arn:aws:ssm:${var.aws_region}:*:parameter/${var.project}/*"
      },
    ]
  })
}


# ─────────────────────────────────────────────────────────────────────────────
# EVENTBRIDGE SCHEDULER ROLE
# Lets EventBridge Scheduler trigger Step Functions state machines on schedule.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_iam_role" "eventbridge_scheduler" {
  name = "${var.project}-${var.environment}-eventbridge-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "eventbridge_scheduler_custom" {
  name = "${var.project}-${var.environment}-eventbridge-scheduler-custom"
  role = aws_iam_role.eventbridge_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "StartStepFunctions"
      Effect   = "Allow"
      Action   = ["states:StartExecution"]
      Resource = "arn:aws:states:${var.aws_region}:*:stateMachine:${var.project}-${var.environment}-*"
    }]
  })
}


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUTS — referenced by other .tf files
# ─────────────────────────────────────────────────────────────────────────────

output "lambda_exec_role_arn" {
  value = aws_iam_role.lambda_exec.arn
}

output "stepfunctions_exec_role_arn" {
  value = aws_iam_role.stepfunctions_exec.arn
}

output "ecs_task_execution_role_arn" {
  value = aws_iam_role.ecs_task_execution.arn
}

output "ecs_task_role_arn" {
  value = aws_iam_role.ecs_task.arn
}

output "eventbridge_scheduler_role_arn" {
  value = aws_iam_role.eventbridge_scheduler.arn
}