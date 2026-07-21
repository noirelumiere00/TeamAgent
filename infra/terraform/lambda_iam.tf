# ============================================================
# Lambda + IAM + S3 + CloudWatch
# ============================================================

# ---------- S3 ----------
resource "aws_s3_bucket" "raw_files" {
  bucket = "${var.project_name}-${var.environment}-raw-files"
}

resource "aws_s3_bucket_versioning" "raw_files" {
  bucket = aws_s3_bucket.raw_files.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw_files" {
  bucket = aws_s3_bucket.raw_files.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# パブリックアクセスを完全ブロック（提案PDF等は社内のみ）
resource "aws_s3_bucket_public_access_block" "raw_files" {
  bucket                  = aws_s3_bucket.raw_files.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------- ライフサイクル（Glacier 遷移 + 非現行版整理。監視#20）----------
# 成果物（vseo-reports/・vseo-proposals/・codebuild/）を一定日数後に Glacier IR へ遷移し、
# バージョニングの非現行版を整理してストレージ費を抑える。署名付き URL は 7 日で失効するため
# 遷移後（既定 90 日）にアクセスが残ることは稀。versioning 有効が前提。
resource "aws_s3_bucket_lifecycle_configuration" "raw_files" {
  bucket = aws_s3_bucket.raw_files.id

  rule {
    id     = "archive-current-to-glacier-ir"
    status = "Enabled"

    filter {} # バケット全体

    transition {
      days          = var.s3_glacier_transition_days
      storage_class = "GLACIER_IR"
    }
  }

  # v0.3 Task10: 分析結果キャッシュは prompt_version/model 込みキーで陳腐化を防いでいるが、
  # ストレージ衛生のため 60 日で現行版ごと削除（ヒット率と費用のバランスは運用で調整）。
  rule {
    id     = "expire-analysis-cache"
    status = "Enabled"

    filter {
      prefix = "analysis-cache/"
    }

    expiration {
      days = 60
    }
  }

  # v0.3 Task8: 長文ペイロード退避（payload-offload/）は営業FB全文等を含むため
  # 無期限蓄積させない（署名URLの実効期限より十分長い 14 日で現行版ごと削除）。
  rule {
    id     = "expire-payload-offload"
    status = "Enabled"

    filter {
      prefix = "payload-offload/"
    }

    expiration {
      days = 14
    }
  }

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {} # バケット全体

    noncurrent_version_expiration {
      noncurrent_days = var.s3_noncurrent_expiration_days
    }
  }

  # tiktok-acquire の中間生成物は30日で削除（2026-07-11 統合）。
  # ⚠️ 1バケット1 lifecycle_configuration の原則: 以前は tiktok_acquire.tf 側に別リソースとして
  # 存在し、同一バケットへの PutBucketLifecycleConfiguration が全ルール置換のため、apply のたびに
  # こちらの Glacier/expire ルールと交互上書きされる事故構成だった（2026-07-06 の既知地雷の再発）。
  # raw_files バケットのルールは必ずこのリソースに集約すること。
  dynamic "rule" {
    for_each = var.enable_tiktok_acquire ? [1] : []
    content {
      id     = "tiktok-acquire-expire"
      status = "Enabled"
      filter {
        prefix = "tiktok-acquire/"
      }
      expiration {
        days = 30
      }
    }
  }

  # x-research(④効果測定)の中間生成物も30日で削除（tiktok-acquire と同じ扱い・上記地雷準拠で
  # ここに集約。x_research.tf 側には lifecycle リソースを置かない）。
  dynamic "rule" {
    for_each = var.enable_x_research ? [1] : []
    content {
      id     = "x-research-expire"
      status = "Enabled"
      filter {
        prefix = "x-research/"
      }
      expiration {
        days = 30
      }
    }
  }

  depends_on = [aws_s3_bucket_versioning.raw_files]
}

# ---------- IAM Role for Lambda ----------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "${var.project_name}-${var.environment}-lambda-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# 基本実行権限
resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# VPC 接続用
resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# Bedrock / Secrets / S3 / RDS の権限
data "aws_iam_policy_document" "lambda_app" {
  statement {
    sid = "Bedrock"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = local.lambda_bedrock_resources
  }

  statement {
    sid = "Secrets"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]
    resources = [data.aws_secretsmanager_secret.database_url.arn]
  }

  statement {
    sid = "S3"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.raw_files.arn,
      "${aws_s3_bucket.raw_files.arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "lambda_app" {
  name   = "${var.project_name}-${var.environment}-lambda-app"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_app.json
}

# ---------- Lambda 関数本体（コード zip は別途用意）----------
# 開発初期はプレースホルダ。実装が進んだら zip / コンテナイメージで差し替え
# resource "aws_lambda_function" "agent" {
#   function_name = "${var.project_name}-${var.environment}-agent"
#   role          = aws_iam_role.lambda_exec.arn
#   handler       = "teamagent.lambda_handler.handler"
#   runtime       = "python3.11"
#   memory_size   = var.lambda_memory_size
#   timeout       = var.lambda_timeout
#   filename      = "../../../dist/agent_lambda.zip"
#
#   environment {
#     variables = {
#       APP_ENV          = var.environment
#       AWS_REGION       = var.aws_region
#       BEDROCK_MODEL_ID = var.bedrock_model_id
#       DATABASE_URL_SECRET = aws_secretsmanager_secret.db_password.arn
#       S3_BUCKET_RAW    = aws_s3_bucket.raw_files.id
#     }
#   }
# }

# ---------- CloudWatch ----------
resource "aws_cloudwatch_log_group" "app" {
  name              = "/${var.project_name}/${var.environment}"
  retention_in_days = 30
}
