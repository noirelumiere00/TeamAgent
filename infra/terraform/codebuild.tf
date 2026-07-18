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

# --- CodeBuild 用 IAM ロール（ECR push / logs / S3 source 読取のみ） ---
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
      "ecr:DescribeImages", # buildspec post_build の digest 取得（無いと tee が空のまま SUCCEEDED）
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

# --- CodeBuild プロジェクト（arm64・docker・S3 zip source・inline buildspec） ---
resource "aws_codebuild_project" "image" {
  name         = "${var.project_name}-${var.environment}-image-builder"
  description  = "Build the TeamAgent MCP image inside AWS (proxy-free) and push to ECR"
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
    environment_variable {
      name  = "IMAGE_TAG"
      value = "p1a"
    }
    # §VSEO: 拡張版（node/chromium/ffmpeg入り）は start-build の env override で true に。既定 false＝薄殻。
    environment_variable {
      name  = "WITH_SCRAPE_TOOLS"
      value = "false"
    }
    # 柱4(2026-06-22): ビルド出所追跡。start-build の --environment-variables-override で
    # GIT_COMMIT=$(git rev-parse HEAD) / GIT_BRANCH=$(git branch --show-current) を渡す。
    environment_variable {
      name  = "GIT_COMMIT"
      value = "unknown"
    }
    environment_variable {
      name  = "GIT_BRANCH"
      value = "unknown"
    }
  }

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.raw_files.id}/codebuild/source.zip"
    buildspec = <<-EOT
      version: 0.2
      phases:
        pre_build:
          commands:
            - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $ECR_REGISTRY
        build:
          commands:
            - echo "Building teamagent-mcp ($IMAGE_TAG) on $(uname -m)"
            - docker build -f infra/docker/Dockerfile.teamagent-mcp --build-arg WITH_SCRAPE_TOOLS=$WITH_SCRAPE_TOOLS --build-arg GIT_COMMIT="$GIT_COMMIT" --build-arg GIT_BRANCH="$GIT_BRANCH" -t $MCP_REPO:$IMAGE_TAG .
        post_build:
          commands:
            - docker push $MCP_REPO:$IMAGE_TAG
            - aws ecr describe-images --repository-name ${var.project_name}-mcp --image-ids imageTag=$IMAGE_TAG --query 'imageDetails[0].imageDigest' --output text | tee /tmp/digest.txt
            - echo "MCP_DIGEST=$(cat /tmp/digest.txt)"
    EOT
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

# OpenClaw builder is deliberately unable to authenticate to or write ECR.
# Canonical promotion belongs to the out-of-process shared trusted-release
# worker. The source ZIP therefore cannot bypass gates by invoking PutImage
# directly, even if it contains hostile repository scripts.
resource "aws_iam_role" "codebuild_openclaw" {
  name               = "${var.project_name}-${var.environment}-codebuild-openclaw"
  assume_role_policy = data.aws_iam_policy_document.codebuild_assume.json
}

data "aws_iam_policy_document" "codebuild_openclaw" {
  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.cb.account_id}:log-group:/aws/codebuild/${var.project_name}-${var.environment}-openclaw-image-builder",
      "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.cb.account_id}:log-group:/aws/codebuild/${var.project_name}-${var.environment}-openclaw-image-builder:*",
    ]
  }
  statement {
    sid       = "TrustedSourceTransportRead"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.raw_files.arn}/codebuild/source.zip"]
  }
  statement {
    sid       = "EvidenceArtifactWrite"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.raw_files.arn}/codebuild/openclaw-evidence/*"]
  }
  statement {
    sid       = "EvidenceBucketMetadata"
    actions   = ["s3:GetBucketAcl", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.raw_files.arn]
  }
}

resource "aws_iam_role_policy" "codebuild_openclaw" {
  name   = "${var.project_name}-${var.environment}-codebuild-openclaw"
  role   = aws_iam_role.codebuild_openclaw.id
  policy = data.aws_iam_policy_document.codebuild_openclaw.json
}

# The project consumes a Terraform-embedded buildspec. It must never execute a
# buildspec supplied by the untrusted S3 ZIP before source verification.
resource "aws_codebuild_project" "openclaw_image" {
  name         = "${var.project_name}-${var.environment}-openclaw-image-builder"
  description  = "Verify trusted source, locally gate, and request trusted promotion of OpenClaw linux/arm64"
  service_role = aws_iam_role.codebuild_openclaw.arn

  artifacts {
    type                   = "S3"
    location               = aws_s3_bucket.raw_files.id
    path                   = "codebuild/openclaw-evidence"
    namespace_type         = "BUILD_ID"
    packaging              = "ZIP"
    name                   = "openclaw-release"
    override_artifact_name = true
    encryption_disabled    = false
  }

  environment {
    compute_type    = "BUILD_GENERAL1_LARGE"
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
    type            = "ARM_CONTAINER"
    privileged_mode = true

    # No start-build override may select source identity or repository. The
    # out-of-source /opt/teamagent/trusted-release CLI supplies source claims
    # from a KMS-signed publisher statement and owns quarantine/promotion.
    # Until the shared worker provides that executable in the build image, the
    # buildspec fails before executing any file from the S3 transport ZIP.
  }

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.raw_files.id}/codebuild/source.zip"
    buildspec = file("${path.module}/../codebuild/buildspec.openclaw.yml")
  }

  logs_config {
    cloudwatch_logs {
      group_name = "/aws/codebuild/${var.project_name}-${var.environment}-openclaw-image-builder"
    }
  }
}

output "openclaw_codebuild_project" {
  value = aws_codebuild_project.openclaw_image.name
}
