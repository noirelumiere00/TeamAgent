# ============================================================
# ECS Fargate — OpenClaw外殻(L0) + TeamAgent-MCP境界（§H / M2・P1パイロット）
# ============================================================
# 設計（§A/§C/§D/§G）:
#   - 2タスク・別IAMロール: OpenClaw=Bedrock InvokeModel のみ＋Secrets/KMS/RDS明示Deny /
#     teamagent-mcp=対象Secret・KMS decrypt・rds connect・Bedrock。
#   - SG で mcpnet 相当を再現: MCP の 8787 は OpenClaw SG からのみ ingress。外部 inbound なし。
#   - OpenClaw は Slack(Socket Mode wss) と Bedrock へ egress するため public subnet+egress（worker と同型）。
#     真の private 化は VPC endpoint（vpc_endpoints.tf・任意）で Secrets/Bedrock/ECR を内部化。
#   - 2タスク間通信は Cloud Map(private DNS) で teamagent-mcp を解決（openclaw.json の url と一致させる）。
#   ⚠️ apply は本人/targeted。secret は本人が Secrets Manager に作成（値はここに無い）。image は ECR push 後に tfvars 指定。

locals {
  account_id = data.aws_caller_identity.current.account_id

  bedrock_resources = [
    "arn:aws:bedrock:*::foundation-model/*",
    "arn:aws:bedrock:${var.aws_region}:${local.account_id}:inference-profile/*",
  ]
}

# secret は本人が事前に Secrets Manager へ作成（値は tf に無い）。名前から full ARN(suffix込)を解決して
# valueFrom / execution role に使う＝ECS が確実に注入できる。plan 時点で secret 実在が前提（=正しい順序）。
data "aws_secretsmanager_secret" "bearer" {
  name = var.mcp_bearer_secret_name
}
data "aws_secretsmanager_secret" "database_url" {
  name = var.database_url_secret_name
}
data "aws_secretsmanager_secret" "slack_bot" {
  name = var.slack_bot_token_secret_name
}
data "aws_secretsmanager_secret" "slack_app" {
  name = var.slack_app_token_secret_name
}
data "aws_secretsmanager_secret" "gateway_token" {
  name = var.openclaw_gateway_token_secret_name
}
# §M改(VSEO有効化): Gemini 認証は本番EC2と同方式の Vertex SA（teamagent/dev/vertex_sa）。
# entrypoint ラッパが SA JSON をファイル化して ADC に渡す（scripts/run_mcp_vertex_entrypoint.sh）。
data "aws_secretsmanager_secret" "vertex_sa" {
  count = var.enable_scrape_tools ? 1 : 0
  name  = var.vertex_sa_secret_name
}

# ---------- クラスタ ----------
resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# ---------- CloudWatch Logs（タスク別） ----------
resource "aws_cloudwatch_log_group" "openclaw" {
  name              = "/${var.project_name}/${var.environment}/openclaw"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "mcp" {
  name              = "/${var.project_name}/${var.environment}/teamagent-mcp"
  retention_in_days = 30
}

# ---------- Cloud Map（2タスク間 private DNS） ----------
resource "aws_service_discovery_private_dns_namespace" "internal" {
  name        = "${var.project_name}.internal"
  description = "TeamAgent Fargate 内部サービス解決（OpenClaw → teamagent-mcp）"
  vpc         = data.aws_vpc.default.id
}

resource "aws_service_discovery_service" "mcp" {
  name = "teamagent-mcp"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }
  health_check_custom_config {
    failure_threshold = 1
  }
}

# ============================================================
# IAM
# ============================================================
data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# --- 実行ロール: タスク別に分割（§J）---
# ⚠️ 実行ロール(launch時の secrets 注入)はタスクロールの Deny の影響を受けない。共有にすると
# OpenClaw の実行ロールが database-url を読めてしまい「OpenClaw=営業データ非接触」が崩れる。
# → OpenClaw実行ロールは database-url を持たない／MCP実行ロールだけが持つ。両者とも ECR pull/Logs は共通。

# OpenClaw 実行ロール: bearer / slack-bot / slack-app / gateway-token のみ（database-url は不可）
resource "aws_iam_role" "ecs_execution_openclaw" {
  name               = "${var.project_name}-${var.environment}-ecs-exec-openclaw"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_openclaw_managed" {
  role       = aws_iam_role.ecs_execution_openclaw.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_openclaw_secrets" {
  statement {
    sid     = "ReadOpenClawSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      data.aws_secretsmanager_secret.bearer.arn,
      data.aws_secretsmanager_secret.slack_bot.arn,
      data.aws_secretsmanager_secret.slack_app.arn,
      data.aws_secretsmanager_secret.gateway_token.arn,
    ]
  }
}

resource "aws_iam_role_policy" "ecs_execution_openclaw_secrets" {
  name   = "${var.project_name}-${var.environment}-ecs-exec-openclaw-secrets"
  role   = aws_iam_role.ecs_execution_openclaw.id
  policy = data.aws_iam_policy_document.ecs_execution_openclaw_secrets.json
}

# MCP 実行ロール: bearer / database-url のみ
resource "aws_iam_role" "ecs_execution_mcp" {
  name               = "${var.project_name}-${var.environment}-ecs-exec-mcp"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_mcp_managed" {
  role       = aws_iam_role.ecs_execution_mcp.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_mcp_secrets" {
  statement {
    sid     = "ReadMcpSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = concat([
      data.aws_secretsmanager_secret.bearer.arn,
      data.aws_secretsmanager_secret.database_url.arn,
    ], var.enable_scrape_tools ? [data.aws_secretsmanager_secret.vertex_sa[0].arn] : [])
  }
}

resource "aws_iam_role_policy" "ecs_execution_mcp_secrets" {
  name   = "${var.project_name}-${var.environment}-ecs-exec-mcp-secrets"
  role   = aws_iam_role.ecs_execution_mcp.id
  policy = data.aws_iam_policy_document.ecs_execution_mcp_secrets.json
}

# --- OpenClaw タスクロール: Bedrock InvokeModel のみ ＋ Secrets/KMS/RDS 明示 Deny ---
data "aws_iam_policy_document" "openclaw_task" {
  statement {
    sid       = "BedrockInvokeOnly"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = local.bedrock_resources
  }
  statement {
    sid    = "DenySalesDataReach"
    effect = "Deny"
    actions = [
      "secretsmanager:*",
      "kms:*",
      "rds:*",
      "rds-db:*",
      "rds-data:*",
      # §Q-Q2: 金庫の中身が DynamoDB(aiia per-user token 4表)にも広がったため明示 Deny に追加
      #（reminder/notified は平文の per-user 状態＝kms Deny では守れない・将来の誤付与への保険）。
      "dynamodb:*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role" "openclaw_task" {
  name               = "${var.project_name}-${var.environment}-openclaw-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "openclaw_task" {
  name   = "${var.project_name}-${var.environment}-openclaw-task"
  role   = aws_iam_role.openclaw_task.id
  policy = data.aws_iam_policy_document.openclaw_task.json
}

# --- MCP タスクロール: 対象Secret / KMS decrypt / Bedrock / rds connect ---
data "aws_iam_policy_document" "mcp_task" {
  statement {
    sid     = "ReadTargetSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    # §J: wildcard をやめ MCP が runtime に必要な secret に限定（Slack tokens 等は対象外）。
    # §M: 拡張版は Gemini も追加。
    resources = concat([
      data.aws_secretsmanager_secret.bearer.arn,
      data.aws_secretsmanager_secret.database_url.arn,
    ], var.enable_scrape_tools ? [data.aws_secretsmanager_secret.vertex_sa[0].arn] : [])
  }
  statement {
    sid       = "KmsDecrypt"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${var.aws_region}:${local.account_id}:key/*"]
  }
  # §M: VSEO レポートの非公開S3発行（vseo-reports/ prefix に限定・presigned 用）。拡張版のみ。
  dynamic "statement" {
    for_each = var.enable_scrape_tools ? [1] : []
    content {
      sid     = "VseoReportS3"
      actions = ["s3:PutObject", "s3:GetObject", "s3:GetBucketLocation"]
      resources = [
        aws_s3_bucket.raw_files.arn,
        "${aws_s3_bucket.raw_files.arn}/vseo-reports/*",
      ]
    }
  }
  statement {
    sid = "BedrockInvoke"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
    ]
    resources = local.bedrock_resources
  }
}

resource "aws_iam_role" "mcp_task" {
  name               = "${var.project_name}-${var.environment}-mcp-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "mcp_task" {
  name   = "${var.project_name}-${var.environment}-mcp-task"
  role   = aws_iam_role.mcp_task.id
  policy = data.aws_iam_policy_document.mcp_task.json
}

# ============================================================
# Security Groups（mcpnet 相当: MCP 8787 は OpenClaw からのみ）
# ============================================================
resource "aws_security_group" "openclaw" {
  name        = "${var.project_name}-${var.environment}-openclaw-sg"
  description = "OpenClaw gateway (egress only: Slack/Bedrock/MCP)"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project_name}-${var.environment}-openclaw-sg" }
}

resource "aws_security_group" "mcp" {
  name        = "${var.project_name}-${var.environment}-mcp-sg"
  description = "TeamAgent-MCP backend (ingress 8787 from OpenClaw only)"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "MCP streamable-http from OpenClaw only"
    from_port       = 8787
    to_port         = 8787
    protocol        = "tcp"
    security_groups = [aws_security_group.openclaw.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project_name}-${var.environment}-mcp-sg" }
}

# RDS への 5432 を MCP SG からのみ許可（純加算ルール・db SG は rds.tf）
resource "aws_security_group_rule" "db_from_mcp" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.mcp.id
  security_group_id        = aws_security_group.db.id
  description              = "PostgreSQL from teamagent-mcp Fargate"
}

# ============================================================
# Task Definitions
# ============================================================
resource "aws_ecs_task_definition" "mcp" {
  family                   = "${var.project_name}-${var.environment}-mcp"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.fargate_mcp_cpu
  memory                   = var.fargate_mcp_memory
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }
  execution_role_arn = aws_iam_role.ecs_execution_mcp.arn
  task_role_arn      = aws_iam_role.mcp_task.arn

  container_definitions = jsonencode([merge({
    name         = "teamagent-mcp"
    image        = var.mcp_image
    essential    = true
    portMappings = [{ containerPort = 8787, protocol = "tcp" }]
    environment = concat([
      { name = "AWS_REGION", value = var.aws_region },
      { name = "TEAMAGENT_MCP_HOST", value = "0.0.0.0" },
      { name = "TEAMAGENT_MCP_PORT", value = "8787" },
      { name = "TEAMAGENT_MCP_PATH", value = "/mcp" },
      { name = "TEAMAGENT_SHARED_COMPANY_DOMAINS", value = var.shared_company_domains },
      ], var.enable_scrape_tools ? [
      # §M: video_algorithm が VSEO レポートを発行する非公開S3 bucket（presigned URL を出力に載せる）。
      { name = "VSEO_REPORT_BUCKET", value = aws_s3_bucket.raw_files.id },
      { name = "USE_VIDEO_TOOLS", value = "1" },
      { name = "USE_TIKTOK_TOOLS", value = "1" },
      # §M改: Gemini は Vertex（本番EC2 .env.production と同値・SA JSON は entrypoint がファイル化）。
      { name = "GEMINI_USE_VERTEX", value = "true" },
      { name = "GEMINI_VERTEX_PROJECT", value = var.gemini_vertex_project },
      { name = "GEMINI_VERTEX_LOCATION", value = var.gemini_vertex_location },
    ] : [])
    secrets = concat([
      { name = "TEAMAGENT_MCP_BEARER", valueFrom = data.aws_secretsmanager_secret.bearer.arn },
      { name = "DATABASE_URL", valueFrom = data.aws_secretsmanager_secret.database_url.arn },
      ], var.enable_scrape_tools ? [
      { name = "VERTEX_SA_JSON", valueFrom = data.aws_secretsmanager_secret.vertex_sa[0].arn },
    ] : [])
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.mcp.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "mcp"
      }
    }
    healthCheck = {
      command     = ["CMD-SHELL", "curl -fsS http://127.0.0.1:8787/healthz || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 5
      startPeriod = 40
    }
    }, var.enable_scrape_tools ? {
    # §M改: 拡張版のみ SA JSON ファイル化ラッパで起動（既定はイメージの CMD のまま＝挙動不変）。
    command = ["sh", "scripts/run_mcp_vertex_entrypoint.sh"]
  } : {})])
}

resource "aws_ecs_task_definition" "openclaw" {
  family                   = "${var.project_name}-${var.environment}-openclaw"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.fargate_openclaw_cpu
  memory                   = var.fargate_openclaw_memory
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }
  execution_role_arn = aws_iam_role.ecs_execution_openclaw.arn
  task_role_arn      = aws_iam_role.openclaw_task.arn

  # §R(go-live): Fargate の空ボリュームは root 所有で、公式OpenClawイメージは非root(node uid1000)。
  # readonly rootfs＋root所有volume だと node が /home/node/.openclaw に書けず crash（実測）。
  # P1 は readonly rootfs と volume を外し、node が自分の HOME(書込み可ephemeral層)に state を持つ。
  # ＝会話メモリはタスク再起動で揮発（既知のP1制限どおり）。readonly rootfs と state永続化は
  # P2 で EFS access point(uid/gid 1000) で両立する（要 EFS 作成）。
  container_definitions = jsonencode([{
    name      = "openclaw"
    image     = var.openclaw_image
    essential = true
    # §S診断: Slack@mention無反応の切り分け用に debug ログを有効化（ソケット接続/イベント受信を可視化）。
    # CMD(Dockerfile)に --log-level debug を前置（グローバルフラグはsubcommand前）。安定後は外す。
    command = ["node", "dist/index.js", "--log-level", "debug", "gateway", "--bind", "loopback", "--port", "18789"]
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "OPENCLAW_CONFIG_PATH", value = "/opt/teamagent/openclaw.json" },
    ]
    secrets = [
      { name = "TEAMAGENT_MCP_BEARER", valueFrom = data.aws_secretsmanager_secret.bearer.arn },
      { name = "SLACK_BOT_TOKEN", valueFrom = data.aws_secretsmanager_secret.slack_bot.arn },
      { name = "SLACK_APP_TOKEN", valueFrom = data.aws_secretsmanager_secret.slack_app.arn },
      { name = "OPENCLAW_GATEWAY_TOKEN", valueFrom = data.aws_secretsmanager_secret.gateway_token.arn },
    ]
    # §O: gateway healthz（loopback:18789）。docker-compose.yml:77-86 と同形（curl 非同梱のため node fetch）。
    healthCheck = {
      command = [
        "CMD",
        "node",
        "-e",
        "fetch('http://127.0.0.1:18789/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
      ]
      interval    = 30
      timeout     = 5
      retries     = 5
      startPeriod = 40
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.openclaw.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "openclaw"
      }
    }
  }])
}

# ============================================================
# Services（desired=1・public subnet+egress・SGで inbound遮断）
# ============================================================
resource "aws_ecs_service" "mcp" {
  count           = var.mcp_image == "" ? 0 : 1
  name            = "${var.project_name}-${var.environment}-mcp"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.mcp.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.mcp.id]
    assign_public_ip = true # default subnet=public。inbound は SG で遮断、egress(RDS/Secrets/Bedrock)用。
  }
  service_registries {
    registry_arn = aws_service_discovery_service.mcp.arn
  }
}

resource "aws_ecs_service" "openclaw" {
  count           = var.openclaw_image == "" ? 0 : 1
  name            = "${var.project_name}-${var.environment}-openclaw"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.openclaw.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.openclaw.id]
    assign_public_ip = true # Slack(Socket Mode wss)/Bedrock への egress 用。inbound は無し。
  }
  # §J: depends_on は撤去（OpenClaw は MCP へ起動後に再接続。count-gated service への depends_on を回避）。
}

output "mcp_service_dns" {
  description = "OpenClaw の openclaw.json で使う MCP の内部DNS（url=http://<this>:8787/mcp）"
  value       = "teamagent-mcp.${aws_service_discovery_private_dns_namespace.internal.name}"
}
