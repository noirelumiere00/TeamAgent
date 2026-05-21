# ============================================================
# RDS PostgreSQL + pgvector
# ============================================================

resource "random_password" "db_password" {
  length  = 32
  special = true
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

  parameter {
    name  = "shared_preload_libraries"
    value = "pg_stat_statements"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"  # 1秒以上のクエリをログ
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

  # Lambda からのアクセス想定。実運用では Lambda SG からのみ許可に絞る
  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_instance" "main" {
  identifier             = "${var.project_name}-${var.environment}"
  engine                 = "postgres"
  engine_version         = var.db_engine_version
  instance_class         = var.db_instance_class
  allocated_storage      = var.db_allocated_storage
  storage_type           = "gp3"
  storage_encrypted      = true
  db_name                = var.db_name
  username               = var.db_username
  password               = random_password.db_password.result
  parameter_group_name   = aws_db_parameter_group.main.name
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  multi_az               = var.db_multi_az
  backup_retention_period = 7
  skip_final_snapshot    = var.environment != "prod"
  deletion_protection    = var.environment == "prod"

  lifecycle {
    ignore_changes = [password]
  }
}

# pgvector 拡張の有効化は接続後 SQL 実行が必要：
#   CREATE EXTENSION IF NOT EXISTS vector;
# Alembic マイグレーションの初回で流す想定。
