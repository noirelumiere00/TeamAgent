# ============================================================
# EC2 踏み台ホスト（SSM Session Manager 経由）
# ============================================================
# - SSH キー不要、IAM Role + SSM Agent でアクセス
# - psql クライアントから RDS に接続する用途
# - t4g.nano（≒$3/月）
#
# 使い方:
#   aws ssm start-session --target <instance-id> --region ap-northeast-1
#   # 踏み台内で:
#   sudo dnf install -y postgresql16
#   psql -h <rds-endpoint> -U teamagent -d teamagent
# ============================================================

# 最新の Amazon Linux 2023 ARM AMI を取得
data "aws_ami" "al2023_arm" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023*-arm64"]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}

# 踏み台用 IAM Role（SSM Session Manager 接続を許可）
data "aws_iam_policy_document" "bastion_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "bastion" {
  name               = "${var.project_name}-${var.environment}-bastion"
  assume_role_policy = data.aws_iam_policy_document.bastion_assume.json
}

resource "aws_iam_role_policy_attachment" "bastion_ssm" {
  role       = aws_iam_role.bastion.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# DB パスワードを Secrets Manager から取得する権限
data "aws_iam_policy_document" "bastion_secrets" {
  statement {
    sid     = "ReadDBPassword"
    actions = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [
      aws_secretsmanager_secret.db_password.arn,
    ]
  }
}

resource "aws_iam_role_policy" "bastion_secrets" {
  name   = "${var.project_name}-${var.environment}-bastion-secrets"
  role   = aws_iam_role.bastion.id
  policy = data.aws_iam_policy_document.bastion_secrets.json
}

resource "aws_iam_instance_profile" "bastion" {
  name = "${var.project_name}-${var.environment}-bastion"
  role = aws_iam_role.bastion.name
}

# 踏み台 SG：受信は一切なし（SSM 経由のみ）、送信は全許可（DB / Bedrock など）
resource "aws_security_group" "bastion" {
  name        = "${var.project_name}-${var.environment}-bastion-sg"
  description = "TeamAgent bastion SG"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "bastion" {
  ami                    = data.aws_ami.al2023_arm.id
  instance_type          = "t4g.nano"
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.bastion.id]
  iam_instance_profile   = aws_iam_instance_profile.bastion.name

  # PostgreSQL クライアント事前インストール
  user_data = <<-EOF
    #!/bin/bash
    set -e
    dnf install -y postgresql16
  EOF

  tags = {
    Name = "${var.project_name}-${var.environment}-bastion"
  }

  # data.aws_ami.al2023_arm は most_recent=true のため AWS が新 AMI を公開するたび
  # id が変わり、ami は ForceNew → 無関係な apply（例: morning_digest）で踏み台が
  # 巻き添え replace される footgun。AMI ドリフトは無視し、更新は意図的な taint で行う。
  lifecycle {
    ignore_changes = [ami]
  }
}
