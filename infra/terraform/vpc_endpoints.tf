# ============================================================
# VPC Interface Endpoints（任意・enable_vpc_endpoints）§H 主要設計判断
# ============================================================
# 目的: Secrets/Bedrock/ECR/Logs を internet 経由せず private 解決＝OpenClaw/MCP の egress を締める。
#   ※ Slack(Socket Mode) は AWS外のため endpoint 化不可（egress は残る）。endpoint は ~$7/月×数=コスト。
#   ※ default VPC の subnet は public（IGW）。endpoint を入れると当該サービスは private 経路を優先。

resource "aws_security_group" "vpce" {
  count       = var.enable_vpc_endpoints ? 1 : 0
  name        = "${var.project_name}-${var.environment}-vpce-sg"
  description = "Interface endpoints: 443 from OpenClaw/MCP tasks only"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "HTTPS from Fargate tasks"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    # §U: connect-web も endpoint 経由（Secrets/KMS/Logs を VPC 内で解決）。本日 2026-06-18 インシデントの
    # 「EC2 worker SG 漏れで SM 不達」を Fargate 化に伴って繰り返さないための必須配線。
    # ここに入れ忘れると private_dns_enabled=true のため当該タスクだけ 443 が落ち provisioning ループになる。
    # （aiia-mcp の SG は #128 の退役で削除済み）
    security_groups = concat(
      [aws_security_group.openclaw.id, aws_security_group.mcp.id],
      # §U-Phase1: connect-web Fargate も VPC endpoint 経由（PR #129）。
      var.enable_connect_web ? [aws_security_group.connect_web[0].id] : [],
      # §U-Phase2: ingest Scheduled Task も VPC endpoint 経由（PR #129）。
      var.enable_ingest_schedule ? [aws_security_group.ingest[0].id] : [],
      # §U-Part3-Step C: morning_digest Scheduled Task も VPC endpoint 経由（PR #131）。
      # 追加忘れると private_dns_enabled=true のため 443 が落ち provisioning ループになる必須配線。
      var.enable_morning_digest ? [aws_security_group.morning_digest[0].id] : [],
      # 2026-06-26: tiktok-acquire 使い捨て Fargate も ECR pull/Logs/Dynamo を endpoint 経由で解決。
      # 上記同様、ここに入れ忘れると ECR auth pull が i/o timeout で TaskFailedToStart になる（実際に発生）。
      var.enable_tiktok_acquire ? [aws_security_group.tiktok_tasks[0].id] : [],
    )
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project_name}-${var.environment}-vpce-sg" }
}

locals {
  vpce_services = var.enable_vpc_endpoints ? [
    "bedrock-runtime",
    "secretsmanager",
    "kms",
    "ecr.api",
    "ecr.dkr",
    "logs",
  ] : []
}

resource "aws_vpc_endpoint" "interface" {
  for_each          = toset(local.vpce_services)
  vpc_id            = data.aws_vpc.default.id
  service_name      = "com.amazonaws.${var.aws_region}.${each.key}"
  vpc_endpoint_type = "Interface"
  # コスト最適化(2026-06-29): インターフェースエンドポイントを3AZ→1AZへ集約（恒久 約-$121/月）。
  # 既にCLIで1AZ化済み。本指定でterraform apply時も同状態を維持し、削減を巻き戻さない。
  # PrivateDNS有効のため単一AZのENIでも全AZのタスクから到達可能。3AZへ復元する場合は
  # data.aws_subnets.default.ids に戻す（可逆）。単一AZ障害域となる点は試験環境として許容。
  subnet_ids          = ["subnet-07e0d4e58b3b83b8a"]
  security_group_ids  = [aws_security_group.vpce[0].id]
  private_dns_enabled = true

  tags = { Name = "${var.project_name}-${var.environment}-vpce-${each.key}" }
}

# §J: S3 gateway endpoint — ECR の層blob は S3 配信。interface(ecr.api/ecr.dkr)だけでは層 pull が
# internet 経由のままで private 化が不成立。gateway endpoint は**無料**・route table に attach。
data "aws_route_tables" "default" {
  count  = var.enable_vpc_endpoints ? 1 : 0
  vpc_id = data.aws_vpc.default.id
}

resource "aws_vpc_endpoint" "s3" {
  count             = var.enable_vpc_endpoints ? 1 : 0
  vpc_id            = data.aws_vpc.default.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = data.aws_route_tables.default[0].ids

  tags = { Name = "${var.project_name}-${var.environment}-vpce-s3" }
}

# DynamoDB gateway endpoint（無料）— private 経路で DynamoDB アクセスを維持
# （aiia-mcp 退役後も terraform-tflock や将来用途のため endpoint は残す）。
resource "aws_vpc_endpoint" "dynamodb" {
  count             = var.enable_vpc_endpoints ? 1 : 0
  vpc_id            = data.aws_vpc.default.id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = data.aws_route_tables.default[0].ids

  tags = { Name = "${var.project_name}-${var.environment}-vpce-dynamodb" }
}
