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
# 利用例:
#   cp terraform.tfvars.example terraform.tfvars
#   # 編集
#   terraform init
#   terraform plan
#   terraform apply
# ============================================================

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # 本番運用時は S3 + DynamoDB ロックバックエンド推奨
  # backend "s3" {
  #   bucket         = "teamagent-tfstate"
  #   key            = "teamagent/terraform.tfstate"
  #   region         = "ap-northeast-1"
  #   dynamodb_table = "teamagent-tflock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "TeamAgent"
      Version     = "v3.0"
      Environment = var.environment
      ManagedBy   = "Terraform"
    }
  }
}
