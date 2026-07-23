# ============================================================
# CodeBuild: TeamAgent MCP candidateを **AWS内（社内proxy外）でbuild・scan gate**
# ============================================================
# The existing project is adopted in place as a quarantine-only builder. It
# cannot write release repositories; independent source publication, attestation,
# promotion, release authorization, and the composed Terraform guard remain
# separate authorization boundaries.

data "aws_caller_identity" "cb" {}

locals {
  expected_build_account_id     = "718959508629"
  expected_build_region         = "ap-northeast-1"
  codebuild_log_retention_days  = 30
  main_codebuild_project_name   = "${var.project_name}-${var.environment}-image-builder"
  runtime_contract_sha256       = filesha256("${path.module}/../codebuild/teamagent_runtime_contract.json")
  mcp_release_contract_path     = "${path.module}/../codebuild/teamagent_core_media_release_contract.json"
  mcp_release_contract          = jsondecode(file(local.mcp_release_contract_path))
  mcp_release_contract_sha256   = filesha256(local.mcp_release_contract_path)
  tiktok_codebuild_project_name = "${var.project_name}-${var.environment}-tiktok-image-builder"
  openclaw_codebuild_project_name = (
    "${var.project_name}-${var.environment}-openclaw-provenance-builder"
  )
  openclaw_launcher_role_name = "${var.project_name}-${var.environment}-openclaw-build-publisher"
  openclaw_evidence_bucket    = "${var.project_name}-${var.environment}-openclaw-build-evidence"
  mcp_source_publisher_project_name = (
    "${var.project_name}-${var.environment}-mcp-source-publisher"
  )
  image_attestor_project_name = "${var.project_name}-${var.environment}-image-attestor"
  image_promoter_project_name = "${var.project_name}-${var.environment}-image-promoter"
  release_evidence_bucket     = "${var.project_name}-${var.environment}-image-release-evidence"
  tiktok_image_buildspec_s3_key = (
    "codebuild-buildspecs/${local.tiktok_codebuild_project_name}.yml"
  )
  mcp_source_publisher_buildspec_s3_key = (
    "codebuild-buildspecs/${local.mcp_source_publisher_project_name}.yml"
  )
  image_attestor_buildspec_s3_key = (
    "codebuild-buildspecs/${local.image_attestor_project_name}.yml"
  )
  image_promoter_buildspec_s3_key = (
    "codebuild-buildspecs/${local.image_promoter_project_name}.yml"
  )
  # A fixed horizon avoids perpetual S3 object updates from timestamp()-based
  # retention while satisfying the evidence bucket's explicit COMPLIANCE lock.
  codebuild_buildspec_retain_until_date = "2099-12-31T23:59:59Z"
  tiktok_launcher_role_name             = "${var.project_name}-${var.environment}-tiktok-build-launcher"
  release_launcher_role_name            = "${var.project_name}-${var.environment}-release-launcher"
  release_control_updater_role_name = (
    "${var.project_name}-${var.environment}-release-control-updater"
  )
  image_deployment_gate_role_name = (
    "${var.project_name}-${var.environment}-image-deployment-gate"
  )
  image_deployment_intent_table = (
    "${var.project_name}-${var.environment}-image-deployment-intents"
  )
  terraform_automation_role_arn = (
    "arn:aws:iam::718959508629:role/teamagent-dev-terraform-runtime-automation"
  )
  openclaw_contract_sha256 = filesha256(
    "${path.module}/../codebuild/openclaw_bundle_contract.json"
  )
  launcher_role_name              = "teamagent-dev-codebuild-launcher"
  launcher_project_arn            = "arn:aws:codebuild:ap-northeast-1:718959508629:project/teamagent-dev-image-builder"
  canonical_app_html_version_id   = local.mcp_release_contract.app_html.production.app_html_s3_version_id
  canonical_app_html_sha256       = local.mcp_release_contract.app_html.production.app_html_sha256
  canonical_vault_manifest_sha256 = local.mcp_release_contract.app_html.production.vault_manifest_sha256
  canonical_build_inputs_sha256   = local.mcp_release_contract.app_html.production.build_inputs_sha256
  canonical_baked_app_html_version_id = coalesce(
    local.mcp_release_contract.app_html.baked_fallback.s3_version_id,
    "__RELEASE_BLOCKED_MISSING_VERSION_ID__",
  )
  canonical_baked_app_html_sha256 = local.mcp_release_contract.app_html.baked_fallback.sha256
  canonical_app_provenance = {
    schema_version = 1
    app_html = {
      bucket     = local.mcp_release_contract.app_html.bucket
      key        = local.mcp_release_contract.app_html.key
      version_id = local.canonical_app_html_version_id
      sha256     = local.canonical_app_html_sha256
    }
    application_provenance = {
      vault_manifest_sha256 = local.canonical_vault_manifest_sha256
      build_inputs_sha256   = local.canonical_build_inputs_sha256
    }
    baked_fallback = {
      version_id = local.mcp_release_contract.app_html.baked_fallback.s3_version_id
      sha256     = local.canonical_baked_app_html_sha256
    }
  }
  canonical_app_provenance_sha256 = sha256(
    "${jsonencode(local.canonical_app_provenance)}\n"
  )
  launcher_environment_names = [
    "GIT_COMMIT",
    "GIT_BRANCH",
    "APP_HTML_VERSION_ID",
    "APP_HTML_SHA256",
    "VAULT_MANIFEST_SHA256",
    "BUILD_INPUTS_SHA256",
    "BAKED_APP_HTML_VERSION_ID",
    "BAKED_APP_HTML_SHA256",
    "APP_PROVENANCE_SHA256",
    "SOURCE_MANIFEST_CONTRACT_SHA256",
    "RELEASE_CONTRACT_SHA256",
    "SOURCE_ARCHIVE_VERSION_ID",
    "SOURCE_DECLARATION_KEY",
    "SOURCE_DECLARATION_VERSION_ID",
    "SOURCE_DECLARATION_SHA256",
    "SOURCE_DECLARATION_SIGNATURE_KEY",
    "SOURCE_DECLARATION_SIGNATURE_VERSION_ID",
  ]
  source_publisher_environment_names = [
    "EXPECTED_COMMIT",
    "EXPECTED_BASE_OID",
    "SOURCE_MANIFEST_CONTRACT_SHA256",
    "RELEASE_CONTRACT_SHA256",
  ]
  attestor_environment_names = [
    "PIPELINE",
    "PROMOTION_CHANNEL",
    "SOURCE_COMMIT",
    "CONTRACT_SHA256",
    "SOURCE_EVIDENCE_BUCKET",
    "SOURCE_EVIDENCE_KEY",
    "SOURCE_EVIDENCE_VERSION_ID",
    "SOURCE_EVIDENCE_SHA256",
    "SOURCE_EVIDENCE_SIGNATURE_KEY",
    "SOURCE_EVIDENCE_SIGNATURE_VERSION_ID",
    "BUILD_ID",
    "SUBJECTS_JSON",
  ]
  release_attestor_environment_names = concat(local.attestor_environment_names, [
    "CANDIDATE_RECEIPT_KEY",
    "CANDIDATE_RECEIPT_VERSION_ID",
    "CANDIDATE_RECEIPT_SIGNATURE_KEY",
    "CANDIDATE_RECEIPT_SIGNATURE_VERSION_ID",
  ])
  promoter_environment_names = [
    "PIPELINE",
    "PROMOTION_CHANNEL",
    "SOURCE_COMMIT",
    "CONTRACT_SHA256",
    "RECEIPT_KEY",
    "RECEIPT_VERSION_ID",
    "RECEIPT_SIGNATURE_KEY",
    "RECEIPT_SIGNATURE_VERSION_ID",
  ]
  tiktok_image_environment_names = [
    "GIT_COMMIT",
    "GIT_BRANCH",
    "TIKTOK_CONTRACT_SHA256",
    "SOURCE_MANIFEST_KEY",
    "SOURCE_MANIFEST_VERSION_ID",
    "SOURCE_MANIFEST_SHA256",
    "SOURCE_MANIFEST_SIGNATURE_KEY",
    "SOURCE_MANIFEST_SIGNATURE_VERSION_ID",
  ]
  launcher_all_project_arns = [
    "arn:aws:codebuild:ap-northeast-1:718959508629:project/teamagent-dev-image-builder",
    "arn:aws:codebuild:ap-northeast-1:718959508629:project/teamagent-dev-mcp-source-publisher",
    "arn:aws:codebuild:ap-northeast-1:718959508629:project/teamagent-dev-image-attestor",
    "arn:aws:codebuild:ap-northeast-1:718959508629:project/teamagent-dev-image-promoter",
  ]
  launcher_fixed_environment_values = {
    GIT_BRANCH                      = "dev"
    APP_HTML_VERSION_ID             = local.canonical_app_html_version_id
    APP_HTML_SHA256                 = local.canonical_app_html_sha256
    VAULT_MANIFEST_SHA256           = local.canonical_vault_manifest_sha256
    BUILD_INPUTS_SHA256             = local.canonical_build_inputs_sha256
    BAKED_APP_HTML_VERSION_ID       = local.canonical_baked_app_html_version_id
    BAKED_APP_HTML_SHA256           = local.canonical_baked_app_html_sha256
    APP_PROVENANCE_SHA256           = local.canonical_app_provenance_sha256
    SOURCE_MANIFEST_CONTRACT_SHA256 = local.runtime_contract_sha256
    RELEASE_CONTRACT_SHA256         = local.mcp_release_contract_sha256
  }
  # Official CodeBuild request condition keys. Each key receives its own Null
  # deny statement so the presence of any one dangerous override is rejected.
  # CodeBuild publishes no condition keys for debugSessionEnabled or timeout
  # overrides. Debug channels are denied below; timeout is not an authorization
  # boundary and cannot change the pinned source/buildspec/role/image/gates.
  launcher_denied_override_condition_keys_manage_a = toset([
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
  ])
  launcher_denied_override_condition_keys_manage_b = toset([
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
  ])
  launcher_denied_override_condition_keys_guardrails = toset([
    "codebuild:autoRetryLimit",
  ])
  launcher_denied_override_condition_keys = setunion(
    local.launcher_denied_override_condition_keys_manage_a,
    local.launcher_denied_override_condition_keys_manage_b,
    local.launcher_denied_override_condition_keys_guardrails,
  )
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

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [kms_key_id]
  }
}

import {
  to = aws_cloudwatch_log_group.codebuild_image
  id = "/aws/codebuild/teamagent-dev-image-builder"
}

# Historical production log group with a distinct name. Manage retention only;
# do not recreate or destroy the existing evidence-bearing group.
resource "aws_cloudwatch_log_group" "codebuild_aiia_image_builder" {
  name              = "/aws/codebuild/${var.project_name}-${var.environment}-aiia-image-builder"
  retention_in_days = local.codebuild_log_retention_days

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

resource "aws_cloudwatch_log_group" "codebuild_tiktok_image" {
  count             = local.tk_enabled
  name              = "/aws/codebuild/${local.tiktok_codebuild_project_name}"
  retention_in_days = local.codebuild_log_retention_days
}

resource "aws_cloudwatch_log_group" "codebuild_openclaw_provenance" {
  name              = "/aws/codebuild/${local.openclaw_codebuild_project_name}"
  retention_in_days = local.codebuild_log_retention_days
}

resource "aws_cloudwatch_log_group" "codebuild_mcp_source_publisher" {
  name              = "/aws/codebuild/${local.mcp_source_publisher_project_name}"
  retention_in_days = local.codebuild_log_retention_days
}

resource "aws_cloudwatch_log_group" "codebuild_image_attestor" {
  name              = "/aws/codebuild/${local.image_attestor_project_name}"
  retention_in_days = local.codebuild_log_retention_days
}

resource "aws_cloudwatch_log_group" "codebuild_image_promoter" {
  name              = "/aws/codebuild/${local.image_promoter_project_name}"
  retention_in_days = local.codebuild_log_retention_days
}

resource "aws_cloudwatch_log_group" "codebuild_aiia_image_legacy" {
  name              = "/aws/codebuild/aiia-image-builder"
  retention_in_days = local.codebuild_log_retention_days
}

# --- CodeBuild 用 IAM ロール（ECR push/scan gate / logs / S3 source 読取） ---
data "aws_iam_policy_document" "main_codebuild_assume" {
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
      values = [
        "arn:aws:codebuild:${local.expected_build_region}:${local.expected_build_account_id}:project/${local.main_codebuild_project_name}",
      ]
    }
  }
}

resource "aws_iam_role" "codebuild" {
  name               = "${var.project_name}-${var.environment}-codebuild-image"
  assume_role_policy = data.aws_iam_policy_document.main_codebuild_assume.json

  lifecycle {
    prevent_destroy = true
  }
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
    resources = ["*"]
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
    resources = [
      aws_ecr_repository.mcp_quarantine.arn,
      aws_ecr_repository.mcp_media_quarantine.arn,
    ]
  }
  statement {
    sid    = "DenyMcpCandidateAndReleaseWrite"
    effect = "Deny"
    actions = [
      "ecr:BatchDeleteImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [
      aws_ecr_repository.mcp_verified_candidates.arn,
      aws_ecr_repository.mcp.arn,
      aws_ecr_repository.mcp_media_verified_candidates.arn,
      aws_ecr_repository.mcp_media.arn,
    ]
  }
  statement {
    sid    = "DenyDynamicEnvironmentAndDebugChannels"
    effect = "Deny"
    actions = [
      "secretsmanager:GetSecretValue",
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssmmessages:*",
    ]
    resources = ["*"]
  }
  statement {
    sid     = "S3Source"
    actions = ["s3:GetObject", "s3:GetObjectRetention", "s3:GetObjectVersion"]
    resources = [
      "${aws_s3_bucket.raw_files.arn}/codebuild/source.zip",
      "${aws_s3_bucket.raw_files.arn}/codebuild/connect-web-app.html",
      "${aws_s3_bucket.raw_files.arn}/codebuild/baked-fallback/connect-web-app.html",
      "${aws_s3_bucket.image_release_evidence.arn}/source-declarations/mcp/*",
      "${aws_s3_bucket.image_release_evidence.arn}/source-contexts/mcp/*",
    ]
  }
  statement {
    sid       = "DecryptAndVerifySignedMcpSource"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [aws_kms_key.image_release_evidence.arn]
  }
  statement {
    sid       = "VerifyIndependentMcpSourcePublisher"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Verify"]
    resources = [aws_kms_key.mcp_source_publisher_signing.arn]
  }
  statement {
    sid    = "DenySourceEvidenceWritesAndSigning"
    effect = "Deny"
    actions = [
      "kms:Sign",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
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

locals {
  image_builder_buildspec_1 = replace(
    file("${path.module}/../codebuild/buildspec.yml"),
    "__SOURCE_PROVENANCE_SHA256__",
    filesha256("${path.module}/../codebuild/source_provenance.py"),
  )
  image_builder_buildspec_2 = replace(
    local.image_builder_buildspec_1,
    "__RELEASE_EVIDENCE_SHA256__",
    filesha256("${path.module}/../codebuild/release_evidence.py"),
  )
  image_builder_buildspec_3 = replace(
    local.image_builder_buildspec_2,
    "__TEAMAGENT_BUNDLE_PROVENANCE_SHA256__",
    filesha256("${path.module}/../codebuild/teamagent_bundle_provenance.py"),
  )
  image_builder_buildspec_4 = replace(
    local.image_builder_buildspec_3,
    "__SOURCE_PUBLISHER_SIGNING_KEY_ARN__",
    aws_kms_key.mcp_source_publisher_signing.arn,
  )
  image_builder_buildspec_5 = replace(
    local.image_builder_buildspec_4,
    "__RELEASE_EVIDENCE_KMS_KEY_ARN__",
    aws_kms_key.image_release_evidence.arn,
  )
  image_builder_buildspec_6 = replace(
    local.image_builder_buildspec_5,
    "__ECR_IMAGE_RESOLVER_SHA256__",
    filesha256("${path.module}/../codebuild/resolve_ecr_image.py"),
  )
  image_builder_buildspec_7 = replace(
    local.image_builder_buildspec_6,
    "__ECR_SCAN_GATE_SHA256__",
    filesha256("${path.module}/../codebuild/verify_ecr_scan.py"),
  )
  image_builder_buildspec_8 = replace(
    local.image_builder_buildspec_7,
    "__MCP_RELEASE_CONTRACT_SHA256__",
    local.mcp_release_contract_sha256,
  )
  image_builder_buildspec = replace(
    local.image_builder_buildspec_8,
    "__SOURCE_MANIFEST_CONTRACT_SHA256__",
    local.runtime_contract_sha256,
  )
}

# Native ARM64 quarantine-only builder. The exact versioned source archive and
# signed declaration are supplied per build by the independent publisher.
resource "aws_codebuild_project" "image" {
  name         = local.main_codebuild_project_name
  description  = "Build and vulnerability-gate TeamAgent MCP candidate images inside AWS"
  service_role = aws_iam_role.codebuild.arn

  artifacts { type = "NO_ARTIFACTS" }

  environment {
    compute_type    = "BUILD_GENERAL1_LARGE"
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
    type            = "ARM_CONTAINER"
    privileged_mode = true
  }

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.raw_files.id}/codebuild/source.zip"
    buildspec = local.image_builder_buildspec
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild_image.name
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
  description = "Native ARM64 quarantine-only MCP builder project name."
  value       = aws_codebuild_project.image.name
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

  statement {
    actions = [
      "sts:AssumeRole",
      "sts:SetSourceIdentity",
    ]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::718959508629:root"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::718959508629:root"]
    }
    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:RoleSessionName"
      values   = ["teamagent-build-launcher"]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:SourceIdentity"
      values   = ["teamagent-production-build"]
    }
  }
}

resource "aws_iam_role" "codebuild_launcher" {
  name                 = local.launcher_role_name
  assume_role_policy   = data.aws_iam_policy_document.codebuild_launcher_assume.json
  max_session_duration = 10800
}

data "aws_iam_policy_document" "codebuild_launcher_core" {
  statement {
    sid = "RequireAvailableTeamAgentCodeConnection"
    actions = [
      "codeconnections:GetConnection",
      "codeconnections:ListConnections",
    ]
    resources = ["*"]
  }
  statement {
    sid     = "ReadVersionedBuildInputs"
    actions = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = [
      "arn:aws:s3:::teamagent-dev-raw-files/codebuild/connect-web-app.html",
      "arn:aws:s3:::teamagent-dev-raw-files/codebuild/baked-fallback/connect-web-app.html",
    ]
  }
  statement {
    sid       = "CheckBuildInputVersioning"
    actions   = ["s3:GetBucketVersioning"]
    resources = ["arn:aws:s3:::teamagent-dev-raw-files"]
  }
  statement {
    sid = "ReadImmutableSourceEvidence"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
    ]
    resources = [
      "arn:aws:s3:::teamagent-dev-image-release-evidence/source-declarations/mcp/*",
    ]
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
        test     = "ForAllValues:StringEquals"
        variable = "codebuild:environment.environmentVariables/${condition.key}.value"
        values   = [condition.value]
      }
    }
  }
  statement {
    sid       = "PollExactProvenanceBuild"
    actions   = ["codebuild:BatchGetBuilds"]
    resources = local.launcher_all_project_arns
  }
  statement {
    sid       = "StartIndependentSourcePublisher"
    actions   = ["codebuild:StartBuild"]
    resources = [local.launcher_all_project_arns[1]]
    condition {
      test     = "Null"
      variable = "codebuild:environment.environmentVariables.name"
      values   = ["false"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables.name"
      values   = local.source_publisher_environment_names
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/SOURCE_MANIFEST_CONTRACT_SHA256.value"
      values   = [local.runtime_contract_sha256]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/RELEASE_CONTRACT_SHA256.value"
      values   = [local.mcp_release_contract_sha256]
    }
  }
  statement {
    sid       = "StartSourceFreeAttestor"
    actions   = ["codebuild:StartBuild"]
    resources = [local.launcher_all_project_arns[2]]
    condition {
      test     = "Null"
      variable = "codebuild:environment.environmentVariables.name"
      values   = ["false"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables.name"
      values   = local.attestor_environment_names
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PIPELINE.value"
      values   = ["mcp"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PROMOTION_CHANNEL.value"
      values   = ["verified-candidate"]
    }
  }
  statement {
    sid       = "StartSourceFreePromoter"
    actions   = ["codebuild:StartBuild"]
    resources = [local.launcher_all_project_arns[3]]
    condition {
      test     = "Null"
      variable = "codebuild:environment.environmentVariables.name"
      values   = ["false"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables.name"
      values   = local.promoter_environment_names
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PIPELINE.value"
      values   = ["mcp"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PROMOTION_CHANNEL.value"
      values   = ["verified-candidate"]
    }
  }
}

data "aws_iam_policy_document" "codebuild_launcher_manage_a" {
  dynamic "statement" {
    for_each = local.launcher_denied_override_condition_keys_manage_a
    content {
      effect    = "Deny"
      actions   = ["codebuild:StartBuild"]
      resources = local.launcher_all_project_arns
      condition {
        test     = "Null"
        variable = statement.value
        values   = ["false"]
      }
    }
  }
}

data "aws_iam_policy_document" "codebuild_launcher_manage_b" {
  dynamic "statement" {
    for_each = local.launcher_denied_override_condition_keys_manage_b
    content {
      effect    = "Deny"
      actions   = ["codebuild:StartBuild"]
      resources = local.launcher_all_project_arns
      condition {
        test     = "Null"
        variable = statement.value
        values   = ["false"]
      }
    }
  }
}

data "aws_iam_policy_document" "codebuild_launcher_guardrails" {
  statement {
    sid     = "ReadVerifiedMcpBundleDigests"
    actions = ["ecr:DescribeImages"]
    resources = [
      "arn:aws:ecr:ap-northeast-1:718959508629:repository/teamagent-mcp-verified-candidates",
      "arn:aws:ecr:ap-northeast-1:718959508629:repository/teamagent-media-worker-verified-candidates",
    ]
  }
  dynamic "statement" {
    for_each = local.launcher_denied_override_condition_keys_guardrails
    content {
      effect    = "Deny"
      actions   = ["codebuild:StartBuild"]
      resources = local.launcher_all_project_arns
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
  statement {
    sid     = "VerifySignedEvidence"
    actions = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Verify"]
    resources = [
      aws_kms_key.mcp_source_publisher_signing.arn,
      aws_kms_key.image_attestor_signing.arn,
    ]
  }
  statement {
    sid    = "DenyEvidenceWritesSigningAndReleaseWrites"
    effect = "Deny"
    actions = [
      "ecr:BatchDeleteImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "kms:Sign",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "codebuild_launcher_core" {
  name   = "${local.launcher_role_name}-core"
  policy = data.aws_iam_policy_document.codebuild_launcher_core.json

  lifecycle {
    precondition {
      condition     = length(replace(data.aws_iam_policy_document.codebuild_launcher_core.json, "/\\s/", "")) < 6144
      error_message = "CodeBuild launcher core policy must remain below 6,144 non-whitespace characters (AWS ignores whitespace when measuring IAM policy size)."
    }
  }
}

resource "aws_iam_policy" "codebuild_launcher_manage_a" {
  name   = "${local.launcher_role_name}-manage-a"
  policy = data.aws_iam_policy_document.codebuild_launcher_manage_a.json

  lifecycle {
    precondition {
      condition     = length(replace(data.aws_iam_policy_document.codebuild_launcher_manage_a.json, "/\\s/", "")) < 6144
      error_message = "CodeBuild launcher manage-a policy must remain below 6,144 non-whitespace characters (AWS ignores whitespace when measuring IAM policy size)."
    }
  }
}

resource "aws_iam_policy" "codebuild_launcher_manage_b" {
  name   = "${local.launcher_role_name}-manage-b"
  policy = data.aws_iam_policy_document.codebuild_launcher_manage_b.json

  lifecycle {
    precondition {
      condition     = length(replace(data.aws_iam_policy_document.codebuild_launcher_manage_b.json, "/\\s/", "")) < 6144
      error_message = "CodeBuild launcher manage-b policy must remain below 6,144 non-whitespace characters (AWS ignores whitespace when measuring IAM policy size)."
    }
  }
}

resource "aws_iam_policy" "codebuild_launcher_guardrails" {
  name   = "${local.launcher_role_name}-guardrails"
  policy = data.aws_iam_policy_document.codebuild_launcher_guardrails.json

  lifecycle {
    precondition {
      condition     = length(replace(data.aws_iam_policy_document.codebuild_launcher_guardrails.json, "/\\s/", "")) < 6144
      error_message = "CodeBuild launcher guardrails policy must remain below 6,144 non-whitespace characters (AWS ignores whitespace when measuring IAM policy size)."
    }
  }
}

resource "aws_iam_role_policy_attachment" "codebuild_launcher_core" {
  role       = aws_iam_role.codebuild_launcher.name
  policy_arn = aws_iam_policy.codebuild_launcher_core.arn
}

resource "aws_iam_role_policy_attachment" "codebuild_launcher_manage_a" {
  role       = aws_iam_role.codebuild_launcher.name
  policy_arn = aws_iam_policy.codebuild_launcher_manage_a.arn
}

resource "aws_iam_role_policy_attachment" "codebuild_launcher_manage_b" {
  role       = aws_iam_role.codebuild_launcher.name
  policy_arn = aws_iam_policy.codebuild_launcher_manage_b.arn
}

resource "aws_iam_role_policy_attachment" "codebuild_launcher_guardrails" {
  role       = aws_iam_role.codebuild_launcher.name
  policy_arn = aws_iam_policy.codebuild_launcher_guardrails.arn
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

# Separate human boundary for active/rollback authorization. It can only start
# the fixed source-free attestor/promoter projects and cannot write ECR/S3/KMS.
resource "aws_iam_user" "release_caller" {
  name = "teamagent-release-caller"
}

data "aws_iam_policy_document" "release_launcher_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_user.release_caller.arn]
    }
  }

  statement {
    actions = [
      "sts:AssumeRole",
      "sts:SetSourceIdentity",
    ]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::718959508629:root"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::718959508629:root"]
    }
    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:RoleSessionName"
      values   = ["teamagent-release-authorization"]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:SourceIdentity"
      values   = ["teamagent-production-release"]
    }
  }
}

resource "aws_iam_role" "release_launcher" {
  name                 = local.release_launcher_role_name
  assume_role_policy   = data.aws_iam_policy_document.release_launcher_assume.json
  max_session_duration = 10800
}

data "aws_iam_policy_document" "release_caller" {
  statement {
    sid       = "AssumeOnlyGuardedReleaseLauncher"
    actions   = ["sts:AssumeRole"]
    resources = [aws_iam_role.release_launcher.arn]
  }
  statement {
    sid    = "DenyDirectBuildEntryPoints"
    effect = "Deny"
    actions = [
      "codebuild:RetryBuild",
      "codebuild:RetryBuildBatch",
      "codebuild:StartBuild",
      "codebuild:StartBuildBatch",
      "codebuild:StartCommandExecution",
      "codebuild:StartSandbox",
      "codebuild:StartSandboxConnection",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_user_policy" "release_caller" {
  name   = "teamagent-release-caller"
  user   = aws_iam_user.release_caller.name
  policy = data.aws_iam_policy_document.release_caller.json
}

data "aws_iam_policy_document" "release_launcher" {
  statement {
    sid = "ReadImmutableVerifiedCandidateLocator"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
    ]
    resources = [
      "${aws_s3_bucket.image_release_evidence.arn}/release-receipts/*",
    ]
  }
  statement {
    sid       = "DecryptReleaseLocator"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [aws_kms_key.image_release_evidence.arn]
  }
  statement {
    sid       = "VerifyAttestorReleaseLocator"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Verify"]
    resources = [aws_kms_key.image_attestor_signing.arn]
  }
  statement {
    sid = "ReadCandidateAndReleaseDigest"
    actions = [
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = concat(
      [
        aws_ecr_repository.mcp_verified_candidates.arn,
        aws_ecr_repository.mcp.arn,
        aws_ecr_repository.mcp_media_verified_candidates.arn,
        aws_ecr_repository.mcp_media.arn,
        aws_ecr_repository.openclaw_verified_candidates.arn,
        aws_ecr_repository.openclaw.arn,
        aws_ecr_repository.openclaw_media_verified_candidates.arn,
        aws_ecr_repository.openclaw_media.arn,
      ],
      local.tk_enabled == 1 ? [
        aws_ecr_repository.tiktok_acquire_verified_candidates[0].arn,
        aws_ecr_repository.tiktok_acquire[0].arn,
      ] : [],
    )
  }
  statement {
    sid       = "StartFreshActiveOrRollbackAttestor"
    actions   = ["codebuild:StartBuild"]
    resources = [aws_codebuild_project.image_attestor.arn]
    condition {
      test     = "Null"
      variable = "codebuild:environment.environmentVariables.name"
      values   = ["false"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables.name"
      values   = local.release_attestor_environment_names
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PIPELINE.value"
      values   = ["mcp", "openclaw", "tiktok"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PROMOTION_CHANNEL.value"
      values   = ["active", "rollback"]
    }
  }
  statement {
    sid       = "StartFreshActiveOrRollbackPromoter"
    actions   = ["codebuild:StartBuild"]
    resources = [aws_codebuild_project.image_promoter.arn]
    condition {
      test     = "Null"
      variable = "codebuild:environment.environmentVariables.name"
      values   = ["false"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables.name"
      values   = local.promoter_environment_names
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PIPELINE.value"
      values   = ["mcp", "openclaw", "tiktok"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PROMOTION_CHANNEL.value"
      values   = ["active", "rollback"]
    }
  }
  statement {
    sid     = "PollOnlySourceFreeReleaseProjects"
    actions = ["codebuild:BatchGetBuilds"]
    resources = [
      aws_codebuild_project.image_attestor.arn,
      aws_codebuild_project.image_promoter.arn,
    ]
  }
  dynamic "statement" {
    for_each = local.launcher_denied_override_condition_keys
    content {
      effect  = "Deny"
      actions = ["codebuild:StartBuild"]
      resources = [
        aws_codebuild_project.image_attestor.arn,
        aws_codebuild_project.image_promoter.arn,
      ]
      condition {
        test     = "Null"
        variable = statement.value
        values   = ["false"]
      }
    }
  }
  statement {
    sid    = "DenyMutationAndAlternateEntryPoints"
    effect = "Deny"
    actions = [
      "codebuild:RetryBuild",
      "codebuild:RetryBuildBatch",
      "codebuild:StartBuildBatch",
      "codebuild:StartCommandExecution",
      "codebuild:StartSandbox",
      "codebuild:StartSandboxConnection",
      "ecr:BatchDeleteImage",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecs:*",
      "events:*",
      "kms:Sign",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
      "ssm:StartSession",
      "ssmmessages:*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "release_launcher" {
  name   = local.release_launcher_role_name
  role   = aws_iam_role.release_launcher.id
  policy = data.aws_iam_policy_document.release_launcher.json
}

output "release_caller_arn" {
  value = aws_iam_user.release_caller.arn
}

output "release_launcher_role_arn" {
  value = aws_iam_role.release_launcher.arn
}

# Independent control-plane boundary for installing changed embedded release
# contracts before any candidate can exist under their new hash. Its Terraform
# role can update only the five contract-consuming CodeBuild projects and the
# one fixed backend state object/lock. It cannot start builds or mutate runtime,
# image, event, scheduler, IAM, or evidence resources.
resource "aws_iam_user" "release_control_update_caller" {
  name = "teamagent-release-control-update-caller"
}

data "aws_iam_policy_document" "release_control_updater_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_user.release_control_update_caller.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:RoleSessionName"
      values   = ["teamagent-contract-control-update"]
    }
  }
}

resource "aws_iam_role" "release_control_updater" {
  name                 = local.release_control_updater_role_name
  assume_role_policy   = data.aws_iam_policy_document.release_control_updater_assume.json
  max_session_duration = 10800
}

data "aws_iam_policy_document" "release_control_update_caller" {
  statement {
    sid       = "AssumeOnlyReleaseControlUpdater"
    actions   = ["sts:AssumeRole"]
    resources = [aws_iam_role.release_control_updater.arn]
  }
  statement {
    sid    = "DenyDirectControlAndRuntimeMutation"
    effect = "Deny"
    actions = [
      "codebuild:*",
      "ecr:*",
      "ecs:*",
      "events:*",
      "scheduler:*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_user_policy" "release_control_update_caller" {
  name   = "teamagent-release-control-update-caller"
  user   = aws_iam_user.release_control_update_caller.name
  policy = data.aws_iam_policy_document.release_control_update_caller.json
}

data "aws_iam_policy_document" "release_control_updater" {
  statement {
    sid     = "ReadAndUpdateOnlyEmbeddedContractProjects"
    actions = ["codebuild:BatchGetProjects", "codebuild:UpdateProject"]
    resources = concat(
      [
        local.launcher_project_arn,
        aws_codebuild_project.mcp_source_publisher.arn,
        aws_codebuild_project.image_attestor.arn,
        aws_codebuild_project.openclaw_provenance.arn,
      ],
      local.tk_enabled == 1 ? [aws_codebuild_project.tiktok_image[0].arn] : [],
    )
  }
  statement {
    sid       = "ReadFixedTerraformStateBucket"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::teamagent-tfstate-718959508629"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["teamagent/terraform.tfstate"]
    }
  }
  statement {
    sid = "ReadOnlyTargetDependencies"
    actions = [
      "codeconnections:GetConnection",
      "iam:GetRole",
      "kms:DescribeKey",
      "logs:DescribeLogGroups",
      "logs:ListTagsForResource",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "ReadWriteOnlyFixedTerraformStateObject"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["arn:aws:s3:::teamagent-tfstate-718959508629/teamagent/terraform.tfstate"]
  }
  statement {
    sid = "UseOnlyFixedTerraformStateLock"
    actions = [
      "dynamodb:DeleteItem",
      "dynamodb:DescribeTable",
      "dynamodb:GetItem",
      "dynamodb:PutItem",
    ]
    resources = [
      "arn:aws:dynamodb:ap-northeast-1:718959508629:table/teamagent-tflock",
    ]
  }
  statement {
    sid       = "ReadOwnIdentity"
    actions   = ["sts:GetCallerIdentity"]
    resources = ["*"]
  }
  statement {
    sid    = "DenyBuildRuntimeImageAndControlExpansion"
    effect = "Deny"
    actions = [
      "codebuild:CreateProject",
      "codebuild:DeleteProject",
      "codebuild:RetryBuild",
      "codebuild:RetryBuildBatch",
      "codebuild:StartBuild",
      "codebuild:StartBuildBatch",
      "codebuild:StartCommandExecution",
      "codebuild:StartSandbox",
      "codebuild:StartSandboxConnection",
      "ecr:*",
      "ecs:*",
      "events:*",
      "lambda:*",
      "scheduler:*",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "release_control_updater" {
  name   = local.release_control_updater_role_name
  role   = aws_iam_role.release_control_updater.id
  policy = data.aws_iam_policy_document.release_control_updater.json
}

output "release_control_update_caller_arn" {
  value = aws_iam_user.release_control_update_caller.arn
}

output "release_control_updater_role_arn" {
  value = aws_iam_role.release_control_updater.arn
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

data "aws_iam_policy_document" "tiktok_codebuild_assume" {
  count = local.tk_enabled
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
      values = [
        "arn:aws:codebuild:${local.expected_build_region}:${local.expected_build_account_id}:project/${local.tiktok_codebuild_project_name}",
      ]
    }
  }
}

resource "aws_iam_role" "tiktok_codebuild" {
  count              = local.tk_enabled
  name               = "${var.project_name}-${var.environment}-codebuild-tiktok-image"
  assume_role_policy = data.aws_iam_policy_document.tiktok_codebuild_assume[0].json
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
    sid       = "ReadExternalBuildspec"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.image_release_evidence.arn}/${local.tiktok_image_buildspec_s3_key}"]
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
    sid    = "DenyTiktokCandidateAndReleaseWrite"
    effect = "Deny"
    actions = [
      "ecr:BatchDeleteImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [
      aws_ecr_repository.tiktok_acquire_verified_candidates[0].arn,
      aws_ecr_repository.tiktok_acquire[0].arn,
    ]
  }
  statement {
    sid    = "DenyEvidenceWritesAndSigning"
    effect = "Deny"
    actions = [
      "kms:Sign",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
    ]
    resources = ["*"]
  }
  statement {
    sid = "ReadSignedTikTokSourceManifest"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
    ]
    resources = [
      "${aws_s3_bucket.image_release_evidence.arn}/source-manifests/tiktok/*",
    ]
  }
  statement {
    sid       = "DecryptTikTokSourceManifest"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [aws_kms_key.image_release_evidence.arn]
  }
  statement {
    sid       = "VerifyTikTokSourcePublisher"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Verify"]
    resources = [aws_kms_key.tiktok_source_publisher_signing.arn]
  }
  statement {
    sid    = "DenyDynamicEnvironmentAndDebugChannels"
    effect = "Deny"
    actions = [
      "secretsmanager:GetSecretValue",
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssmmessages:*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "tiktok_codebuild" {
  count  = local.tk_enabled
  name   = "${var.project_name}-${var.environment}-codebuild-tiktok-image"
  role   = aws_iam_role.tiktok_codebuild[0].id
  policy = data.aws_iam_policy_document.tiktok_codebuild[0].json
}

locals {
  tiktok_image_buildspec_1 = replace(
    file("${path.module}/../codebuild/tiktok-buildspec.yml"),
    "__ECR_SCAN_GATE_BASE64__",
    filebase64("${path.module}/../codebuild/verify_ecr_scan.py"),
  )
  tiktok_image_buildspec_2 = replace(
    local.tiktok_image_buildspec_1,
    "__ECR_IMAGE_RESOLVER_BASE64__",
    filebase64("${path.module}/../codebuild/resolve_ecr_image.py"),
  )
  tiktok_image_buildspec_3 = replace(
    local.tiktok_image_buildspec_2,
    "__TIKTOK_SOURCE_PROVENANCE_BASE64__",
    filebase64("${path.module}/../codebuild/tiktok_source_provenance.py"),
  )
  tiktok_image_buildspec_4 = replace(
    local.tiktok_image_buildspec_3,
    "__TIKTOK_CONTRACT_BASE64__",
    filebase64("${path.module}/../codebuild/tiktok_release_contract.json"),
  )
  tiktok_image_buildspec_5 = replace(
    local.tiktok_image_buildspec_4,
    "__TIKTOK_SOURCE_SIGNING_KEY_ARN__",
    aws_kms_key.tiktok_source_publisher_signing.arn,
  )
  tiktok_image_buildspec = replace(
    local.tiktok_image_buildspec_5,
    "__RELEASE_EVIDENCE_KMS_KEY_ARN__",
    aws_kms_key.image_release_evidence.arn,
  )
}

resource "aws_s3_object" "tiktok_image_buildspec" {
  count = local.tk_enabled

  bucket                        = aws_s3_bucket.image_release_evidence.id
  key                           = local.tiktok_image_buildspec_s3_key
  content                       = local.tiktok_image_buildspec
  content_type                  = "text/yaml"
  source_hash                   = sha256(local.tiktok_image_buildspec)
  server_side_encryption        = "aws:kms"
  kms_key_id                    = aws_kms_key.image_release_evidence.arn
  bucket_key_enabled            = true
  object_lock_mode              = "COMPLIANCE"
  object_lock_retain_until_date = local.codebuild_buildspec_retain_until_date

  depends_on = [
    aws_s3_bucket_object_lock_configuration.image_release_evidence,
    aws_s3_bucket_policy.image_release_evidence,
  ]
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
    buildspec           = "${aws_s3_bucket.image_release_evidence.arn}/${local.tiktok_image_buildspec_s3_key}"
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

  depends_on = [
    aws_iam_role_policy.tiktok_codebuild,
    aws_s3_object.tiktok_image_buildspec,
  ]
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

# Dedicated TikTok caller and launcher. No access key is created here and this
# boundary has no dependency on the legacy AIIAdev principal.
resource "aws_iam_user" "tiktok_build_caller" {
  count = local.tk_enabled
  name  = "teamagent-tiktok-build-caller"
}

data "aws_iam_policy_document" "tiktok_build_launcher_assume" {
  count = local.tk_enabled
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_user.tiktok_build_caller[0].arn]
    }
  }

  statement {
    actions = [
      "sts:AssumeRole",
      "sts:SetSourceIdentity",
    ]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::718959508629:root"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::718959508629:root"]
    }
    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:RoleSessionName"
      values   = ["teamagent-tiktok-build"]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:SourceIdentity"
      values   = ["teamagent-production-tiktok-build"]
    }
  }
}

resource "aws_iam_role" "tiktok_build_launcher" {
  count                = local.tk_enabled
  name                 = local.tiktok_launcher_role_name
  assume_role_policy   = data.aws_iam_policy_document.tiktok_build_launcher_assume[0].json
  max_session_duration = 10800
}

data "aws_iam_policy_document" "tiktok_build_caller" {
  count = local.tk_enabled
  statement {
    sid       = "AssumeOnlyDedicatedTikTokLauncher"
    actions   = ["sts:AssumeRole"]
    resources = [aws_iam_role.tiktok_build_launcher[0].arn]
  }
  statement {
    sid    = "DenyDirectBuildEntryPoints"
    effect = "Deny"
    actions = [
      "codebuild:RetryBuild",
      "codebuild:RetryBuildBatch",
      "codebuild:StartBuild",
      "codebuild:StartBuildBatch",
      "codebuild:StartCommandExecution",
      "codebuild:StartSandbox",
      "codebuild:StartSandboxConnection",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_user_policy" "tiktok_build_caller" {
  count  = local.tk_enabled
  name   = "teamagent-tiktok-build-caller"
  user   = aws_iam_user.tiktok_build_caller[0].name
  policy = data.aws_iam_policy_document.tiktok_build_caller[0].json
}

data "aws_iam_policy_document" "tiktok_build_launcher" {
  count = local.tk_enabled

  statement {
    sid = "RequireAvailableTikTokCodeConnection"
    actions = [
      "codeconnections:GetConnection",
      "codeconnections:ListConnections",
    ]
    resources = ["*"]
  }

  statement {
    sid = "CheckImmutableTikTokEvidenceBucket"
    actions = [
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketVersioning",
    ]
    resources = [aws_s3_bucket.image_release_evidence.arn]
  }
  statement {
    sid = "PublishOnlySignedTikTokSourceEvidence"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
    ]
    resources = [
      "${aws_s3_bucket.image_release_evidence.arn}/source-manifests/tiktok/*",
    ]
  }
  statement {
    sid = "EncryptTikTokSourceEvidence"
    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:Encrypt",
      "kms:GenerateDataKey",
    ]
    resources = [aws_kms_key.image_release_evidence.arn]
  }
  statement {
    sid       = "SignOnlyTikTokSourceEvidence"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Sign", "kms:Verify"]
    resources = [aws_kms_key.tiktok_source_publisher_signing.arn]
  }
  statement {
    sid       = "StartExactTikTokBuild"
    actions   = ["codebuild:StartBuild"]
    resources = [aws_codebuild_project.tiktok_image[0].arn]
    condition {
      test     = "Null"
      variable = "codebuild:environment.environmentVariables.name"
      values   = ["false"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables.name"
      values   = local.tiktok_image_environment_names
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/GIT_BRANCH.value"
      values   = ["main"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/TIKTOK_CONTRACT_SHA256.value"
      values   = [filesha256("${path.module}/../codebuild/tiktok_release_contract.json")]
    }
  }
  statement {
    sid       = "StartTikTokSourceFreeAttestor"
    actions   = ["codebuild:StartBuild"]
    resources = [aws_codebuild_project.image_attestor.arn]
    condition {
      test     = "Null"
      variable = "codebuild:environment.environmentVariables.name"
      values   = ["false"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables.name"
      values   = local.attestor_environment_names
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PIPELINE.value"
      values   = ["tiktok"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PROMOTION_CHANNEL.value"
      values   = ["verified-candidate"]
    }
  }
  statement {
    sid       = "StartTikTokSourceFreePromoter"
    actions   = ["codebuild:StartBuild"]
    resources = [aws_codebuild_project.image_promoter.arn]
    condition {
      test     = "Null"
      variable = "codebuild:environment.environmentVariables.name"
      values   = ["false"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables.name"
      values   = local.promoter_environment_names
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PIPELINE.value"
      values   = ["tiktok"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PROMOTION_CHANNEL.value"
      values   = ["verified-candidate"]
    }
  }
  statement {
    sid     = "PollExactTikTokPipeline"
    actions = ["codebuild:BatchGetBuilds"]
    resources = [
      aws_codebuild_project.tiktok_image[0].arn,
      aws_codebuild_project.image_attestor.arn,
      aws_codebuild_project.image_promoter.arn,
    ]
  }
  statement {
    sid = "ReadTikTokQuarantineAndVerifiedCandidateDigest"
    actions = [
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
    ]
    resources = [
      aws_ecr_repository.tiktok_acquire_quarantine[0].arn,
      aws_ecr_repository.tiktok_acquire_verified_candidates[0].arn,
    ]
  }
  dynamic "statement" {
    for_each = local.launcher_denied_override_condition_keys
    content {
      effect  = "Deny"
      actions = ["codebuild:StartBuild"]
      resources = [
        aws_codebuild_project.tiktok_image[0].arn,
        aws_codebuild_project.image_attestor.arn,
        aws_codebuild_project.image_promoter.arn,
      ]
      condition {
        test     = "Null"
        variable = statement.value
        values   = ["false"]
      }
    }
  }
  statement {
    sid    = "DenyAlternateEntryPointsAndRuntimeMutation"
    effect = "Deny"
    actions = [
      "codebuild:RetryBuild",
      "codebuild:RetryBuildBatch",
      "codebuild:StartBuildBatch",
      "codebuild:StartCommandExecution",
      "codebuild:StartSandbox",
      "codebuild:StartSandboxConnection",
      "ecs:*",
      "events:*",
      "ssm:StartSession",
      "ssmmessages:*",
    ]
    resources = ["*"]
  }
  statement {
    sid    = "DenyReleaseWritesAndEvidenceDeletion"
    effect = "Deny"
    actions = [
      "ecr:BatchDeleteImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "tiktok_build_launcher" {
  count  = local.tk_enabled
  name   = local.tiktok_launcher_role_name
  role   = aws_iam_role.tiktok_build_launcher[0].id
  policy = data.aws_iam_policy_document.tiktok_build_launcher[0].json
}

output "tiktok_build_caller_arn" {
  value = local.tk_enabled == 1 ? aws_iam_user.tiktok_build_caller[0].arn : null
}

output "tiktok_build_launcher_role_arn" {
  value = local.tk_enabled == 1 ? aws_iam_role.tiktok_build_launcher[0].arn : null
}

# ============================================================
# Immutable cross-pipeline evidence + source-free attestor/promoter
# ============================================================

resource "aws_kms_key" "image_release_evidence" {
  description             = "Encrypt immutable TeamAgent image release evidence"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "image_release_evidence" {
  name          = "alias/teamagent-dev-image-release-evidence"
  target_key_id = aws_kms_key.image_release_evidence.key_id
}

resource "aws_kms_key" "mcp_source_publisher_signing" {
  description              = "Sign independently published MCP source declarations"
  deletion_window_in_days  = 30
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_3072"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "mcp_source_publisher_signing" {
  name          = "alias/teamagent-dev-mcp-source-publisher"
  target_key_id = aws_kms_key.mcp_source_publisher_signing.key_id
}

resource "aws_kms_key" "tiktok_source_publisher_signing" {
  description              = "Sign dedicated TikTok full-commit source manifests"
  deletion_window_in_days  = 30
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_3072"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "tiktok_source_publisher_signing" {
  name          = "alias/teamagent-dev-tiktok-source-publisher"
  target_key_id = aws_kms_key.tiktok_source_publisher_signing.key_id
}

resource "aws_kms_key" "image_attestor_signing" {
  description              = "Sign actual-image attestations and release receipts"
  deletion_window_in_days  = 30
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_3072"

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "image_attestor_signing" {
  name          = "alias/teamagent-dev-image-attestor"
  target_key_id = aws_kms_key.image_attestor_signing.key_id
}

resource "aws_s3_bucket" "image_release_evidence" {
  bucket              = local.release_evidence_bucket
  force_destroy       = false
  object_lock_enabled = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "image_release_evidence" {
  bucket = aws_s3_bucket.image_release_evidence.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "image_release_evidence" {
  bucket = aws_s3_bucket.image_release_evidence.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "image_release_evidence" {
  bucket = aws_s3_bucket.image_release_evidence.id
  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.image_release_evidence.arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_object_lock_configuration" "image_release_evidence" {
  bucket = aws_s3_bucket.image_release_evidence.id

  depends_on = [aws_s3_bucket_versioning.image_release_evidence]

  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = 3650
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "image_release_evidence" {
  bucket = aws_s3_bucket.image_release_evidence.id

  depends_on = [aws_s3_bucket_object_lock_configuration.image_release_evidence]

  rule {
    id     = "retain-audit-evidence"
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

data "aws_iam_policy_document" "image_release_evidence_bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.image_release_evidence.arn,
      "${aws_s3_bucket.image_release_evidence.arn}/*",
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
    resources = ["${aws_s3_bucket.image_release_evidence.arn}/*"]
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
    resources = ["${aws_s3_bucket.image_release_evidence.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.image_release_evidence.arn]
    }
  }
  statement {
    sid       = "DenyEvidenceWithoutComplianceLock"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.image_release_evidence.arn}/*"]
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
    resources = ["${aws_s3_bucket.image_release_evidence.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_s3_bucket_policy" "image_release_evidence" {
  bucket = aws_s3_bucket.image_release_evidence.id
  policy = data.aws_iam_policy_document.image_release_evidence_bucket.json
}

# Durable one-use deployment authorization ledger. One transaction acquires the
# shared lock and burns PREPARED as APPLYING for one exact attempt; another
# changes that attempt to CONSUMED while conditionally creating every exact
# receipt claim. TTL is audit cleanup only; receipt freshness is revalidated at
# both transitions and cannot be extended by this table.
resource "aws_dynamodb_table" "image_deployment_intents" {
  name         = local.image_deployment_intent_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "record_id"

  attribute {
    name = "record_id"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.image_release_evidence.arn
  }

  point_in_time_recovery {
    enabled = true
  }

  ttl {
    attribute_name = "audit_expires_at"
    enabled        = true
  }

  deletion_protection_enabled = true

  lifecycle {
    prevent_destroy = true
  }
}

data "aws_iam_policy_document" "image_deployment_gate_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::718959508629:root"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = [local.terraform_automation_role_arn]
    }
  }
}

resource "aws_iam_role" "image_deployment_gate" {
  name                 = local.image_deployment_gate_role_name
  assume_role_policy   = data.aws_iam_policy_document.image_deployment_gate_assume.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "image_deployment_gate" {
  statement {
    sid = "ReadExactImmutableReleaseReceipts"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
    ]
    resources = [
      "${aws_s3_bucket.image_release_evidence.arn}/release-receipts/*",
    ]
  }
  statement {
    sid       = "DecryptReleaseReceipts"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [aws_kms_key.image_release_evidence.arn]
  }
  statement {
    sid       = "VerifyAttestorReceiptSignature"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Verify"]
    resources = [aws_kms_key.image_attestor_signing.arn]
  }
  statement {
    sid = "ReadReleaseSubjectAndReferrerGraph"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetLifecyclePolicy",
    ]
    resources = concat(
      [
        aws_ecr_repository.mcp.arn,
        aws_ecr_repository.mcp_media.arn,
        aws_ecr_repository.openclaw.arn,
        aws_ecr_repository.openclaw_media.arn,
      ],
      local.tk_enabled == 1 ? [aws_ecr_repository.tiktok_acquire[0].arn] : [],
    )
  }
  statement {
    sid       = "ReadDeploymentLedger"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.image_deployment_intents.arn]
    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values = [
        "intent#*",
        "receipt#*",
        "lock#teamagent/terraform.tfstate",
      ]
    }
  }
  statement {
    sid       = "PrepareUniqueDeploymentIntent"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.image_deployment_intents.arn]
    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values   = ["intent#*"]
    }
  }
  statement {
    sid       = "TransitionDeploymentIntentOrHeartbeatLock"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.image_deployment_intents.arn]
    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values = [
        "intent#*",
        "lock#teamagent/terraform.tfstate",
      ]
    }
  }
  statement {
    sid       = "AtomicallyStartAndConsumeDeployment"
    actions   = ["dynamodb:TransactWriteItems"]
    resources = [aws_dynamodb_table.image_deployment_intents.arn]
    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values = [
        "intent#*",
        "receipt#*",
        "lock#teamagent/terraform.tfstate",
      ]
    }
  }
  statement {
    sid       = "ReleaseOnlySharedDeploymentLock"
    actions   = ["dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.image_deployment_intents.arn]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values   = ["lock#teamagent/terraform.tfstate"]
    }
  }
  statement {
    sid    = "DenyRuntimeEvidenceAndImageMutation"
    effect = "Deny"
    actions = [
      "codebuild:StartBuild",
      "codebuild:StartBuildBatch",
      "ecr:BatchDeleteImage",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecs:*",
      "events:*",
      "kms:Sign",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "image_deployment_gate" {
  name   = local.image_deployment_gate_role_name
  role   = aws_iam_role.image_deployment_gate.id
  policy = data.aws_iam_policy_document.image_deployment_gate.json
}

output "image_deployment_gate_role_arn" {
  value = aws_iam_role.image_deployment_gate.arn
}

output "image_deployment_intent_table" {
  value = aws_dynamodb_table.image_deployment_intents.name
}

data "aws_iam_policy_document" "mcp_source_publisher_assume" {
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
      values = [
        "arn:aws:codebuild:${local.expected_build_region}:${local.expected_build_account_id}:project/${local.mcp_source_publisher_project_name}",
      ]
    }
  }
}

resource "aws_iam_role" "mcp_source_publisher" {
  name               = "${var.project_name}-${var.environment}-codebuild-mcp-source-publisher"
  assume_role_policy = data.aws_iam_policy_document.mcp_source_publisher_assume.json
}

data "aws_iam_policy_document" "mcp_source_publisher" {
  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "${aws_cloudwatch_log_group.codebuild_mcp_source_publisher.arn}:*",
    ]
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
    sid       = "ReadExternalBuildspec"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.image_release_evidence.arn}/${local.mcp_source_publisher_buildspec_s3_key}"]
  }
  statement {
    sid = "ReadPinnedAppInputs"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]
    resources = [
      "${aws_s3_bucket.raw_files.arn}/codebuild/connect-web-app.html",
      "${aws_s3_bucket.raw_files.arn}/codebuild/baked-fallback/connect-web-app.html",
    ]
  }
  statement {
    sid     = "PublishExactSource"
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.raw_files.arn}/codebuild/source.zip",
    ]
  }
  statement {
    sid       = "CheckVersionedSourceBucket"
    actions   = ["s3:GetBucketVersioning"]
    resources = [aws_s3_bucket.raw_files.arn]
  }
  statement {
    sid = "PublishImmutableSourceAndContextEvidence"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
    ]
    resources = [
      "${aws_s3_bucket.image_release_evidence.arn}/source-declarations/mcp/*",
      "${aws_s3_bucket.image_release_evidence.arn}/source-contexts/mcp/*",
    ]
  }
  statement {
    sid = "EncryptSourceDeclarations"
    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:Encrypt",
      "kms:GenerateDataKey",
    ]
    resources = [aws_kms_key.image_release_evidence.arn]
  }
  statement {
    sid       = "SignSourceDeclarations"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Sign", "kms:Verify"]
    resources = [aws_kms_key.mcp_source_publisher_signing.arn]
  }
  statement {
    sid    = "DenyAllEcrAndEvidenceDeletion"
    effect = "Deny"
    actions = [
      "ecr:*",
      "secretsmanager:GetSecretValue",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:StartSession",
      "ssmmessages:*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "mcp_source_publisher" {
  name   = "${var.project_name}-${var.environment}-codebuild-mcp-source-publisher"
  role   = aws_iam_role.mcp_source_publisher.id
  policy = data.aws_iam_policy_document.mcp_source_publisher.json
}

data "aws_iam_policy_document" "image_attestor_assume" {
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
      values = [
        "arn:aws:codebuild:${local.expected_build_region}:${local.expected_build_account_id}:project/${local.image_attestor_project_name}",
      ]
    }
  }
}

resource "aws_iam_role" "image_attestor" {
  name               = "${var.project_name}-${var.environment}-codebuild-image-attestor"
  assume_role_policy = data.aws_iam_policy_document.image_attestor_assume.json
}

data "aws_iam_policy_document" "image_attestor" {
  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "${aws_cloudwatch_log_group.codebuild_image_attestor.arn}:*",
    ]
  }
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "ReadExternalBuildspec"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.image_release_evidence.arn}/${local.image_attestor_buildspec_s3_key}"]
  }
  statement {
    sid = "ReadWriteOnlyQuarantineEvidence"
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
    resources = concat(
      [
        aws_ecr_repository.mcp_quarantine.arn,
        aws_ecr_repository.mcp_media_quarantine.arn,
        aws_ecr_repository.openclaw_quarantine.arn,
        aws_ecr_repository.openclaw_media_quarantine.arn,
      ],
      local.tk_enabled == 1 ? [aws_ecr_repository.tiktok_acquire_quarantine[0].arn] : [],
    )
  }
  statement {
    sid = "ReadBasicScanResults"
    actions = [
      "ecr:DescribeImageScanFindings",
    ]
    resources = concat(
      [
        aws_ecr_repository.mcp_quarantine.arn,
        aws_ecr_repository.mcp_media_quarantine.arn,
        aws_ecr_repository.openclaw_quarantine.arn,
        aws_ecr_repository.openclaw_media_quarantine.arn,
      ],
      local.tk_enabled == 1 ? [aws_ecr_repository.tiktok_acquire_quarantine[0].arn] : [],
    )
  }
  statement {
    sid = "ReadVerifiedCandidates"
    actions = [
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = concat(
      [
        aws_ecr_repository.mcp_verified_candidates.arn,
        aws_ecr_repository.mcp_media_verified_candidates.arn,
        aws_ecr_repository.openclaw_verified_candidates.arn,
        aws_ecr_repository.openclaw_media_verified_candidates.arn,
      ],
      local.tk_enabled == 1 ? [aws_ecr_repository.tiktok_acquire_verified_candidates[0].arn] : [],
    )
  }
  statement {
    sid    = "DenyEveryCandidateAndReleaseRepositoryWrite"
    effect = "Deny"
    actions = [
      "ecr:BatchDeleteImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = concat(
      [
        aws_ecr_repository.mcp.arn,
        aws_ecr_repository.mcp_verified_candidates.arn,
        aws_ecr_repository.mcp_media.arn,
        aws_ecr_repository.mcp_media_verified_candidates.arn,
        aws_ecr_repository.openclaw.arn,
        aws_ecr_repository.openclaw_verified_candidates.arn,
        aws_ecr_repository.openclaw_media.arn,
        aws_ecr_repository.openclaw_media_verified_candidates.arn,
      ],
      local.tk_enabled == 1 ? [
        aws_ecr_repository.tiktok_acquire[0].arn,
        aws_ecr_repository.tiktok_acquire_verified_candidates[0].arn,
      ] : [],
    )
  }
  statement {
    sid = "ReadBuildResult"
    actions = [
      "codebuild:BatchGetBuilds",
    ]
    resources = concat(
      [
        local.launcher_project_arn,
        aws_codebuild_project.openclaw_provenance.arn,
      ],
      local.tk_enabled == 1 ? [aws_codebuild_project.tiktok_image[0].arn] : [],
    )
  }
  statement {
    sid = "ReadImmutableSignedSourceEvidence"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
    ]
    resources = [
      "${aws_s3_bucket.image_release_evidence.arn}/source-declarations/mcp/*",
      "${aws_s3_bucket.image_release_evidence.arn}/source-manifests/tiktok/*",
      "${aws_s3_bucket.openclaw_build_evidence.arn}/source-manifests/*",
    ]
  }
  statement {
    sid       = "DecryptAndVerifyPublisherEvidence"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [aws_kms_key.openclaw_evidence.arn]
  }
  statement {
    sid     = "VerifyTrustedSourcePublishers"
    actions = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Verify"]
    resources = [
      aws_kms_key.mcp_source_publisher_signing.arn,
      aws_kms_key.tiktok_source_publisher_signing.arn,
      aws_kms_key.openclaw_publisher_signing.arn,
    ]
  }
  statement {
    sid = "WriteImmutableReleaseReceipts"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
    ]
    resources = [
      "${aws_s3_bucket.image_release_evidence.arn}/release-receipts/*",
    ]
  }
  statement {
    sid = "EncryptReleaseReceipts"
    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:Encrypt",
      "kms:GenerateDataKey",
    ]
    resources = [aws_kms_key.image_release_evidence.arn]
  }
  statement {
    sid       = "SignAndVerifyActualImageEvidence"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Sign", "kms:Verify"]
    resources = [aws_kms_key.image_attestor_signing.arn]
  }
  statement {
    sid    = "DenySourceAndEvidenceDeletion"
    effect = "Deny"
    actions = [
      "codeconnections:GetConnection",
      "codeconnections:GetConnectionToken",
      "secretsmanager:GetSecretValue",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:StartSession",
      "ssmmessages:*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "image_attestor" {
  name   = "${var.project_name}-${var.environment}-codebuild-image-attestor"
  role   = aws_iam_role.image_attestor.id
  policy = data.aws_iam_policy_document.image_attestor.json
}

data "aws_iam_policy_document" "image_promoter_assume" {
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
      values = [
        "arn:aws:codebuild:${local.expected_build_region}:${local.expected_build_account_id}:project/${local.image_promoter_project_name}",
      ]
    }
  }
}

resource "aws_iam_role" "image_promoter" {
  name               = "${var.project_name}-${var.environment}-codebuild-image-promoter"
  assume_role_policy = data.aws_iam_policy_document.image_promoter_assume.json
}

data "aws_iam_policy_document" "image_promoter" {
  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "${aws_cloudwatch_log_group.codebuild_image_promoter.arn}:*",
    ]
  }
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "ReadExternalBuildspec"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.image_release_evidence.arn}/${local.image_promoter_buildspec_s3_key}"]
  }
  statement {
    sid = "ReadOnlyQuarantineAndCandidateSubjects"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = concat(
      [
        aws_ecr_repository.mcp_quarantine.arn,
        aws_ecr_repository.mcp_verified_candidates.arn,
        aws_ecr_repository.mcp_media_quarantine.arn,
        aws_ecr_repository.mcp_media_verified_candidates.arn,
        aws_ecr_repository.openclaw_quarantine.arn,
        aws_ecr_repository.openclaw_verified_candidates.arn,
        aws_ecr_repository.openclaw_media_quarantine.arn,
        aws_ecr_repository.openclaw_media_verified_candidates.arn,
      ],
      local.tk_enabled == 1 ? [
        aws_ecr_repository.tiktok_acquire_quarantine[0].arn,
        aws_ecr_repository.tiktok_acquire_verified_candidates[0].arn,
      ] : [],
    )
  }
  statement {
    sid = "WriteOnlyAllowlistedCandidateAndReleaseRepositories"
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
    resources = concat(
      [
        aws_ecr_repository.mcp_verified_candidates.arn,
        aws_ecr_repository.mcp.arn,
        aws_ecr_repository.mcp_media_verified_candidates.arn,
        aws_ecr_repository.mcp_media.arn,
        aws_ecr_repository.openclaw_verified_candidates.arn,
        aws_ecr_repository.openclaw.arn,
        aws_ecr_repository.openclaw_media_verified_candidates.arn,
        aws_ecr_repository.openclaw_media.arn,
      ],
      local.tk_enabled == 1 ? [
        aws_ecr_repository.tiktok_acquire_verified_candidates[0].arn,
        aws_ecr_repository.tiktok_acquire[0].arn,
      ] : [],
    )
  }
  statement {
    sid = "ReadExactSignedReleaseReceipts"
    actions = [
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
    ]
    resources = [
      "${aws_s3_bucket.image_release_evidence.arn}/release-receipts/*",
    ]
  }
  statement {
    sid       = "DecryptReleaseReceipts"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [aws_kms_key.image_release_evidence.arn]
  }
  statement {
    sid       = "VerifyAttestorReceiptSignature"
    actions   = ["kms:DescribeKey", "kms:GetPublicKey", "kms:Verify"]
    resources = [aws_kms_key.image_attestor_signing.arn]
  }
  statement {
    sid    = "DenySourceSigningAndEvidenceWrites"
    effect = "Deny"
    actions = [
      "codeconnections:GetConnection",
      "codeconnections:GetConnectionToken",
      "kms:Sign",
      "secretsmanager:GetSecretValue",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:StartSession",
      "ssmmessages:*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "image_promoter" {
  name   = "${var.project_name}-${var.environment}-codebuild-image-promoter"
  role   = aws_iam_role.image_promoter.id
  policy = data.aws_iam_policy_document.image_promoter.json
}

locals {
  mcp_source_publisher_buildspec_1 = replace(
    file("${path.module}/../codebuild/mcp-source-publisher-buildspec.yml"),
    "__SOURCE_PROVENANCE_BASE64__",
    filebase64("${path.module}/../codebuild/source_provenance.py"),
  )
  mcp_source_publisher_buildspec_2 = replace(
    local.mcp_source_publisher_buildspec_1,
    "__RELEASE_EVIDENCE_BASE64__",
    filebase64("${path.module}/../codebuild/release_evidence.py"),
  )
  mcp_source_publisher_buildspec_3 = replace(
    local.mcp_source_publisher_buildspec_2,
    "__TEAMAGENT_BUNDLE_PROVENANCE_BASE64__",
    filebase64("${path.module}/../codebuild/teamagent_bundle_provenance.py"),
  )
  mcp_source_publisher_buildspec_4 = replace(
    local.mcp_source_publisher_buildspec_3,
    "__SOURCE_MANIFEST_CONTRACT_BASE64__",
    filebase64("${path.module}/../codebuild/teamagent_runtime_contract.json"),
  )
  mcp_source_publisher_buildspec_5 = replace(
    local.mcp_source_publisher_buildspec_4,
    "__MCP_RELEASE_CONTRACT_BASE64__",
    filebase64("${path.module}/../codebuild/teamagent_core_media_release_contract.json"),
  )
  mcp_source_publisher_buildspec_6 = replace(
    local.mcp_source_publisher_buildspec_5,
    "__SOURCE_PUBLISHER_SIGNING_KEY_ARN__",
    aws_kms_key.mcp_source_publisher_signing.arn,
  )
  mcp_source_publisher_buildspec = replace(
    local.mcp_source_publisher_buildspec_6,
    "__RELEASE_EVIDENCE_KMS_KEY_ARN__",
    aws_kms_key.image_release_evidence.arn,
  )

  image_attestor_buildspec_1 = replace(
    file("${path.module}/../codebuild/image-attestor-buildspec.yml"),
    "__RELEASE_EVIDENCE_BASE64__",
    filebase64("${path.module}/../codebuild/release_evidence.py"),
  )
  image_attestor_buildspec_2 = replace(
    local.image_attestor_buildspec_1,
    "__ACTUAL_IMAGE_EVIDENCE_BASE64__",
    filebase64("${path.module}/../codebuild/actual_image_evidence.py"),
  )
  image_attestor_buildspec_3 = replace(
    replace(
      local.image_attestor_buildspec_2,
      "__SOURCE_PROVENANCE_BASE64__",
      filebase64("${path.module}/../codebuild/source_provenance.py"),
    ),
    "__TEAMAGENT_BUNDLE_PROVENANCE_BASE64__",
    filebase64("${path.module}/../codebuild/teamagent_bundle_provenance.py"),
  )
  image_attestor_buildspec_4 = replace(
    local.image_attestor_buildspec_3,
    "__OPENCLAW_PROVENANCE_BASE64__",
    filebase64("${path.module}/../codebuild/openclaw_provenance.py"),
  )
  image_attestor_buildspec_5 = replace(
    local.image_attestor_buildspec_4,
    "__TIKTOK_SOURCE_PROVENANCE_BASE64__",
    filebase64("${path.module}/../codebuild/tiktok_source_provenance.py"),
  )
  image_attestor_buildspec_6 = replace(
    local.image_attestor_buildspec_5,
    "__VERIFY_ACTUAL_IMAGE_BASE64__",
    filebase64("${path.module}/../codebuild/verify_actual_image.sh"),
  )
  image_attestor_buildspec_7 = replace(
    local.image_attestor_buildspec_6,
    "__MCP_CONTRACT_BASE64__",
    filebase64("${path.module}/../codebuild/teamagent_core_media_release_contract.json"),
  )
  image_attestor_buildspec_8 = replace(
    local.image_attestor_buildspec_7,
    "__TIKTOK_CONTRACT_BASE64__",
    filebase64("${path.module}/../codebuild/tiktok_release_contract.json"),
  )
  image_attestor_buildspec_9 = replace(
    local.image_attestor_buildspec_8,
    "__OPENCLAW_CONTRACT_BASE64__",
    filebase64("${path.module}/../codebuild/openclaw_bundle_contract.json"),
  )
  image_attestor_buildspec_10 = replace(
    local.image_attestor_buildspec_9,
    "__ATTESTOR_SIGNING_KEY_ARN__",
    aws_kms_key.image_attestor_signing.arn,
  )
  image_attestor_buildspec_11 = replace(
    local.image_attestor_buildspec_10,
    "__SOURCE_PUBLISHER_SIGNING_KEY_ARN__",
    aws_kms_key.mcp_source_publisher_signing.arn,
  )
  image_attestor_buildspec_12 = replace(
    local.image_attestor_buildspec_11,
    "__TIKTOK_SOURCE_SIGNING_KEY_ARN__",
    aws_kms_key.tiktok_source_publisher_signing.arn,
  )
  image_attestor_buildspec_13 = replace(
    local.image_attestor_buildspec_12,
    "__OPENCLAW_SIGNING_KMS_KEY_ARN__",
    aws_kms_key.openclaw_publisher_signing.arn,
  )
  image_attestor_buildspec_14 = replace(
    local.image_attestor_buildspec_13,
    "__OPENCLAW_EVIDENCE_KMS_KEY_ARN__",
    aws_kms_key.openclaw_evidence.arn,
  )
  image_attestor_buildspec = replace(
    local.image_attestor_buildspec_14,
    "__RELEASE_EVIDENCE_KMS_KEY_ARN__",
    aws_kms_key.image_release_evidence.arn,
  )

  image_promoter_buildspec_1 = replace(
    file("${path.module}/../codebuild/image-promoter-buildspec.yml"),
    "__RELEASE_EVIDENCE_BASE64__",
    filebase64("${path.module}/../codebuild/release_evidence.py"),
  )
  image_promoter_buildspec_2 = replace(
    local.image_promoter_buildspec_1,
    "__ATTESTOR_SIGNING_KEY_ARN__",
    aws_kms_key.image_attestor_signing.arn,
  )
  image_promoter_buildspec = replace(
    local.image_promoter_buildspec_2,
    "__RELEASE_EVIDENCE_KMS_KEY_ARN__",
    aws_kms_key.image_release_evidence.arn,
  )
}

resource "aws_s3_object" "mcp_source_publisher_buildspec" {
  bucket                        = aws_s3_bucket.image_release_evidence.id
  key                           = local.mcp_source_publisher_buildspec_s3_key
  content                       = local.mcp_source_publisher_buildspec
  content_type                  = "text/yaml"
  source_hash                   = sha256(local.mcp_source_publisher_buildspec)
  server_side_encryption        = "aws:kms"
  kms_key_id                    = aws_kms_key.image_release_evidence.arn
  bucket_key_enabled            = true
  object_lock_mode              = "COMPLIANCE"
  object_lock_retain_until_date = local.codebuild_buildspec_retain_until_date

  depends_on = [
    aws_s3_bucket_object_lock_configuration.image_release_evidence,
    aws_s3_bucket_policy.image_release_evidence,
  ]
}

resource "aws_s3_object" "image_attestor_buildspec" {
  bucket                        = aws_s3_bucket.image_release_evidence.id
  key                           = local.image_attestor_buildspec_s3_key
  content                       = local.image_attestor_buildspec
  content_type                  = "text/yaml"
  source_hash                   = sha256(local.image_attestor_buildspec)
  server_side_encryption        = "aws:kms"
  kms_key_id                    = aws_kms_key.image_release_evidence.arn
  bucket_key_enabled            = true
  object_lock_mode              = "COMPLIANCE"
  object_lock_retain_until_date = local.codebuild_buildspec_retain_until_date

  depends_on = [
    aws_s3_bucket_object_lock_configuration.image_release_evidence,
    aws_s3_bucket_policy.image_release_evidence,
  ]
}

resource "aws_s3_object" "image_promoter_buildspec" {
  bucket                        = aws_s3_bucket.image_release_evidence.id
  key                           = local.image_promoter_buildspec_s3_key
  content                       = local.image_promoter_buildspec
  content_type                  = "text/yaml"
  source_hash                   = sha256(local.image_promoter_buildspec)
  server_side_encryption        = "aws:kms"
  kms_key_id                    = aws_kms_key.image_release_evidence.arn
  bucket_key_enabled            = true
  object_lock_mode              = "COMPLIANCE"
  object_lock_retain_until_date = local.codebuild_buildspec_retain_until_date

  depends_on = [
    aws_s3_bucket_object_lock_configuration.image_release_evidence,
    aws_s3_bucket_policy.image_release_evidence,
  ]
}

resource "aws_codebuild_project" "mcp_source_publisher" {
  name           = local.mcp_source_publisher_project_name
  description    = "Independently validate origin/dev and publish a signed versioned MCP source"
  service_role   = aws_iam_role.mcp_source_publisher.arn
  source_version = "0000000000000000000000000000000000000000"

  artifacts { type = "NO_ARTIFACTS" }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
    type            = "ARM_CONTAINER"
    privileged_mode = false
  }

  source {
    type                = "GITHUB"
    location            = "https://github.com/noirelumiere00/TeamAgent.git"
    git_clone_depth     = 0
    report_build_status = false
    buildspec           = "${aws_s3_bucket.image_release_evidence.arn}/${local.mcp_source_publisher_buildspec_s3_key}"
    auth {
      type     = "CODECONNECTIONS"
      resource = aws_codestarconnections_connection.openclaw_codebuild.arn
    }
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild_mcp_source_publisher.name
    }
  }

  depends_on = [
    aws_iam_role_policy.mcp_source_publisher,
    aws_s3_object.mcp_source_publisher_buildspec,
  ]
}

resource "aws_codebuild_project" "image_attestor" {
  name          = local.image_attestor_project_name
  description   = "Source-free actual-image verifier and immutable receipt signer"
  service_role  = aws_iam_role.image_attestor.arn
  build_timeout = 120

  artifacts { type = "NO_ARTIFACTS" }

  environment {
    compute_type    = "BUILD_GENERAL1_LARGE"
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
    type            = "ARM_CONTAINER"
    privileged_mode = true
  }

  source {
    type      = "NO_SOURCE"
    buildspec = "${aws_s3_bucket.image_release_evidence.arn}/${local.image_attestor_buildspec_s3_key}"
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild_image_attestor.name
    }
  }

  depends_on = [
    aws_iam_role_policy.image_attestor,
    aws_s3_object.image_attestor_buildspec,
  ]
}

resource "aws_codebuild_project" "image_promoter" {
  name          = local.image_promoter_project_name
  description   = "Source-free signed-receipt-only quarantine to release promoter"
  service_role  = aws_iam_role.image_promoter.arn
  build_timeout = 60

  artifacts { type = "NO_ARTIFACTS" }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
    type            = "ARM_CONTAINER"
    privileged_mode = false
  }

  source {
    type      = "NO_SOURCE"
    buildspec = "${aws_s3_bucket.image_release_evidence.arn}/${local.image_promoter_buildspec_s3_key}"
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.codebuild_image_promoter.name
    }
  }

  depends_on = [
    aws_iam_role_policy.image_promoter,
    aws_s3_object.image_promoter_buildspec,
  ]
}

output "mcp_source_publisher_project" {
  value = aws_codebuild_project.mcp_source_publisher.name
}

output "image_attestor_project" {
  value = aws_codebuild_project.image_attestor.name
}

output "image_promoter_project" {
  value = aws_codebuild_project.image_promoter.name
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
  description              = "Sign trusted OpenClaw source manifests"
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
      days = 3650
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

data "aws_iam_policy_document" "openclaw_codebuild_assume" {
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
      values = [
        "arn:aws:codebuild:${local.expected_build_region}:${local.expected_build_account_id}:project/${local.openclaw_codebuild_project_name}",
      ]
    }
  }
}

resource "aws_iam_role" "openclaw_codebuild" {
  name               = "${var.project_name}-${var.environment}-codebuild-openclaw"
  assume_role_policy = data.aws_iam_policy_document.openclaw_codebuild_assume.json
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
    sid    = "DenyOpenClawCandidateAndReleaseWrite"
    effect = "Deny"
    actions = [
      "ecr:BatchDeleteImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [
      aws_ecr_repository.openclaw_verified_candidates.arn,
      aws_ecr_repository.openclaw.arn,
      aws_ecr_repository.openclaw_media_verified_candidates.arn,
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
      aws_ecr_repository.mcp_verified_candidates.arn,
      aws_ecr_repository.mcp_media.arn,
      aws_ecr_repository.mcp_media_quarantine.arn,
      aws_ecr_repository.mcp_media_verified_candidates.arn,
    ]
  }
  statement {
    sid    = "DenyDynamicEnvironmentAndDebugChannels"
    effect = "Deny"
    actions = [
      "secretsmanager:GetSecretValue",
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssmmessages:*",
    ]
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

  statement {
    actions = [
      "sts:AssumeRole",
      "sts:SetSourceIdentity",
    ]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::718959508629:root"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::718959508629:root"]
    }
    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:RoleSessionName"
      values   = ["openclaw-build-publisher"]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:SourceIdentity"
      values   = ["teamagent-production-openclaw-build"]
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
    sid = "RequireAvailableOpenClawCodeConnection"
    actions = [
      "codeconnections:GetConnection",
      "codeconnections:ListConnections",
    ]
    resources = ["*"]
  }
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
    sid     = "PollExactOpenClawBuild"
    actions = ["codebuild:BatchGetBuilds"]
    resources = [
      aws_codebuild_project.openclaw_provenance.arn,
      aws_codebuild_project.image_attestor.arn,
      aws_codebuild_project.image_promoter.arn,
    ]
  }
  statement {
    sid       = "StartOpenClawSourceFreeAttestor"
    actions   = ["codebuild:StartBuild"]
    resources = [aws_codebuild_project.image_attestor.arn]
    condition {
      test     = "Null"
      variable = "codebuild:environment.environmentVariables.name"
      values   = ["false"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables.name"
      values   = local.attestor_environment_names
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PIPELINE.value"
      values   = ["openclaw"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PROMOTION_CHANNEL.value"
      values   = ["verified-candidate"]
    }
  }
  statement {
    sid       = "StartOpenClawSourceFreePromoter"
    actions   = ["codebuild:StartBuild"]
    resources = [aws_codebuild_project.image_promoter.arn]
    condition {
      test     = "Null"
      variable = "codebuild:environment.environmentVariables.name"
      values   = ["false"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables.name"
      values   = local.promoter_environment_names
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PIPELINE.value"
      values   = ["openclaw"]
    }
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "codebuild:environment.environmentVariables/PROMOTION_CHANNEL.value"
      values   = ["verified-candidate"]
    }
  }
  statement {
    sid = "ReadOpenClawQuarantineAndVerifiedCandidateEvidence"
    actions = [
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [
      aws_ecr_repository.openclaw_quarantine.arn,
      aws_ecr_repository.openclaw_verified_candidates.arn,
      aws_ecr_repository.openclaw_media_quarantine.arn,
      aws_ecr_repository.openclaw_media_verified_candidates.arn,
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
      effect  = "Deny"
      actions = ["codebuild:StartBuild"]
      resources = [
        aws_codebuild_project.openclaw_provenance.arn,
        aws_codebuild_project.image_attestor.arn,
        aws_codebuild_project.image_promoter.arn,
      ]
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
    sid    = "DenyDirectImageRuntimeAndEvidenceMutation"
    effect = "Deny"
    actions = [
      "ecr:BatchDeleteImage",
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecs:*",
      "events:*",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = ["*"]
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
