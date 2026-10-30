# Inventory-only builder, independent of the frozen image-release generations.
locals {
  apk_probe_project_name = "teamagent-dev-apk-probe"
  apk_probe_bucket_name  = "${local.apk_probe_project_name}-${local.expected_build_account_id}"
  apk_probe_project_arn  = "arn:aws:codebuild:${local.expected_build_region}:${local.expected_build_account_id}:project/${local.apk_probe_project_name}"
}

resource "aws_s3_bucket" "apk_probe" {
  bucket = local.apk_probe_bucket_name
}

resource "aws_s3_bucket_public_access_block" "apk_probe" {
  bucket = aws_s3_bucket.apk_probe.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "apk_probe" {
  bucket = aws_s3_bucket.apk_probe.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_iam_policy_document" "apk_probe_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.apk_probe.arn,
      "${aws_s3_bucket.apk_probe.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "apk_probe" {
  bucket = aws_s3_bucket.apk_probe.id
  policy = data.aws_iam_policy_document.apk_probe_bucket.json
}

resource "aws_cloudwatch_log_group" "apk_probe" {
  name              = "/aws/codebuild/${local.apk_probe_project_name}"
  retention_in_days = local.codebuild_log_retention_days
}

data "aws_iam_policy_document" "apk_probe_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.expected_build_account_id]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [local.apk_probe_project_arn]
    }
  }
}

resource "aws_iam_role" "apk_probe" {
  name               = local.apk_probe_project_name
  assume_role_policy = data.aws_iam_policy_document.apk_probe_assume.json
}

data "aws_iam_policy_document" "apk_probe" {
  statement {
    sid       = "EcrAuthorization"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "ChromiumMirrorPull"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchCheckLayerAvailability",
    ]
    resources = [
      "arn:aws:ecr:${local.expected_build_region}:${local.expected_build_account_id}:repository/teamagent-mirror/chromium-headless",
    ]
  }
  statement {
    sid       = "ReadProbeInputs"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.apk_probe.arn}/inputs/*"]
  }
  statement {
    sid       = "WriteProbeOutputs"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.apk_probe.arn}/outputs/*"]
  }
  statement {
    sid       = "ProbeLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.apk_probe.arn}:*"]
  }
}

resource "aws_iam_role_policy" "apk_probe" {
  role   = aws_iam_role.apk_probe.id
  policy = data.aws_iam_policy_document.apk_probe.json
}

resource "aws_codebuild_project" "apk_probe" {
  name                   = local.apk_probe_project_name
  description            = "Build the media runtime-packages stage and export its APK inventory only"
  service_role           = aws_iam_role.apk_probe.arn
  build_timeout          = 30
  queued_timeout         = 30
  concurrent_build_limit = 1

  artifacts { type = "NO_ARTIFACTS" }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
    type            = "ARM_CONTAINER"
    privileged_mode = true

    environment_variable {
      name  = "APK_PROBE_BUCKET"
      value = aws_s3_bucket.apk_probe.id
    }

    environment_variable {
      name  = "APK_PROBE_REGISTRY"
      value = "${local.expected_build_account_id}.dkr.ecr.${local.expected_build_region}.amazonaws.com"
    }

    environment_variable {
      name  = "APK_PROBE_RUN_ID"
      value = ""
    }
  }

  source {
    type      = "NO_SOURCE"
    buildspec = file("${path.module}/../codebuild/apk-probe-buildspec.yml")
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.apk_probe.name
    }
  }

  depends_on = [
    aws_iam_role_policy.apk_probe,
    aws_s3_bucket_public_access_block.apk_probe,
    aws_s3_bucket_server_side_encryption_configuration.apk_probe,
    aws_s3_bucket_policy.apk_probe,
    terraform_data.runtime_guard,
  ]

  lifecycle {
    precondition {
      condition = (
        var.project_name == "teamagent" &&
        var.environment == "dev" &&
        var.aws_region == local.expected_build_region &&
        data.aws_caller_identity.cb.account_id == local.expected_build_account_id
      )
      error_message = "The APK probe is fixed to teamagent/dev in account 718959508629 and ap-northeast-1."
    }
  }
}

output "apk_probe_project_name" {
  value = aws_codebuild_project.apk_probe.name
}

output "apk_probe_bucket_name" {
  value = aws_s3_bucket.apk_probe.id
}
