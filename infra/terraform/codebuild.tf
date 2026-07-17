# ============================================================
# CodeBuild: TeamAgent MCP candidateを **AWS内（社内proxy外）でbuild・scan gate**
# ============================================================
# go-live 実測: 社内proxy が PyTorch CDN(download.pytorch.org) の大容量DLを遮断し、
# ローカル docker build で torch(CPU) が取得できない。CodeBuild は AWS 内で走るため
# proxy を経由せず CDN に直結＝slim な CPU イメージを確実にビルドできる（再利用可能なCI基盤）。
# arm64 ネイティブ（ARM_CONTAINER）でビルドし qemu 不要。source は raw_files S3 の zip。

data "aws_caller_identity" "cb" {}

locals {
  expected_build_account_id     = "718959508629"
  expected_build_region         = "ap-northeast-1"
  codebuild_log_retention_days  = 30
  main_codebuild_project_name   = "${var.project_name}-${var.environment}-image-builder"
  runtime_contract_sha256       = filesha256("${path.module}/../codebuild/teamagent_runtime_contract.json")
  tiktok_codebuild_project_name = "${var.project_name}-${var.environment}-tiktok-image-builder"
  openclaw_codebuild_project_name = (
    "${var.project_name}-${var.environment}-openclaw-provenance-builder"
  )
  openclaw_launcher_role_name = "${var.project_name}-${var.environment}-openclaw-build-publisher"
  openclaw_evidence_bucket    = "${var.project_name}-${var.environment}-openclaw-build-evidence"
  openclaw_contract_sha256 = filesha256(
    "${path.module}/../codebuild/openclaw_bundle_contract.json"
  )
  launcher_role_name   = "teamagent-dev-codebuild-launcher"
  launcher_project_arn = "arn:aws:codebuild:ap-northeast-1:718959508629:project/teamagent-dev-image-builder"
  launcher_environment_names = [
    "GIT_COMMIT",
    "GIT_BRANCH",
    "IMAGE_TAG",
    "WITH_SCRAPE_TOOLS",
    "APP_HTML_VERSION_ID",
    "APP_HTML_SHA256",
    "RUNTIME_CONTRACT_SHA256",
  ]
  launcher_fixed_environment_values = {
    GIT_BRANCH              = "dev"
    WITH_SCRAPE_TOOLS       = "true"
    RUNTIME_CONTRACT_SHA256 = local.runtime_contract_sha256
  }
  # Official CodeBuild request condition keys. Each key receives its own Null
  # deny statement so the presence of any one dangerous override is rejected.
  # CodeBuild publishes no condition keys for debugSessionEnabled or timeout
  # overrides. Debug channels are denied below; timeout is not an authorization
  # boundary and cannot change the pinned source/buildspec/role/image/gates.
  launcher_denied_override_condition_keys = toset([
    "codebuild:source",
    "codebuild:source.buildspec",
    "codebuild:source.buildStatusConfig.context",
    "codebuild:source.buildStatusConfig.targetUrl",
    "codebuild:source.location",
    "codebuild:source.auth.resource",
    "codebuild:source.auth.type",
    "codebuild:source.insecureSsl",
    "codebuild:secondarySources",
    "codebuild:artifacts",
    "codebuild:secondaryArtifacts",
    "codebuild:environment.image",
    "codebuild:environment.type",
    "codebuild:environment.computeType",
    "codebuild:environment.computeConfiguration",
    "codebuild:environment.privilegedMode",
    "codebuild:environment.certificate",
    "codebuild:environment.registryCredential",
    "codebuild:environment.imagePullCredentialsType",
    "codebuild:environment.fleet.fleetArn",
    "codebuild:logsConfig",
    "codebuild:cache",
    "codebuild:serviceRole",
    "codebuild:encryptionKey",
    "codebuild:autoRetryLimit",
  ])
}

check "fixed_codebuild_account_and_region" {
  assert {
    condition = (
      data.aws_caller_identity.cb.account_id == local.expected_build_account_id &&
      var.aws_region == local.expected_build_region &&
      var.project_name == "teamagent" &&
      var.environment == "dev"
    )
    error_message = "CodeBuild image pipelines are fixed to teamagent/dev in AWS account 718959508629 and ap-northeast-1."
  }
}

# CodeBuild creates log groups with unlimited retention when they do not
# already exist. Manage every project log group explicitly at the same 30-day
# retention used by the application logs. The orphaned aiia-image-builder log
# group is retention-only; this does not recreate the retired CodeBuild project.
resource "aws_cloudwatch_log_group" "codebuild_image" {
  name              = "/aws/codebuild/${local.main_codebuild_project_name}"
  retention_in_days = local.codebuild_log_retention_days
}

resource "aws_cloudwatch_log_group" "codebuild_tiktok_image" {
  count             = local.tk_enabled
  name              = "/aws/codebuild/${local.tiktok_codebuild_project_name}"
  retention_in_days = local.codebuild_log_retention_days
}

resource "aws_cloudwatch_log_group" "codebuild_openclaw_provenance" {
  name              = "/aws/codebuild/${local.openclaw_codebuild_project_name}"
  retention_in_days = local.codebuild_log_retention_days
}

resource "aws_cloudwatch_log_group" "codebuild_aiia_image_legacy" {
  name              = "/aws/codebuild/aiia-image-builder"
  retention_in_days = local.codebuild_log_retention_days
}

# --- CodeBuild 用 IAM ロール（ECR push/scan gate / logs / S3 source 読取） ---
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
}

data "aws_iam_policy_document" "codebuild" {
  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "${aws_cloudwatch_log_group.codebuild_image.arn}:*",
    ]
  }
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # GetAuthorizationToken はリソース指定不可（AWS仕様）
  }
  statement {
    sid = "EcrMcpQuarantineWrite"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:DescribeImages",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:DescribeImageScanFindings",
    ]
    resources = [aws_ecr_repository.mcp_quarantine.arn]
  }
  statement {
    sid = "EcrMcpReleasePromotion"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:DescribeImages",
    ]
    resources = [aws_ecr_repository.mcp.arn]
  }
  statement {
    sid       = "DenyCodeBuildDebugChannels"
    effect    = "Deny"
    actions   = ["ssmmessages:*"]
    resources = ["*"]
  }
  statement {
    sid     = "S3Source"
    actions = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = [
      "${aws_s3_bucket.raw_files.arn}/codebuild/source.zip",
      "${aws_s3_bucket.raw_files.arn}/codebuild/connect-web-app.html",
    ]
  }
}

resource "aws_iam_role_policy" "codebuild" {
  name   = "${var.project_name}-${var.environment}-codebuild-image"
  role   = aws_iam_role.codebuild.id
  policy = data.aws_iam_policy_document.codebuild.json
}

# --- CodeBuild プロジェクト（arm64・docker・versioned S3 source） ---
resource "aws_codebuild_project" "image" {
  name         = local.main_codebuild_project_name
  description  = "Build and vulnerability-gate TeamAgent MCP candidate images inside AWS"
  service_role = aws_iam_role.codebuild.arn

  artifacts { type = "NO_ARTIFACTS" }

  environment {
    compute_type    = "BUILD_GENERAL1_LARGE"                           # torch/sentence-transformers ビルドに余裕
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0" # arm64 ネイティブ
    type            = "ARM_CONTAINER"
    privileged_mode = true # docker ビルドに必須

    # IMAGE_TAG / WITH_SCRAPE_TOOLS / GIT_COMMIT / GIT_BRANCH /
    # APP_HTML_VERSION_ID / APP_HTML_SHA256 / runtime contract values have no
    # project defaults.
    # build_teamagent_image.sh binds all provenance inputs per build.
  }

  source {
    type     = "S3"
    location = "${aws_s3_bucket.raw_files.id}/codebuild/source.zip"
    buildspec = replace(
      replace(
        replace(
          file("${path.module}/../codebuild/buildspec.yml"),
          "__SOURCE_PROVENANCE_SHA256__",
          filesha256("${path.module}/../codebuild/source_provenance.py"),
        ),
        "__ECR_IMAGE_RESOLVER_SHA256__",
        filesha256("${path.module}/../codebuild/resolve_ecr_image.py"),
      ),
      "__ECR_SCAN_GATE_SHA256__",
      filesha256("${path.module}/../codebuild/verify_ecr_scan.py"),
    )
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild_image.name
    }
  }
}

output "codebuild_project" {
  value = aws_codebuild_project.image.name
}

# ============================================================
# Human launcher boundary: AIIAdev must assume this role once.
# ============================================================

data "aws_iam_user" "aiia_dev" {
  user_name = "AIIAdev"
}

data "aws_iam_policy_document" "codebuild_launcher_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [data.aws_iam_user.aiia_dev.arn]
    }
  }
}

resource "aws_iam_role" "codebuild_launcher" {
  name                 = local.launcher_role_name
  assume_role_policy   = data.aws_iam_policy_document.codebuild_launcher_assume.json
  max_session_duration = 10800
}

data "aws_iam_policy_document" "codebuild_launcher" {
  statement {
    sid       = "ReadVersionedBuildInputs"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["arn:aws:s3:::teamagent-dev-raw-files/codebuild/connect-web-app.html"]
  }
  statement {
    sid       = "CheckBuildInputVersioning"
    actions   = ["s3:GetBucketVersioning"]
    resources = ["arn:aws:s3:::teamagent-dev-raw-files"]
  }
  statement {
    sid       = "WriteExactVersionedSourceKey"
    actions   = ["s3:PutObject"]
    resources = ["arn:aws:s3:::teamagent-dev-raw-files/codebuild/source.zip"]
  }
  statement {
    sid       = "StartExactProvenanceBuild"
    actions   = ["codebuild:StartBuild"]
    resources = [local.launcher_project_arn]

    condition {
      test     = "Null"
      variable = "codebuild:environment.environmentVariables.name"
      values   = ["false"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables.name"
      values   = local.launcher_environment_names
    }
    dynamic "condition" {
      for_each = local.launcher_fixed_environment_values
      content {
        test     = "StringEquals"
        variable = "codebuild:environment.environmentVariables/${condition.key}.value"
        values   = [condition.value]
      }
    }
  }
  statement {
    sid       = "PollExactProvenanceBuild"
    actions   = ["codebuild:BatchGetBuilds"]
    resources = [local.launcher_project_arn]
  }
  statement {
    sid = "ReadQuarantinedMcpDigest"
    actions = [
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
    ]
    resources = ["arn:aws:ecr:ap-northeast-1:718959508629:repository/teamagent-mcp-quarantine"]
  }
  statement {
    sid = "ReadPromotedMcpDigestAndConfig"
    actions = [
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = ["arn:aws:ecr:ap-northeast-1:718959508629:repository/teamagent-mcp"]
  }
  dynamic "statement" {
    for_each = local.launcher_denied_override_condition_keys
    content {
      effect    = "Deny"
      actions   = ["codebuild:StartBuild"]
      resources = [local.launcher_project_arn]
      condition {
        test     = "Null"
        variable = statement.value
        values   = ["false"]
      }
    }
  }
  statement {
    sid    = "DenyAlternateBuildEntryPoints"
    effect = "Deny"
    actions = [
      "codebuild:RetryBuild",
      "codebuild:RetryBuildBatch",
      "codebuild:StartBuildBatch",
      "codebuild:StartCommandExecution",
      "codebuild:StartSandbox",
      "codebuild:StartSandboxConnection",
    ]
    resources = ["*"]
  }
  # CodeBuild exposes no official StartBuild condition key for
  # debugSessionEnabled. Denying both sides of the Session Manager channel here
  # and on the CodeBuild service role makes that override unusable.
  statement {
    sid    = "DenyDebugSessionChannels"
    effect = "Deny"
    actions = [
      "ssm:StartSession",
      "ssmmessages:*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "codebuild_launcher" {
  name   = "teamagent-dev-codebuild-launcher"
  role   = aws_iam_role.codebuild_launcher.id
  policy = data.aws_iam_policy_document.codebuild_launcher.json
}

data "aws_iam_policy_document" "aiia_dev_no_direct_start_build" {
  statement {
    sid    = "RequireDedicatedLauncherRole"
    effect = "Deny"
    actions = [
      "codebuild:StartBuild",
      "codebuild:RetryBuild",
      "codebuild:RetryBuildBatch",
      "codebuild:StartBuildBatch",
      "codebuild:StartCommandExecution",
      "codebuild:StartSandbox",
      "codebuild:StartSandboxConnection",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_user_policy" "aiia_dev_no_direct_start_build" {
  name   = "require-teamagent-codebuild-launcher-role"
  user   = data.aws_iam_user.aiia_dev.user_name
  policy = data.aws_iam_policy_document.aiia_dev_no_direct_start_build.json
}

output "codebuild_launcher_role_arn" {
  value = aws_iam_role.codebuild_launcher.arn
}

# ============================================================
# TikTok worker: separate repository, project, role, and ECR boundary
# ============================================================
# tiktok-data-service is a separate Git repository. Its safe launcher must call
# start-build with a full main-branch commit as source-version and pass the same
# GIT_COMMIT/GIT_BRANCH values. The all-zero project default deliberately makes
# an argument-free build fail during source download instead of selecting latest.

resource "aws_codestarconnections_connection" "tiktok_codebuild" {
  count         = local.tk_enabled
  name          = "${var.project_name}-${var.environment}-tiktok-codebuild"
  provider_type = "GitHub"
}

resource "aws_iam_role" "tiktok_codebuild" {
  count              = local.tk_enabled
  name               = "${var.project_name}-${var.environment}-codebuild-tiktok-image"
  assume_role_policy = data.aws_iam_policy_document.codebuild_assume.json
}

data "aws_iam_policy_document" "tiktok_codebuild" {
  count = local.tk_enabled

  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "${aws_cloudwatch_log_group.codebuild_tiktok_image[0].arn}:*",
    ]
  }
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # GetAuthorizationToken はリソース指定不可（AWS仕様）
  }
  statement {
    sid = "GitHubSource"
    actions = [
      "codeconnections:GetConnection",
      "codeconnections:GetConnectionToken",
    ]
    resources = [aws_codestarconnections_connection.tiktok_codebuild[0].arn]
  }
  statement {
    sid = "TiktokEcrQuarantineWrite"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:DescribeImages",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:DescribeImageScanFindings",
    ]
    resources = [aws_ecr_repository.tiktok_acquire_quarantine[0].arn]
  }
  statement {
    sid = "TiktokEcrReleasePromotion"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:DescribeImages",
    ]
    resources = [aws_ecr_repository.tiktok_acquire[0].arn]
  }
  statement {
    sid       = "DenyCodeBuildDebugChannels"
    effect    = "Deny"
    actions   = ["ssmmessages:*"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "tiktok_codebuild" {
  count  = local.tk_enabled
  name   = "${var.project_name}-${var.environment}-codebuild-tiktok-image"
  role   = aws_iam_role.tiktok_codebuild[0].id
  policy = data.aws_iam_policy_document.tiktok_codebuild[0].json
}

resource "aws_codebuild_project" "tiktok_image" {
  count        = local.tk_enabled
  name         = local.tiktok_codebuild_project_name
  description  = "Build and zero-exception vulnerability-gate TikTok worker candidate images"
  service_role = aws_iam_role.tiktok_codebuild[0].arn
  source_version = (
    "0000000000000000000000000000000000000000" # unusable default; explicit source-version required
  )
  build_timeout = 120

  artifacts { type = "NO_ARTIFACTS" }

  environment {
    compute_type    = "BUILD_GENERAL1_LARGE"
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
    type            = "ARM_CONTAINER"
    privileged_mode = true

    # GIT_COMMIT/GIT_BRANCH deliberately have no defaults. The separate
    # tiktok-data-service launcher must bind both to the source-version commit.
  }

  source {
    type                = "GITHUB"
    location            = "https://github.com/noirelumiere00/tiktok-data-service.git"
    git_clone_depth     = 0
    report_build_status = false
    buildspec = replace(
      replace(
        file("${path.module}/../codebuild/tiktok-buildspec.yml"),
        "__ECR_SCAN_GATE_BASE64__",
        filebase64("${path.module}/../codebuild/verify_ecr_scan.py"),
      ),
      "__ECR_IMAGE_RESOLVER_BASE64__",
      filebase64("${path.module}/../codebuild/resolve_ecr_image.py"),
    )
    auth {
      type     = "CODECONNECTIONS"
      resource = aws_codestarconnections_connection.tiktok_codebuild[0].arn
    }
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild_tiktok_image[0].name
    }
  }
}

output "tiktok_codebuild_project" {
  value = local.tk_enabled == 1 ? aws_codebuild_project.tiktok_image[0].name : null
}

output "tiktok_codebuild_connection_arn" {
  description = "Complete the GitHub App handshake after apply; Terraform creates it PENDING."
  value = (
    local.tk_enabled == 1 ? aws_codestarconnections_connection.tiktok_codebuild[0].arn : null
  )
}

# ============================================================
# OpenClaw core/media: isolated source publisher + build boundary
# ============================================================

resource "aws_kms_key" "openclaw_evidence" {
  description             = "Encrypt immutable OpenClaw build evidence"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "openclaw_evidence" {
  name          = "alias/teamagent-dev-openclaw-build-evidence"
  target_key_id = aws_kms_key.openclaw_evidence.key_id
}

resource "aws_kms_key" "openclaw_publisher_signing" {
  description              = "Sign trusted OpenClaw source manifests and release evidence"
  deletion_window_in_days  = 30
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_3072"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "openclaw_publisher_signing" {
  name          = "alias/teamagent-dev-openclaw-build-publisher"
  target_key_id = aws_kms_key.openclaw_publisher_signing.key_id
}

resource "aws_s3_bucket" "openclaw_build_evidence" {
  bucket              = local.openclaw_evidence_bucket
  force_destroy       = false
  object_lock_enabled = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "openclaw_build_evidence" {
  bucket = aws_s3_bucket.openclaw_build_evidence.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "openclaw_build_evidence" {
  bucket = aws_s3_bucket.openclaw_build_evidence.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "openclaw_build_evidence" {
  bucket = aws_s3_bucket.openclaw_build_evidence.id
  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.openclaw_evidence.arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_object_lock_configuration" "openclaw_build_evidence" {
  bucket = aws_s3_bucket.openclaw_build_evidence.id

  depends_on = [aws_s3_bucket_versioning.openclaw_build_evidence]

  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = 30
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "openclaw_build_evidence" {
  bucket = aws_s3_bucket.openclaw_build_evidence.id

  depends_on = [aws_s3_bucket_object_lock_configuration.openclaw_build_evidence]

  rule {
    id     = "expire-after-audit-window"
    status = "Enabled"
    filter {}
    expiration {
      days = 365
    }
    noncurrent_version_expiration {
      noncurrent_days = 365
    }
  }
}

data "aws_iam_policy_document" "openclaw_build_evidence_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.openclaw_build_evidence.arn,
      "${aws_s3_bucket.openclaw_build_evidence.arn}/*",
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
  statement {
    sid       = "DenyUnencryptedEvidenceWrites"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.openclaw_build_evidence.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }
  statement {
    sid       = "DenyWrongEvidenceEncryptionKey"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.openclaw_build_evidence.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.openclaw_evidence.arn]
    }
  }
  statement {
    sid       = "DenyEvidenceWithoutComplianceLock"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.openclaw_build_evidence.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:object-lock-mode"
      values   = ["COMPLIANCE"]
    }
  }
  statement {
    sid    = "DenyEvidenceDeletion"
    effect = "Deny"
    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = ["${aws_s3_bucket.openclaw_build_evidence.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_s3_bucket_policy" "openclaw_build_evidence" {
  bucket = aws_s3_bucket.openclaw_build_evidence.id
  policy = data.aws_iam_policy_document.openclaw_build_evidence_bucket.json
}

resource "aws_codestarconnections_connection" "openclaw_codebuild" {
  name          = "${var.project_name}-${var.environment}-openclaw-codebuild"
  provider_type = "GitHub"
}

resource "aws_iam_role" "openclaw_codebuild" {
  name               = "${var.project_name}-${var.environment}-codebuild-openclaw"
  assume_role_policy = data.aws_iam_policy_document.codebuild_assume.json
}

data "aws_iam_policy_document" "openclaw_codebuild" {
  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "${aws_cloudwatch_log_group.codebuild_openclaw_provenance.arn}:*",
    ]
  }
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "GitHubSource"
    actions = [
      "codeconnections:GetConnection",
      "codeconnections:GetConnectionToken",
    ]
    resources = [aws_codestarconnections_connection.openclaw_codebuild.arn]
  }
  statement {
    sid = "OpenClawQuarantineOnlyBuildAndVerify"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:DescribeImageScanFindings",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [
      aws_ecr_repository.openclaw_quarantine.arn,
      aws_ecr_repository.openclaw_media_quarantine.arn,
    ]
  }
  statement {
    sid = "OpenClawReleasePromotionAfterGates"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [
      aws_ecr_repository.openclaw.arn,
      aws_ecr_repository.openclaw_media.arn,
    ]
  }
  statement {
    sid = "ReadVersionedSignedSourceManifest"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
    ]
    resources = [
      "${aws_s3_bucket.openclaw_build_evidence.arn}/source-manifests/*",
    ]
  }
  statement {
    sid       = "DecryptSourceManifestEvidence"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [aws_kms_key.openclaw_evidence.arn]
  }
  statement {
    sid       = "VerifyTrustedPublisherSignature"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Verify"]
    resources = [aws_kms_key.openclaw_publisher_signing.arn]
  }
  statement {
    sid    = "DenyS3WritesAndDeletes"
    effect = "Deny"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
      "s3:ReplicateObject",
    ]
    resources = [
      aws_s3_bucket.openclaw_build_evidence.arn,
      "${aws_s3_bucket.openclaw_build_evidence.arn}/*",
    ]
  }
  statement {
    sid       = "DenySigning"
    effect    = "Deny"
    actions   = ["kms:GenerateMac", "kms:Sign"]
    resources = ["*"]
  }
  statement {
    sid     = "DenyMcpRepositories"
    effect  = "Deny"
    actions = ["ecr:*"]
    resources = [
      aws_ecr_repository.mcp.arn,
      aws_ecr_repository.mcp_quarantine.arn,
    ]
  }
  statement {
    sid       = "DenyDebugChannels"
    effect    = "Deny"
    actions   = ["ssmmessages:*"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "openclaw_codebuild" {
  name   = "${var.project_name}-${var.environment}-codebuild-openclaw"
  role   = aws_iam_role.openclaw_codebuild.id
  policy = data.aws_iam_policy_document.openclaw_codebuild.json
}

resource "aws_codebuild_project" "openclaw_provenance" {
  name         = local.openclaw_codebuild_project_name
  description  = "Build and gate the signed OpenClaw arm64 core/media bundle"
  service_role = aws_iam_role.openclaw_codebuild.arn
  source_version = (
    "0000000000000000000000000000000000000000" # unusable without an explicit full SHA
  )
  build_timeout = 120

  artifacts { type = "NO_ARTIFACTS" }

  environment {
    compute_type    = "BUILD_GENERAL1_LARGE"
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
    type            = "ARM_CONTAINER"
    privileged_mode = true

    # No project environment variables: repository, registry, Trivy DB, contract,
    # account, and region are fixed in the embedded buildspec.
  }

  source {
    type                = "GITHUB"
    location            = "https://github.com/noirelumiere00/TeamAgent.git"
    git_clone_depth     = 0
    report_build_status = false
    buildspec = replace(
      replace(
        replace(
          replace(
            replace(
              file("${path.module}/../codebuild/openclaw-provenance-buildspec.yml"),
              "__OPENCLAW_PROVENANCE_SHA256__",
              filesha256("${path.module}/../codebuild/openclaw_provenance.py"),
            ),
            "__OPENCLAW_BUNDLE_CONTRACT_SHA256__",
            local.openclaw_contract_sha256,
          ),
          "__OPENCLAW_SCAN_GATE_SHA256__",
          filesha256("${path.module}/../codebuild/verify_ecr_scan.py"),
        ),
        "__OPENCLAW_SIGNING_KMS_KEY_ARN__",
        aws_kms_key.openclaw_publisher_signing.arn,
      ),
      "__OPENCLAW_EVIDENCE_KMS_KEY_ARN__",
      aws_kms_key.openclaw_evidence.arn,
    )
    auth {
      type     = "CODECONNECTIONS"
      resource = aws_codestarconnections_connection.openclaw_codebuild.arn
    }
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild_openclaw_provenance.name
    }
  }
}

data "aws_iam_policy_document" "openclaw_publisher_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [data.aws_iam_user.aiia_dev.arn]
    }
  }
}

resource "aws_iam_role" "openclaw_publisher" {
  name                 = local.openclaw_launcher_role_name
  assume_role_policy   = data.aws_iam_policy_document.openclaw_publisher_assume.json
  max_session_duration = 10800
}

data "aws_iam_policy_document" "openclaw_publisher" {
  statement {
    sid = "PublishAndReadImmutableEvidence"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
    ]
    resources = [
      "${aws_s3_bucket.openclaw_build_evidence.arn}/source-manifests/*",
      "${aws_s3_bucket.openclaw_build_evidence.arn}/release-evidence/*",
    ]
  }
  statement {
    sid = "CheckImmutableEvidenceBucket"
    actions = [
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketVersioning",
    ]
    resources = [aws_s3_bucket.openclaw_build_evidence.arn]
  }
  statement {
    sid       = "EncryptAndReadEvidence"
    actions   = ["kms:Decrypt", "kms:DescribeKey", "kms:Encrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.openclaw_evidence.arn]
  }
  statement {
    sid       = "SignTrustedPublisherEvidence"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Sign", "kms:Verify"]
    resources = [aws_kms_key.openclaw_publisher_signing.arn]
  }
  statement {
    sid       = "StartExactOpenClawBuild"
    actions   = ["codebuild:StartBuild"]
    resources = [aws_codebuild_project.openclaw_provenance.arn]
  }
  statement {
    sid       = "PollExactOpenClawBuild"
    actions   = ["codebuild:BatchGetBuilds"]
    resources = [aws_codebuild_project.openclaw_provenance.arn]
  }
  statement {
    sid = "ReadOpenClawReleaseAndQuarantineEvidence"
    actions = [
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [
      aws_ecr_repository.openclaw.arn,
      aws_ecr_repository.openclaw_quarantine.arn,
      aws_ecr_repository.openclaw_media.arn,
      aws_ecr_repository.openclaw_media_quarantine.arn,
    ]
  }
  statement {
    sid       = "DenyAnyStartBuildEnvironmentOverride"
    effect    = "Deny"
    actions   = ["codebuild:StartBuild"]
    resources = [aws_codebuild_project.openclaw_provenance.arn]
    condition {
      test     = "Null"
      variable = "codebuild:environment.environmentVariables.name"
      values   = ["false"]
    }
  }
  dynamic "statement" {
    for_each = local.launcher_denied_override_condition_keys
    content {
      effect    = "Deny"
      actions   = ["codebuild:StartBuild"]
      resources = [aws_codebuild_project.openclaw_provenance.arn]
      condition {
        test     = "Null"
        variable = statement.value
        values   = ["false"]
      }
    }
  }
  statement {
    sid    = "DenyOpenClawAlternateBuildEntryPoints"
    effect = "Deny"
    actions = [
      "codebuild:RetryBuild",
      "codebuild:RetryBuildBatch",
      "codebuild:StartBuildBatch",
      "codebuild:StartCommandExecution",
      "codebuild:StartSandbox",
      "codebuild:StartSandboxConnection",
      "ssm:StartSession",
      "ssmmessages:*",
    ]
    resources = ["*"]
  }
  statement {
    sid    = "DenyEvidenceDeletion"
    effect = "Deny"
    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = ["${aws_s3_bucket.openclaw_build_evidence.arn}/*"]
  }
}

resource "aws_iam_role_policy" "openclaw_publisher" {
  name   = local.openclaw_launcher_role_name
  role   = aws_iam_role.openclaw_publisher.id
  policy = data.aws_iam_policy_document.openclaw_publisher.json
}

output "openclaw_codebuild_project" {
  value = aws_codebuild_project.openclaw_provenance.name
}

output "openclaw_codebuild_connection_arn" {
  description = "Complete the GitHub App handshake after apply; Terraform creates it PENDING."
  value       = aws_codestarconnections_connection.openclaw_codebuild.arn
}

output "openclaw_publisher_role_arn" {
  value = aws_iam_role.openclaw_publisher.arn
}

output "openclaw_evidence_bucket" {
  value = aws_s3_bucket.openclaw_build_evidence.id
}
