# ─────────────────────────────────────────────────────────────────────────────
# SSM PARAMETER STORE — WHAYNE secrets and config
#
# Terraform creates the parameter slots here with placeholder values.
# After terraform apply, fill in real values manually:
#   AWS Console → Systems Manager → Parameter Store → click each parameter → Edit
# OR via CLI:
#   aws ssm put-parameter --name "/whayne/r2_access_key_id" --value "your-real-value" --type SecureString --overwrite
#
# IMPORTANT: lifecycle { ignore_changes = [value] } on every parameter means
# Terraform will NOT reset your real values back to placeholders on future applies.
# ─────────────────────────────────────────────────────────────────────────────

locals {
  ssm_prefix = "/${var.project}"
}


# ── Cloudflare R2 credentials ─────────────────────────────────────────────────
# These match the env vars in storage.py exactly.
# SecureString = encrypted at rest by AWS KMS, hidden in console by default.

resource "aws_ssm_parameter" "r2_account_id" {
  name        = "${local.ssm_prefix}/r2_account_id"
  description = "Cloudflare R2 account ID"
  type        = "String"
  value       = "PLACEHOLDER"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "r2_access_key_id" {
  name        = "${local.ssm_prefix}/r2_access_key_id"
  description = "Cloudflare R2 access key ID (S3-compatible)"
  type        = "SecureString"
  value       = "PLACEHOLDER"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "r2_secret_access_key" {
  name        = "${local.ssm_prefix}/r2_secret_access_key"
  description = "Cloudflare R2 secret access key (S3-compatible)"
  type        = "SecureString"
  value       = "PLACEHOLDER"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "r2_bronze_bucket" {
  name        = "${local.ssm_prefix}/r2_bronze_bucket"
  description = "R2 Bronze bucket name"
  type        = "String"
  value       = "wine"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "r2_silver_bucket" {
  name        = "${local.ssm_prefix}/r2_silver_bucket"
  description = "R2 Silver bucket name"
  type        = "String"
  value       = "wine-silver"

  lifecycle {
    ignore_changes = [value]
  }
}


# ── Neon Postgres — Iceberg catalog ───────────────────────────────────────────
# SILVER_CATALOG_URI contains the full Neon connection string including password.
# Store as SecureString so it's encrypted and never visible in plain text.

resource "aws_ssm_parameter" "silver_catalog_uri" {
  name        = "${local.ssm_prefix}/silver_catalog_uri"
  description = "Neon Postgres connection string for PyIceberg SQL catalog"
  type        = "SecureString"
  value       = "PLACEHOLDER"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "silver_catalog_namespace" {
  name        = "${local.ssm_prefix}/silver_catalog_namespace"
  description = "Iceberg catalog namespace"
  type        = "String"
  value       = "whayne"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "silver_catalog_table" {
  name        = "${local.ssm_prefix}/silver_catalog_table"
  description = "Iceberg Silver table name"
  type        = "String"
  value       = "silver"

  lifecycle {
    ignore_changes = [value]
  }
}


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUTS
# ─────────────────────────────────────────────────────────────────────────────

output "ssm_prefix" {
  description = "SSM Parameter Store prefix for all WHAYNE parameters"
  value       = local.ssm_prefix
}

output "ssm_parameter_names" {
  description = "All SSM parameter names — fill these in after terraform apply"
  value = {
    r2_account_id            = aws_ssm_parameter.r2_account_id.name
    r2_access_key_id         = aws_ssm_parameter.r2_access_key_id.name
    r2_secret_access_key     = aws_ssm_parameter.r2_secret_access_key.name
    r2_bronze_bucket         = aws_ssm_parameter.r2_bronze_bucket.name
    r2_silver_bucket         = aws_ssm_parameter.r2_silver_bucket.name
    silver_catalog_uri       = aws_ssm_parameter.silver_catalog_uri.name
    silver_catalog_namespace = aws_ssm_parameter.silver_catalog_namespace.name
    silver_catalog_table     = aws_ssm_parameter.silver_catalog_table.name
  }
}