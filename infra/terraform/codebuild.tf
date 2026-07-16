# ============================================================
# CodeBuild: MCP/OpenClaw イメージを **AWS内（社内proxy外）でビルド＆ECR push**
# ============================================================
# go-live 実測: 社内proxy が PyTorch CDN(download.pytorch.org) の大容量DLを遮断し、
# ローカル docker build で torch(CPU) が取得できない。CodeBuild は AWS 内で走るため
# proxy を経由せず CDN に直結＝slim な CPU イメージを確実にビルドできる（再利用可能なCI基盤）。
# arm64 ネイティブ（ARM_CONTAINER）でビルドし qemu 不要。source は raw_files S3 の zip。

data "aws_caller_identity" "cb" {}

locals {
  cb_registry = "${data.aws_caller_identity.cb.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
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
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:DescribeImages", # buildspec post_build の candidate digest 取得
    ]
    resources = [aws_ecr_repository.mcp.arn, aws_ecr_repository.openclaw.arn]
  }
  statement {
    sid       = "EcrMcpScanGate"
    actions   = ["ecr:DescribeImageScanFindings"]
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
  description  = "Build teamagent-mcp/openclaw images inside AWS (proxy-free) and push to ECR"
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
    environment_variable {
      name  = "OC_REPO"
      value = aws_ecr_repository.openclaw.repository_url
    }
    # IMAGE_TAG / WITH_SCRAPE_TOOLS / GIT_COMMIT / GIT_BRANCH have no project
    # defaults. build_teamagent_image.sh must bind all four to the source manifest
    # and pass them as explicit per-build overrides.
  }

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.raw_files.id}/codebuild/source.zip"
    buildspec = file("${path.module}/../codebuild/buildspec.yml")
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
