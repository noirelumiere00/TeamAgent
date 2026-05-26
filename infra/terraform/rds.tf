# ============================================================
# RDS PostgreSQL + pgvector
# ============================================================

resource "random_password" "db_password" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

# パスワードは Secrets Manager に保管
resource "aws_secretsmanager_secret" "db_password" {
  name = "${var.project_name}/${var.environment}/db_password"
}

resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id     = aws_secretsmanager_secret.db_password.id
  secret_string = random_password.db_password.result
}

# パラメータグループで pgvector を有効化
resource "aws_db_parameter_group" "main" {
  name   = "${var.project_name}-${var.environment}-pg16"
  family = "postgres16"

  # static パラメータ（変更には DB 再起動が必要）
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot"
  }

  # dynamic パラメータ（即時反映可）
  parameter {
    name  = "log_min_duration_statement"
    value = "1000" # 1秒以上のクエリをログ
  }

  # SSL 接続を強制（Sprint 2 / 2.7 セキュリティ）
  # rds.force_ssl=1 で TLS 必須化。接続側は sslmode=require 以上
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
}

# サブネットグループ（既存 VPC のサブネットを使う想定。要調整）
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.project_name}-${var.environment}"
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_security_group" "db" {
  name        = "${var.project_name}-${var.environment}-db-sg"
  description = "TeamAgent DB SG"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# 踏み台 SG からの 5432 を許可
resource "aws_security_group_rule" "db_from_bastion" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.bastion.id
  security_group_id        = aws_security_group.db.id
  description              = "Allow PostgreSQL from bastion"
}

# Lambda SG からの 5432 を許可（Lambda 実装後に有効化）
# resource "aws_security_group_rule" "db_from_lambda" {
#   type                     = "ingress"
#   from_port                = 5432
#   to_port                  = 5432
#   protocol                 = "tcp"
#   source_security_group_id = aws_security_group.lambda.id
#   security_group_id        = aws_security_group.db.id
#   description              = "Allow PostgreSQL from Lambda"
# }

resource "aws_db_instance" "main" {
  identifier              = "${var.project_name}-${var.environment}"
  engine                  = "postgres"
  engine_version          = var.db_engine_version
  instance_class          = var.db_instance_class
  allocated_storage       = var.db_allocated_storage
  storage_type            = "gp3"
  storage_encrypted       = true
  db_name                 = var.db_name
  username                = var.db_username
  password                = random_password.db_password.result
  parameter_group_name    = aws_db_parameter_group.main.name
  db_subnet_group_name    = aws_db_subnet_group.main.name
  vpc_security_group_ids  = [aws_security_group.db.id]
  multi_az                = var.db_multi_az
  backup_retention_period = 7
  skip_final_snapshot     = var.environment != "prod"
  deletion_protection     = var.environment == "prod"

  # IAM database authentication を有効化（Sprint 2 / 2.7 セキュリティ）
  # 本番運用は IAM 認証トークンに移行予定（Sprint 4）
  iam_database_authentication_enabled = true

  lifecycle {
    ignore_changes = [password]
  }
}

# pgvector 拡張の有効化は接続後 SQL 実行が必要：
#   CREATE EXTENSION IF NOT EXISTS vector;
# Alembic マイグレーションの初回で流す想定。
