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
    # §Q-Q2: aiia-mcp も endpoint 経由（ECR pull/Secrets注入/Logs/KMS/Bedrock）。
    # ここに入れ忘れると private_dns_enabled=true のため当該タスクだけ 443 が落ち provisioning ループになる。
    security_groups = concat(
      [aws_security_group.openclaw.id, aws_security_group.mcp.id],
      var.enable_aiia_mcp ? [aws_security_group.aiia_mcp[0].id] : [],
      # §U-Part3-Step C: morning_digest Scheduled Task も VPC endpoint 経由（SM/KMS/Bedrock/Logs）。
      # ingest_schedule と同様に追加忘れると 443 が落ちて provisioning ループになる必須配線。
      var.enable_morning_digest ? [aws_security_group.morning_digest[0].id] : [],
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
  for_each            = toset(local.vpce_services)
  vpc_id              = data.aws_vpc.default.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = data.aws_subnets.default.ids
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

# §Q-Q2: DynamoDB gateway endpoint（無料）— aiia-mcp の本務（per-user token 4表）を private 経路に。
# これが無いと DynamoDB だけ IGW 依存＝将来 public IP を外すと真っ先に切れる。
resource "aws_vpc_endpoint" "dynamodb" {
  count             = var.enable_vpc_endpoints ? 1 : 0
  vpc_id            = data.aws_vpc.default.id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = data.aws_route_tables.default[0].ids

  tags = { Name = "${var.project_name}-${var.environment}-vpce-dynamodb" }
}
