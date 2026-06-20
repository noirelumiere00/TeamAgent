# ============================================================
# §U: connect-web — Google OAuth callback receiver（4つ目の Fargate タスク）
# ============================================================
# 役割: per-user Google 連携の callback (`https://connect.newstv.co.jp/oauth2/callback`) を
#   受け、state 検証→token 交換→KMS 暗号化→RDS oauth_tokens 保存まで担う。本番は EC2
#   `teamagent-connect.service` で稼働中だが、SM endpoint SG ingress 漏れ等の運用ハマリの
#   元になるため Fargate に移行して EC2 worker 完全廃止を目指す（§U・2026-06-18 着手）。
#
# 通信: ALB(`teamagent-connectweb-alb`、internal) → connect-web:8788 (このタスク)
#   → 外向き: Secrets Manager / KMS / RDS / Bedrock (callback で token を KMS 暗号化して保存)
#   外部公開は API Gateway カスタムドメイン (connect.newstv.co.jp) → ALB 経由のみ。
#
# image: 既存 teamagent-mcp の ECR image をそのまま流用（teamagent パッケージ同一・command 差分のみ）。
#   ECR/CodeBuild の新規追加は不要 → 差分最小。command で `uv run python -m teamagent.connect_web`。
#
# 段階ゲート: enable_connect_web=true で生える。ALB 切替は Terraform 外で aws elbv2 modify-rule
#   による weighted target group 切替で段階移行する（Phase 1.0〜1.5）。

# ---------- 変数 ----------
variable "enable_connect_web" {
  description = "connect-web (Fargate) の secret 参照/IAM/SG/CloudMap/taskdef/service/TG を有効化"
  type        = bool
  default     = false
}

variable "fargate_connect_cpu" {
  description = "connect-web タスク CPU (uvicorn FastAPI のみ＝軽量)"
  type        = number
  default     = 512
}

variable "fargate_connect_memory" {
  description = "connect-web タスク メモリ MB"
  type        = number
  default     = 1024
}

variable "connect_oauth_state_secret_name" {
  description = "OAUTH_STATE_SECRET の Secrets Manager 名（callback の HMAC state 検証鍵・EC2 と同一値必須）"
  type        = string
  default     = "teamagent/dev/oauth_state_secret"
}

variable "connect_google_client_secret_name" {
  description = "CONNECT_GOOGLE_CLIENT_SECRET の Secrets Manager 名（Web型クライアント pgd1mj4 の client_secret）"
  type        = string
  default     = "teamagent/dev/connect_google_secret"
}

variable "connect_google_client_id" {
  description = "CONNECT_GOOGLE_CLIENT_ID（Web型クライアント・平文 env で OK）"
  type        = string
  default     = "676659122211-pgd1mj4et6sf7uqqmsni2b3kmbbd8qeg.apps.googleusercontent.com"
}

variable "connect_redirect_uri" {
  description = "OAUTH_REDIRECT_URI（GCP コンソール登録済 callback URL）"
  type        = string
  default     = "https://connect.newstv.co.jp/oauth2/callback"
}

variable "connect_base_url" {
  description = "CONNECT_BASE_URL（本人連携リンクの base・本人通知メッセージで使う）"
  type        = string
  default     = "https://connect.newstv.co.jp"
}

variable "connect_oauth_kms_key_id" {
  description = "OAUTH_KMS_KEY_ID（oauth_tokens 暗号化に使う既存 KMS 鍵の ARN・既定 alias 解決）"
  type        = string
  default     = "" # 空なら data.aws_kms_alias.aiia_oauth で解決した key arn を使用（aiia と共用）
}

variable "connect_web_vpc_cidr" {
  description = "connect-web の ingress を許可する VPC CIDR（ALB→Fargate の経路・internal ALB は VPC 内）"
  type        = string
  default     = "172.31.0.0/16"
}

# §V: メールサマリーのインタラクティブ・ボタン（/slack/interactivity を connect-web で host）。
# enable_interactive_mail=true で connect-web タスクに署名検証鍵 + 第2 App bot token を注入し、
# task role に Bedrock（「対応する」の下書き生成）を許可する。secret は本人が Secrets Manager に作成。
variable "enable_interactive_mail" {
  description = "メールサマリーのボタン（/slack/interactivity）を有効化（署名鍵/第2App token/Bedrock を connect-web に配線）"
  type        = bool
  default     = false
}

variable "interactive_mail_signing_secret_name" {
  description = "SLACK_SIGNING_SECRET の Secrets Manager 名（インタラクティブ用 第2 Slack App の署名シークレット）"
  type        = string
  default     = "teamagent/dev/interactive_mail/signing_secret"
}

variable "interactive_mail_bot_token_secret_name" {
  description = "INTERACTIVE_MAIL_BOT_TOKEN の Secrets Manager 名（第2 Slack App の xoxb・本人解決/投稿用）"
  type        = string
  default     = "teamagent/dev/interactive_mail/bot_token"
}

# ---------- CloudWatch Logs（非ゲート・無害） ----------
resource "aws_cloudwatch_log_group" "connect_web" {
  name              = "/${var.project_name}/${var.environment}/connect-web"
  retention_in_days = 30
}

# ---------- 以降は enable_connect_web ゲート ----------

# 連携用 OAuth Web 型クライアントの client_secret（PR #127 / dev e7ca482 と同パターン）
data "aws_secretsmanager_secret" "connect_oauth_state" {
  count = var.enable_connect_web ? 1 : 0
  name  = var.connect_oauth_state_secret_name
}
data "aws_secretsmanager_secret" "connect_google_client_secret" {
  count = var.enable_connect_web ? 1 : 0
  name  = var.connect_google_client_secret_name
}

# KMS 鍵：aiia と同じ `alias/teamagent-oauth-tokens` を共用（oauth_tokens 暗号化）。
# enable_aiia_mcp が false でも単独で解決できるよう data を別途定義。
data "aws_kms_alias" "connect_oauth" {
  count = var.enable_connect_web ? 1 : 0
  name  = "alias/teamagent-oauth-tokens"
}

# §V interactivity 用 secret（enable_interactive_mail ゲート・本人が Secrets Manager に作成）。
data "aws_secretsmanager_secret" "interactive_mail_signing" {
  count = var.enable_interactive_mail ? 1 : 0
  name  = var.interactive_mail_signing_secret_name
}
data "aws_secretsmanager_secret" "interactive_mail_bot_token" {
  count = var.enable_interactive_mail ? 1 : 0
  name  = var.interactive_mail_bot_token_secret_name
}

# --- 実行ロール（launch 時 secrets 注入用・connect 関連 secret + database_url のみ） ---
resource "aws_iam_role" "ecs_execution_connect_web" {
  count              = var.enable_connect_web ? 1 : 0
  name               = "${var.project_name}-${var.environment}-ecs-exec-connect-web"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_connect_web_managed" {
  count      = var.enable_connect_web ? 1 : 0
  role       = aws_iam_role.ecs_execution_connect_web[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_connect_web_secrets" {
  count = var.enable_connect_web ? 1 : 0
  statement {
    sid     = "ReadConnectWebSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    # interactive 用 2 secret は splat で加算（enable_interactive_mail=false なら [] で増えない）。
    resources = concat(
      [
        data.aws_secretsmanager_secret.connect_oauth_state[0].arn,
        data.aws_secretsmanager_secret.connect_google_client_secret[0].arn,
        data.aws_secretsmanager_secret.database_url.arn,
      ],
      data.aws_secretsmanager_secret.interactive_mail_signing[*].arn,
      data.aws_secretsmanager_secret.interactive_mail_bot_token[*].arn,
    )
  }
}

resource "aws_iam_role_policy" "ecs_execution_connect_web_secrets" {
  count  = var.enable_connect_web ? 1 : 0
  name   = "${var.project_name}-${var.environment}-ecs-exec-connect-web-secrets"
  role   = aws_iam_role.ecs_execution_connect_web[0].id
  policy = data.aws_iam_policy_document.ecs_execution_connect_web_secrets[0].json
}

# --- タスクロール: KMS Encrypt/Decrypt（oauth_tokens 暗号化）・RDS connect 経由 ---
# connect-web は callback で token を KMS 暗号化して RDS に保存する。Decrypt は将来再連携時に必要。
# Bedrock は不要（Skill 実行は teamagent-mcp の責務）。
data "aws_iam_policy_document" "connect_web_task" {
  count = var.enable_connect_web ? 1 : 0
  statement {
    sid     = "KmsEncryptDecryptOauthTokens"
    actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
    resources = [
      data.aws_kms_alias.connect_oauth[0].target_key_arn,
    ]
  }
  # §V: 「対応する」ボタンが mail_reply（Bedrock 起草）をインプロセス実行するため、
  # interactivity 有効時のみ Bedrock InvokeModel を許可する。
  dynamic "statement" {
    for_each = var.enable_interactive_mail ? [1] : []
    content {
      sid = "BedrockInvokeForMailReply"
      actions = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
      ]
      resources = local.bedrock_resources
    }
  }
}

resource "aws_iam_role" "connect_web_task" {
  count              = var.enable_connect_web ? 1 : 0
  name               = "${var.project_name}-${var.environment}-connect-web-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "connect_web_task" {
  count  = var.enable_connect_web ? 1 : 0
  name   = "${var.project_name}-${var.environment}-connect-web-task"
  role   = aws_iam_role.connect_web_task[0].id
  policy = data.aws_iam_policy_document.connect_web_task[0].json
}

# --- SG: 8788 を VPC 内（internal ALB）からのみ許可 ---
# 内部 ALB は VPC 内に存在し、API Gateway VPC link 経由でのみアクセスされる。
# Fargate task の ENI は VPC 内 IP を持つので、CIDR ingress で十分。ALB SG が存在する場合は
# ここを `security_groups = [aws_security_group.alb_connect.id]` に絞ると更に堅牢（将来の P2 硬化）。
resource "aws_security_group" "connect_web" {
  count       = var.enable_connect_web ? 1 : 0
  name        = "${var.project_name}-${var.environment}-connect-web-sg"
  description = "connect-web Fargate (ingress 8788 from internal ALB / VPC only)"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "OAuth callback receiver from internal ALB (VPC-internal only)"
    from_port   = 8788
    to_port     = 8788
    protocol    = "tcp"
    cidr_blocks = [var.connect_web_vpc_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project_name}-${var.environment}-connect-web-sg" }
}

# RDS への 5432 を connect-web SG から許可（既存 db_from_mcp と同型・純加算）
resource "aws_security_group_rule" "db_from_connect_web" {
  count                    = var.enable_connect_web ? 1 : 0
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.connect_web[0].id
  security_group_id        = aws_security_group.db.id
  description              = "PostgreSQL from connect-web Fargate"
}

# --- Cloud Map: connect-web.teamagent.internal（observability/将来の内部呼出用・必須ではない） ---
resource "aws_service_discovery_service" "connect_web" {
  count = var.enable_connect_web ? 1 : 0
  name  = "connect-web"

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

# --- ALB Target Group: target_type=ip (Fargate 専用) ---
# 既存の EC2 用 TG `teamagent-connectweb-tg` (target_type=instance) は手動作成のため触らない。
# 新規に Fargate 用 TG を作成し、ALB の listener rule で weighted target group 切替で段階移行する。
# ALB の listener rule 変更は Terraform 外（aws elbv2 modify-rule）で実施。
resource "aws_lb_target_group" "connect_web_fargate" {
  count       = var.enable_connect_web ? 1 : 0
  name        = "${var.project_name}-${var.environment}-connect-fg-tg"
  port        = 8788
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = data.aws_vpc.default.id

  health_check {
    enabled             = true
    path                = "/healthz"
    port                = "8788"
    protocol            = "HTTP"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 2
    matcher             = "200"
  }

  # Fargate task 再 deploy 時の 502 を避けるため slow deregistration
  deregistration_delay = 30
}

# --- Task Definition ---
# image は既存 teamagent-mcp を流用（teamagent パッケージ同一・コマンド差分のみ）。
# command で `uv run python -m teamagent.connect_web` を指定して connect-web として起動。
# CONNECT_WEB_HOST=0.0.0.0 で 0.0.0.0:8788 にバインド（コンテナ外からアクセス可）。
resource "aws_ecs_task_definition" "connect_web" {
  count                    = var.enable_connect_web && var.mcp_image != "" ? 1 : 0
  family                   = "${var.project_name}-${var.environment}-connect-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.fargate_connect_cpu
  memory                   = var.fargate_connect_memory
  execution_role_arn       = aws_iam_role.ecs_execution_connect_web[0].arn
  task_role_arn            = aws_iam_role.connect_web_task[0].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name      = "connect-web"
    image     = var.mcp_image
    essential = true
    command   = ["python", "-m", "teamagent.connect_web"]
    portMappings = [
      { containerPort = 8788, protocol = "tcp" },
    ]
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "CONNECT_WEB_HOST", value = "0.0.0.0" },
      { name = "CONNECT_WEB_PORT", value = "8788" },
      { name = "OAUTH_REDIRECT_URI", value = var.connect_redirect_uri },
      { name = "CONNECT_BASE_URL", value = var.connect_base_url },
      { name = "CONNECT_GOOGLE_CLIENT_ID", value = var.connect_google_client_id },
      { name = "OAUTH_KMS_KEY_ID", value = var.connect_oauth_kms_key_id != "" ? var.connect_oauth_kms_key_id : data.aws_kms_alias.connect_oauth[0].target_key_arn },
      { name = "OAUTH_KMS_REGION", value = var.aws_region },
      { name = "STRUCTLOG_FORMAT", value = "json" },
    ]
    secrets = concat(
      [
        { name = "OAUTH_STATE_SECRET", valueFrom = data.aws_secretsmanager_secret.connect_oauth_state[0].arn },
        { name = "CONNECT_GOOGLE_CLIENT_SECRET", valueFrom = data.aws_secretsmanager_secret.connect_google_client_secret[0].arn },
        { name = "DATABASE_URL", valueFrom = data.aws_secretsmanager_secret.database_url.arn },
      ],
      # §V interactivity（enable_interactive_mail=false なら splat が空＝増えない）。
      [for arn in data.aws_secretsmanager_secret.interactive_mail_signing[*].arn :
      { name = "SLACK_SIGNING_SECRET", valueFrom = arn }],
      [for arn in data.aws_secretsmanager_secret.interactive_mail_bot_token[*].arn :
      { name = "INTERACTIVE_MAIL_BOT_TOKEN", valueFrom = arn }],
    )
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.connect_web.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "connect-web"
      }
    }
    healthCheck = {
      command     = ["CMD-SHELL", "curl -fsS http://127.0.0.1:8788/healthz || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 5
      startPeriod = 30
    }
  }])
}

# --- ECS Service ---
resource "aws_ecs_service" "connect_web" {
  count           = var.enable_connect_web && var.mcp_image != "" ? 1 : 0
  name            = "${var.project_name}-${var.environment}-connect-web"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.connect_web[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.connect_web[0].id]
    assign_public_ip = true # default subnet=public・inbound は SG で遮断
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.connect_web_fargate[0].arn
    container_name   = "connect-web"
    container_port   = 8788
  }

  service_registries {
    registry_arn = aws_service_discovery_service.connect_web[0].arn
  }

  # ALB target group が target_type=ip + healthcheck 30s なので startup grace を付けて
  # 初期 unhealthy で service が落ちないようにする。
  health_check_grace_period_seconds = 60

  depends_on = [
    aws_lb_target_group.connect_web_fargate,
  ]
}

# ---------- Outputs ----------
output "connect_web_target_group_arn" {
  description = "Fargate 用 ALB target group ARN（aws elbv2 modify-rule で weighted forward に使う）"
  value       = var.enable_connect_web ? aws_lb_target_group.connect_web_fargate[0].arn : ""
}

output "connect_web_service_dns" {
  description = "connect-web の Cloud Map 内部DNS（observability/将来の内部呼出用）"
  value       = var.enable_connect_web ? "connect-web.${aws_service_discovery_private_dns_namespace.internal.name}:8788" : ""
}

output "connect_web_log_group" {
  description = "CloudWatch Logs グループ（Phase 1 切替時の動作確認用）"
  value       = aws_cloudwatch_log_group.connect_web.name
}
