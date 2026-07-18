# ============================================================
# TeamAgent v3.0 — AWS インフラ Terraform
# ============================================================
# 構築リソース:
#   - VPC（簡易、デフォルト VPC 利用も可）
#   - RDS PostgreSQL 16 + pgvector
#   - Lambda（Agent SDK ループ実行）
#   - Secrets Manager（OAuth トークン・API キー）
#   - S3（提案書 PDF / 動画 / 生ファイル保存）
#   - CloudWatch Logs
#   - IAM Role（Lambda 用）
#
# 操作入口:
#   infra/deploy/terraform_runtime_guard.sh
# plain terraform plan/apply と旧image-only deploy scriptは禁止。詳細はREADME参照。
# ============================================================

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "= 2.8.0"
    }
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }

  # tfstate を S3 + DynamoDB ロックで管理（2026/5/22 有効化）
  backend "s3" {
    bucket         = "teamagent-tfstate-718959508629"
    key            = "teamagent/terraform.tfstate"
    region         = "ap-northeast-1"
    dynamodb_table = "teamagent-tflock"
    encrypt        = true
  }
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = ["718959508629"]

  default_tags {
    tags = {
      Project     = "TeamAgent"
      Version     = "v3.0"
      Environment = var.environment
      ManagedBy   = "Terraform"
    }
  }
}
