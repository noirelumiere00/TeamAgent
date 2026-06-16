# ============================================================
# §Q-Q2: aiia-mcp — AiLaメール境界（3つ目のFargateタスク・金庫内）
# ============================================================
# 役割: AiLa(朝メール要約)のメール能力を OpenClaw から streamable-http MCP(bearer) でのみ
#   触れる「信頼境界の内側」に置く。per-user Google token(DynamoDB+KMS)・Gmail・Bedrock に
#   触れるのはこのタスクだけ。OpenClaw は slack_user_id を渡すのみ＝本人解決はサーバ側STRICT
#   （ソース: ~/Documents/AI-IA-UAE `src/aiia/mcp_server/`・Dockerfile=同repo infra/docker/）。
# 通信: OpenClaw SG → aiia-mcp:8788 のみ ingress（teamagent-mcp 8787 と同型・外部 inbound なし）。
#
# 段階ゲート（既存 plan を壊さない）:
#   - ECR repo / CodeBuild は非ゲート（イメージビルドが secret 作成より先に必要）。
#   - それ以外（secret 参照/IAM/SG/CloudMap/taskdef/service）は enable_aiia_mcp=true で生える。
#     有効化前に secret 4本を Secrets Manager へ作成（値は tf に無い・teamagent 流儀）:
#       teamagent/dev/aiia/mcp-bearer            … OpenClaw⟷aiia-mcp の bearer（新規生成）
#       teamagent/dev/aiia/google-client-id      … W1 OAuth client（.env.aila と同値）
#       teamagent/dev/aiia/google-client-secret  … 〃
#       teamagent/dev/aiia/oauth-state-secret    … reply_url 署名（.env.aila と同値）
#     SLACK_BOT_TOKEN は既存 teamagent/dev/openclaw/slack-bot-token を read-only 流用
#     （同じ共用Slackアプリ・users.info での本人解決にのみ使用。§Q-Q6 で送信(chat.postMessage)を
#       このタスクに足す場合は「送信経路なし」前提が変わる＝その時に再評価）。
# 🔴 §Q-Q3 結線時の規律: OpenClaw 実行ロール/taskdef に足してよいのは aiia_bearer の **1本だけ**。
#   google-client-id/secret・oauth-state-secret は絶対に外殻へ渡さない。bearer は teamagent-mcp 用と別値。
# ⚠️ apply は本人/targeted。DynamoDB 4表（aiia-*）と KMS alias/teamagent-oauth-tokens は作成済み前提。

# ---------- 変数（命名規則は variables_fargate.tf に準拠・add-only のため本ファイルに同居） ----------
variable "enable_aiia_mcp" {
  description = "aiia-mcp の secret参照/IAM/SG/CloudMap/taskdef/service を有効化（secret 4本の事前作成が前提）"
  type        = bool
  default     = false
}

variable "aiia_mcp_image" {
  description = "aiia-mcp イメージ (ECR URL@digest)。空なら taskdef/service とも作らない（push 後に digest 指定で有効化）"
  type        = string
  default     = ""
}

variable "fargate_aiia_cpu" {
  description = "aiia-mcp タスク CPU（E5/torch 無し＝軽量）"
  type        = number
  default     = 512
}

variable "fargate_aiia_memory" {
  description = "aiia-mcp タスクメモリ(MB)"
  type        = number
  default     = 1024
}

variable "aiia_mcp_bearer_secret_name" {
  description = "OpenClaw⟷aiia-mcp の bearer secret 名"
  type        = string
  default     = "teamagent/dev/aiia/mcp-bearer"
}

variable "aiia_google_client_id_secret_name" {
  description = "Google OAuth client_id（W1 Internal client・token refresh に必須）"
  type        = string
  default     = "teamagent/dev/aiia/google-client-id"
}

variable "aiia_google_client_secret_secret_name" {
  description = "Google OAuth client_secret"
  type        = string
  default     = "teamagent/dev/aiia/google-client-secret"
}

variable "aiia_oauth_state_secret_name" {
  description = "reply_url(✏️返信を作成) の HMAC 署名鍵（connect-web と同値にする）"
  type        = string
  default     = "teamagent/dev/aiia/oauth-state-secret"
}

variable "aiia_connect_base_url" {
  description = "reply_url のベースURL（connect-web の公開URL・IT正式エンドポイント確定後に差替）"
  type        = string
  default     = "https://aila.vseoanalytics.com"
}

variable "aiia_oauth_table" {
  description = "per-user Google token の DynamoDB テーブル名（AIIA_DDB_TABLE）"
  type        = string
  default     = "aiia-oauth-tokens"
}

variable "aiia_slack_token_table" {
  description = "per-user Slack xoxp の DynamoDB テーブル名（§Q-Q6 両連携ゲート用・digest 経路では未使用）"
  type        = string
  default     = "aiia-slack-tokens"
}

variable "aiia_reminder_table" {
  description = "未返信リマインダー状態の DynamoDB テーブル名"
  type        = string
  default     = "aiia-reminder-state"
}

variable "aiia_notified_table" {
  description = "通知 dedup（claim-before-send）の DynamoDB テーブル名"
  type        = string
  default     = "aiia-notified-events"
}

variable "aiia_default_internal_domain" {
  description = "INTERNAL 分類ヒントの社内ドメイン（.env.aila の AIIA_DEFAULT_INTERNAL_DOMAIN と同値・無いと分類品質が黙って劣化）"
  type        = string
  default     = "vectorinc.co.jp"
}

variable "aiia_kms_alias" {
  description = "per-user token 暗号化 KMS alias（既存・§V6で使用中のもの）"
  type        = string
  default     = "alias/teamagent-oauth-tokens"
}

variable "aiia_image_tag" {
  description = "CodeBuild が push するイメージタグ（IMMUTABLE repo のため build ごとに更新）"
  type        = string
  default     = "q2a"
}

# ---------- ECR（非ゲート: ビルドが先） ----------
resource "aws_ecr_repository" "aiia_mcp" {
  name                 = "${var.project_name}-aiia-mcp"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "aiia_mcp" {
  repository = aws_ecr_repository.aiia_mcp.name
  policy     = local.ecr_lifecycle_policy
}

# ---------- CodeBuild（非ゲート・専用ロール＝既存 codebuild ロール無変更の add-only） ----------
resource "aws_iam_role" "codebuild_aiia" {
  name               = "${var.project_name}-${var.environment}-codebuild-aiia"
  assume_role_policy = data.aws_iam_policy_document.codebuild_assume.json
}

data "aws_iam_policy_document" "codebuild_aiia" {
  statement {
    sid     = "Logs"
    actions = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/codebuild/${var.project_name}-${var.environment}-*",
    ]
  }
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # GetAuthorizationToken はリソース指定不可（AWS仕様）
  }
  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:DescribeImages", # buildspec post_build の digest 取得（無いと tee が空のまま SUCCEEDED）
    ]
    resources = [aws_ecr_repository.aiia_mcp.arn]
  }
  statement {
    sid       = "S3Source"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.raw_files.arn}/codebuild/aiia-source.zip"]
  }
}

resource "aws_iam_role_policy" "codebuild_aiia" {
  name   = "${var.project_name}-${var.environment}-codebuild-aiia"
  role   = aws_iam_role.codebuild_aiia.id
  policy = data.aws_iam_policy_document.codebuild_aiia.json
}

# source zip（AI-IA-UAE repo root から）:
#   cd ~/Documents/AI-IA-UAE && zip -r /tmp/aiia-source.zip pyproject.toml src config scripts infra/docker
#   aws s3 cp /tmp/aiia-source.zip s3://teamagent-dev-raw-files/codebuild/aiia-source.zip
# ⚠️ サプライチェーン注意(P2): raw_files へ PutObject を持つ worker/lambda ロールが zip を差し替え可能
#   （teamagent source.zip と同型の既存姿勢）。緩和=service は ECR URL@digest でピン留め＋build直後に
#   digest 照合。恒久対処（codebuild/ prefix の書込分離 or sha256 照合）は §T4 硬化で。
resource "aws_codebuild_project" "aiia_image" {
  name         = "${var.project_name}-${var.environment}-aiia-image-builder"
  description  = "Build aiia-mcp image inside AWS (proxy-free) and push to ECR"
  service_role = aws_iam_role.codebuild_aiia.arn

  artifacts { type = "NO_ARTIFACTS" }

  environment {
    compute_type    = "BUILD_GENERAL1_SMALL" # pure-python wheel のみ＝軽量で足る
    image           = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
    type            = "ARM_CONTAINER"
    privileged_mode = true # docker ビルドに必須

    environment_variable {
      name  = "ECR_REGISTRY"
      value = local.cb_registry
    }
    environment_variable {
      name  = "AIIA_REPO"
      value = aws_ecr_repository.aiia_mcp.repository_url
    }
    environment_variable {
      name  = "IMAGE_TAG"
      value = var.aiia_image_tag
    }
  }

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.raw_files.id}/codebuild/aiia-source.zip"
    buildspec = <<-EOT
      version: 0.2
      phases:
        pre_build:
          commands:
            - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $ECR_REGISTRY
        build:
          commands:
            - echo "Building aiia-mcp ($IMAGE_TAG) on $(uname -m)"
            - docker build -f infra/docker/Dockerfile.aiia-mcp -t $AIIA_REPO:$IMAGE_TAG .
        post_build:
          commands:
            - docker push $AIIA_REPO:$IMAGE_TAG
            - aws ecr describe-images --repository-name ${var.project_name}-aiia-mcp --image-ids imageTag=$IMAGE_TAG --query 'imageDetails[0].imageDigest' --output text | tee /tmp/digest.txt
            - echo "AIIA_DIGEST=$(cat /tmp/digest.txt)"
    EOT
  }

  logs_config {
    cloudwatch_logs {
      group_name = "/aws/codebuild/${var.project_name}-${var.environment}-aiia-image-builder"
    }
  }
}

# ---------- CloudWatch Logs（非ゲート・無害） ----------
resource "aws_cloudwatch_log_group" "aiia_mcp" {
  name              = "/${var.project_name}/${var.environment}/aiia-mcp"
  retention_in_days = 30
}

# ---------- 以降は enable_aiia_mcp ゲート（secret 4本の事前作成が前提） ----------
data "aws_secretsmanager_secret" "aiia_bearer" {
  count = var.enable_aiia_mcp ? 1 : 0
  name  = var.aiia_mcp_bearer_secret_name
}
data "aws_secretsmanager_secret" "aiia_google_client_id" {
  count = var.enable_aiia_mcp ? 1 : 0
  name  = var.aiia_google_client_id_secret_name
}
data "aws_secretsmanager_secret" "aiia_google_client_secret" {
  count = var.enable_aiia_mcp ? 1 : 0
  name  = var.aiia_google_client_secret_secret_name
}
data "aws_secretsmanager_secret" "aiia_oauth_state" {
  count = var.enable_aiia_mcp ? 1 : 0
  name  = var.aiia_oauth_state_secret_name
}

# KMS alias は §V6 で作成済（worker の aiia-runtime が使用中）。key ARN を IAM/env に使う。
data "aws_kms_alias" "aiia_oauth" {
  count = var.enable_aiia_mcp ? 1 : 0
  name  = var.aiia_kms_alias
}

# --- 実行ロール（launch時 secrets 注入・aiia の secret ＋ slack-bot-token のみ＝§J分割方針踏襲） ---
resource "aws_iam_role" "ecs_execution_aiia" {
  count              = var.enable_aiia_mcp ? 1 : 0
  name               = "${var.project_name}-${var.environment}-ecs-exec-aiia"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_aiia_managed" {
  count      = var.enable_aiia_mcp ? 1 : 0
  role       = aws_iam_role.ecs_execution_aiia[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_aiia_secrets" {
  count = var.enable_aiia_mcp ? 1 : 0
  statement {
    sid     = "ReadAiiaSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      data.aws_secretsmanager_secret.aiia_bearer[0].arn,
      data.aws_secretsmanager_secret.aiia_google_client_id[0].arn,
      data.aws_secretsmanager_secret.aiia_google_client_secret[0].arn,
      data.aws_secretsmanager_secret.aiia_oauth_state[0].arn,
      data.aws_secretsmanager_secret.slack_bot.arn,
    ]
  }
}

resource "aws_iam_role_policy" "ecs_execution_aiia_secrets" {
  count  = var.enable_aiia_mcp ? 1 : 0
  name   = "${var.project_name}-${var.environment}-ecs-exec-aiia-secrets"
  role   = aws_iam_role.ecs_execution_aiia[0].id
  policy = data.aws_iam_policy_document.ecs_execution_aiia_secrets[0].json
}

# --- タスクロール: DynamoDB(aiia 4表・表ごと最小権限) / KMS Decrypt / Bedrock のみ ---
# Secrets Manager の runtime read は不要（実行ロールが launch 時に注入）＝teamagent-mcp より狭い。
# KMS は Decrypt のみ・token 2表は読取のみ（保存=Encrypt/Put は connect-web 側の責務・ここには無い）。
# DeleteItem はコード上の利用が無いため全表で不許可（連携の強制解除＝可用性攻撃面を塞ぐ）。
data "aws_iam_policy_document" "aiia_mcp_task" {
  count = var.enable_aiia_mcp ? 1 : 0
  statement {
    sid     = "AiiaGoogleTokenRead"
    actions = ["dynamodb:GetItem", "dynamodb:Scan"] # Scan=§Q-Q6 朝配信のユーザー列挙(list_emails)
    resources = [
      "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/${var.aiia_oauth_table}",
    ]
  }
  statement {
    sid     = "AiiaSlackTokenRead"
    actions = ["dynamodb:GetItem", "dynamodb:Scan"] # §Q-Q6 両連携ゲート判定用（digest 経路では未使用）
    resources = [
      "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/${var.aiia_slack_token_table}",
    ]
  }
  statement {
    sid = "AiiaStateTables"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]
    resources = [
      "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/${var.aiia_reminder_table}",
      "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/${var.aiia_notified_table}",
    ]
  }
  statement {
    sid       = "KmsDecryptOauthTokens"
    actions   = ["kms:Decrypt"]
    resources = [data.aws_kms_alias.aiia_oauth[0].target_key_arn]
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

resource "aws_iam_role" "aiia_mcp_task" {
  count              = var.enable_aiia_mcp ? 1 : 0
  name               = "${var.project_name}-${var.environment}-aiia-mcp-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "aiia_mcp_task" {
  count  = var.enable_aiia_mcp ? 1 : 0
  name   = "${var.project_name}-${var.environment}-aiia-mcp-task"
  role   = aws_iam_role.aiia_mcp_task[0].id
  policy = data.aws_iam_policy_document.aiia_mcp_task[0].json
}

# --- SG: 8788 は OpenClaw SG からのみ（外部 inbound なし） ---
resource "aws_security_group" "aiia_mcp" {
  count       = var.enable_aiia_mcp ? 1 : 0
  name        = "${var.project_name}-${var.environment}-aiia-mcp-sg"
  description = "aiia-mcp backend (ingress 8788 from OpenClaw only)"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "MCP streamable-http from OpenClaw only"
    from_port       = 8788
    to_port         = 8788
    protocol        = "tcp"
    security_groups = [aws_security_group.openclaw.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- Cloud Map: aiia-mcp.teamagent.internal（OpenClaw config の url と一致させる） ---
resource "aws_service_discovery_service" "aiia_mcp" {
  count = var.enable_aiia_mcp ? 1 : 0
  name  = "aiia-mcp"

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

# --- Task Definition ---
resource "aws_ecs_task_definition" "aiia_mcp" {
  count                    = var.enable_aiia_mcp && var.aiia_mcp_image != "" ? 1 : 0
  family                   = "${var.project_name}-${var.environment}-aiia-mcp"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.fargate_aiia_cpu
  memory                   = var.fargate_aiia_memory
  execution_role_arn       = aws_iam_role.ecs_execution_aiia[0].arn
  task_role_arn            = aws_iam_role.aiia_mcp_task[0].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "aiia-mcp"
      image     = var.aiia_mcp_image
      essential = true
      portMappings = [
        { containerPort = 8788, protocol = "tcp" }
      ]
      # AIIA_MCP_HOST/PORT/PATH・AIIA_PROFILE=bedrock・AIIA_STRICT_PROVIDER=1 はイメージ env 既定。
      environment = [
        { name = "AIIA_DDB_TABLE", value = var.aiia_oauth_table },
        { name = "OAUTH_KMS_KEY_ID", value = data.aws_kms_alias.aiia_oauth[0].target_key_arn },
        { name = "CONNECT_BASE_URL", value = var.aiia_connect_base_url },
        { name = "AIIA_DEFAULT_INTERNAL_DOMAIN", value = var.aiia_default_internal_domain },
        { name = "AWS_REGION", value = var.aws_region },
      ]
      secrets = [
        { name = "AIIA_MCP_BEARER", valueFrom = data.aws_secretsmanager_secret.aiia_bearer[0].arn },
        { name = "SLACK_BOT_TOKEN", valueFrom = data.aws_secretsmanager_secret.slack_bot.arn },
        { name = "GOOGLE_CLIENT_ID", valueFrom = data.aws_secretsmanager_secret.aiia_google_client_id[0].arn },
        { name = "GOOGLE_CLIENT_SECRET", valueFrom = data.aws_secretsmanager_secret.aiia_google_client_secret[0].arn },
        { name = "OAUTH_STATE_SECRET", valueFrom = data.aws_secretsmanager_secret.aiia_oauth_state[0].arn },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.aiia_mcp.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "aiia-mcp"
        }
      }
      healthCheck = {
        command     = ["CMD-SHELL", "curl -fsS http://127.0.0.1:8788/healthz || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 5
        startPeriod = 30
      }
    }
  ])
}

# --- Service ---
resource "aws_ecs_service" "aiia_mcp" {
  count           = var.enable_aiia_mcp && var.aiia_mcp_image != "" ? 1 : 0
  name            = "${var.project_name}-${var.environment}-aiia-mcp"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.aiia_mcp[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.aiia_mcp[0].id]
    assign_public_ip = true
  }

  service_registries {
    registry_arn = aws_service_discovery_service.aiia_mcp[0].arn
  }
}

# ---------- Outputs ----------
output "aiia_mcp_ecr_url" {
  value       = aws_ecr_repository.aiia_mcp.repository_url
  description = "aiia-mcp の ECR repository URL（CodeBuild が push）"
}

output "aiia_codebuild_project" {
  value       = aws_codebuild_project.aiia_image.name
  description = "aiia-mcp イメージの CodeBuild プロジェクト名"
}

output "aiia_mcp_service_dns" {
  value       = "aiia-mcp.${aws_service_discovery_private_dns_namespace.internal.name}:8788"
  description = "OpenClaw config の mcp.servers.aiia url に使う内部DNS（enable後に有効）"
}
