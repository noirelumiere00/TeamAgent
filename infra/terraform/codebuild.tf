# ============================================================
# CodeBuild: TeamAgent MCP candidateを **AWS内（社内proxy外）でbuild・scan gate**
# ============================================================
# go-live 実測: 社内proxy が PyTorch CDN(download.pytorch.org) の大容量DLを遮断し、
# ローカル docker build で torch(CPU) が取得できない。CodeBuild は AWS 内で走るため
# proxy を経由せず CDN に直結＝slim な CPU イメージを確実にビルドできる（再利用可能なCI基盤）。
# arm64 ネイティブ（ARM_CONTAINER）でビルドし qemu 不要。source は raw_files S3 の zip。

data "aws_caller_identity" "cb" {}

locals {
  cb_registry                   = "${data.aws_caller_identity.cb.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
  tiktok_codebuild_project_name = "${var.project_name}-${var.environment}-tiktok-image-builder"
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
    actions = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.cb.account_id}:log-group:/aws/codebuild/${var.project_name}-${var.environment}-*",
    ]
  }
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # GetAuthorizationToken はリソース指定不可（AWS仕様）
  }
  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:DescribeImages", # buildspec post_build の candidate digest 取得
    ]
    resources = [aws_ecr_repository.mcp.arn]
  }
  statement {
    sid       = "EcrMcpScanGate"
    actions   = ["ecr:DescribeImageScanFindings"]
    resources = [aws_ecr_repository.mcp.arn]
  }
  statement {
    sid = "EcrMcpRemoteVerification"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.mcp.arn]
  }
  statement {
    sid       = "S3Source"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.raw_files.arn}/codebuild/*"]
  }
}

resource "aws_iam_role_policy" "codebuild" {
  name   = "${var.project_name}-${var.environment}-codebuild-image"
  role   = aws_iam_role.codebuild.id
  policy = data.aws_iam_policy_document.codebuild.json
}

# --- CodeBuild プロジェクト（arm64・docker・versioned S3 source） ---
resource "aws_codebuild_project" "image" {
  name         = "${var.project_name}-${var.environment}-image-builder"
  description  = "Build and vulnerability-gate TeamAgent MCP candidate images inside AWS"
  service_role = aws_iam_role.codebuild.arn

  artifacts { type = "NO_ARTIFACTS" }

  environment {
    compute_type    = "BUILD_GENERAL1_LARGE"                           # torch/sentence-transformers ビルドに余裕
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0" # arm64 ネイティブ
    type            = "ARM_CONTAINER"
    privileged_mode = true # docker ビルドに必須

    environment_variable {
      name  = "ECR_REGISTRY"
      value = local.cb_registry
    }
    environment_variable {
      name  = "MCP_REPO"
      value = aws_ecr_repository.mcp.repository_url
    }
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
      ),
      "__ECR_SCAN_EXCEPTIONS_SHA256__",
      filesha256("${path.module}/../codebuild/ecr_scan_exceptions.json"),
    )
  }

  logs_config {
    cloudwatch_logs {
      group_name = "/aws/codebuild/${var.project_name}-${var.environment}-image-builder"
    }
  }
}

output "codebuild_project" {
  value = aws_codebuild_project.image.name
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
    actions = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.cb.account_id}:log-group:/aws/codebuild/${local.tiktok_codebuild_project_name}*",
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
    sid = "TiktokEcrPush"
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
    sid       = "TiktokEcrScanGate"
    actions   = ["ecr:DescribeImageScanFindings"]
    resources = [aws_ecr_repository.tiktok_acquire[0].arn]
  }
  statement {
    sid = "TiktokEcrRemoteVerification"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.tiktok_acquire[0].arn]
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

    environment_variable {
      name  = "ECR_REGISTRY"
      value = local.cb_registry
    }
    environment_variable {
      name  = "TIKTOK_REPO"
      value = aws_ecr_repository.tiktok_acquire[0].repository_url
    }
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
      group_name = "/aws/codebuild/${local.tiktok_codebuild_project_name}"
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
