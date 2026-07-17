# ============================================================
# Retired mutable-source CodeBuild project
# ============================================================
# This project previously accepted codebuild/source.zip plus StartBuild
# overrides and could publish directly to the release ECR repositories. It is
# retained only so Terraform cannot accidentally recreate or destroy the live
# object during the guarded migration. Its desired state has no source,
# privileged Docker, runtime inputs, ECR credentials, or publish permission and
# always exits non-zero.
#
# A future signed builder must be a distinct project/repository quarantine path.
# The runtime guard accepts its result only as an immutable KMS-signed digest;
# this legacy project must never be repurposed.

data "aws_caller_identity" "cb" {}

locals {
  retired_codebuild_project_name = "${var.project_name}-${var.environment}-image-builder"
  retired_codebuild_buildspec    = <<-EOT
    version: 0.2
    phases:
      build:
        commands:
          - echo "RETIRED: mutable source.zip image publishing is disabled" >&2
          - exit 64
  EOT
}

# These groups were created implicitly by historical CodeBuild projects and
# already contain production audit data. Adopt them in-place: only retention is
# managed here, destruction is blocked, and any existing KMS association is
# deliberately preserved for a separately reviewed encryption migration.
resource "aws_cloudwatch_log_group" "codebuild_aiia_image_builder" {
  name              = "/aws/codebuild/${var.project_name}-${var.environment}-aiia-image-builder"
  retention_in_days = 30

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [kms_key_id]
  }
}

import {
  to = aws_cloudwatch_log_group.codebuild_aiia_image_builder
  id = "/aws/codebuild/teamagent-dev-aiia-image-builder"
}

resource "aws_cloudwatch_log_group" "codebuild_image_builder" {
  name              = "/aws/codebuild/${local.retired_codebuild_project_name}"
  retention_in_days = 30

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [kms_key_id]
  }
}

import {
  to = aws_cloudwatch_log_group.codebuild_image_builder
  id = "/aws/codebuild/teamagent-dev-image-builder"
}

data "aws_iam_policy_document" "codebuild_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "codebuild" {
  name               = "${var.project_name}-${var.environment}-codebuild-image"
  assume_role_policy = data.aws_iam_policy_document.codebuild_assume.json

  lifecycle {
    prevent_destroy = true
  }
}

data "aws_iam_policy_document" "codebuild" {
  statement {
    sid    = "DenyLegacyBuildAwsAccess"
    effect = "Deny"
    actions = [
      "codebuild:*",
      "ecr:*",
      "ecs:*",
      "iam:PassRole",
      "lambda:*",
      "s3:*",
      "secretsmanager:*",
      "ssm:*",
      "ssmmessages:*",
      "sts:AssumeRole",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "codebuild" {
  name   = "${var.project_name}-${var.environment}-codebuild-image"
  role   = aws_iam_role.codebuild.id
  policy = data.aws_iam_policy_document.codebuild.json

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_codebuild_project" "image" {
  name         = local.retired_codebuild_project_name
  description  = "RETIRED - mutable source.zip release publishing is denied"
  service_role = aws_iam_role.codebuild.arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
    type            = "ARM_CONTAINER"
    privileged_mode = false
  }

  source {
    type      = "NO_SOURCE"
    buildspec = local.retired_codebuild_buildspec
  }

  logs_config {
    cloudwatch_logs {
      status = "DISABLED"
    }
    s3_logs {
      status = "DISABLED"
    }
  }

  depends_on = [
    aws_iam_role_policy.codebuild,
    terraform_data.runtime_guard,
  ]

  lifecycle {
    prevent_destroy = true
  }
}

output "codebuild_project" {
  description = "Retired legacy project name; this is not an approved build entrypoint."
  value       = aws_codebuild_project.image.name
}
