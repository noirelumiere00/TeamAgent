# ============================================================
# §U-Phase2: ingest を ECS Scheduled Task に移行（EC2 worker 廃止 Phase 2）
# ============================================================
# 役割: 社内ナレッジ（Slack / Google Drive / Sheets）を週次で pgvector に取り込む処理を
#   EC2 systemd timer (`teamagent-ingest.timer`、毎週月 18:00 UTC = 火 03:00 JST) から
#   EventBridge Scheduled Task → ECS RunTask（Fargate）に移行する（§U・2026-06-18 着手）。
#
# 選定理由（Plan の Phase 2 評価表に従う）:
#   - Lambda+VPC ENI: 15 分制限・ENI cold start で重バッチに不向き
#   - ECS Scheduled Task（本実装）: 15 分制限なし・既存 Fargate IAM 流用・OpenClaw cluster 同居
#   - AWS Batch: 設定複雑・週次 1 回には過剰
#
# image: 既存 teamagent-mcp の ECR image をそのまま流用（teamagent パッケージ同一）。
#   ENTRYPOINT は scripts/run_ingest_fargate.py（GOOGLE_OAUTH_JSON 展開 + ingest_sources.py 呼び出し）。

# ---------- 変数 ----------
variable "enable_ingest_schedule" {
  description = "ingest の ECS Scheduled Task（taskdef/EventBridge rule/target/IAM）を有効化"
  type        = bool
  default     = false
}

variable "fargate_ingest_cpu" {
  description = "ingest タスク CPU（embedding + Bedrock API call + RDS bulk write）"
  type        = number
  # ⚠️ 実運用値はここではなく CLI（register-task-definition）で上書きする。
  # infra/docker/runtime-consumers.json の memory 契約とスモークテストが
  # この既定値に固定されているため、ここを動かすと契約テストが赤くなる。
  # 2026-08-06 の高速化では live を cpu=4096/memory=8192 へ CLI で引き上げた。
  default     = 1024
}

variable "fargate_ingest_memory" {
  description = "ingest タスク メモリ MB"
  type        = number
  default     = 4096
}

variable "ingest_owner_email" {
  description = "ingest が走るときの owner_email（Drive/Sheets の per-user OAuth subject）"
  type        = string
  default     = "shogo@vectorinc.co.jp"
}

variable "ingest_sources" {
  # shared_drives は data/ingest_sources.yaml の shared_drives_crawl(enabled=true) を
  # 発火させる唯一のキー。ここに無いと pipeline の `if "shared_drives" in kinds:` に
  # 入らず、共有ドライブ配下は yaml が有効でも永久に巡回されない（実測: 共有ドライブ内の
  # 案件フォルダが金庫に存在しなかった原因）。
  description = "取り込むソース（カンマ区切り・slack,gdrive,gsheets,shared_drives）"
  type        = string
  default     = "slack,gdrive,gsheets,shared_drives"
}

variable "ingest_schedule_expression" {
  description = "EventBridge cron 式（既定: 平日 09:00 UTC = 平日 18:00 JST。EventBridge cron は常に UTC）"
  type        = string
  default     = "cron(0 9 ? * MON-FRI *)"
}

variable "ingest_max_runtime_hours" {
  description = "先行 ingest タスクを異常滞留として停止・再起動するまでの時間"
  type        = number
  default     = 20

  validation {
    condition     = var.ingest_max_runtime_hours > 0
    error_message = "ingest_max_runtime_hours は 0 より大きい値にしてください。"
  }
}

variable "ingest_google_oauth_secret_name" {
  description = "GOOGLE_OAUTH_JSON の Secrets Manager 名（client_id/client_secret/refresh_token の JSON 形式）"
  type        = string
  default     = "teamagent/dev/google_oauth"
}

# ---------- CloudWatch Logs ----------
resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/${var.project_name}/${var.environment}/ingest"
  retention_in_days = 30
}

# ---------- 以降は enable_ingest_schedule ゲート ----------

# ingest が必要とする SM secrets（既存 data sources を流用しつつ、google_oauth のみ追加）
data "aws_secretsmanager_secret" "google_oauth" {
  count = var.enable_ingest_schedule ? 1 : 0
  name  = var.ingest_google_oauth_secret_name
}

# --- 実行ロール（launch 時 secrets 注入用） ---
resource "aws_iam_role" "ecs_execution_ingest" {
  count              = var.enable_ingest_schedule ? 1 : 0
  name               = "${var.project_name}-${var.environment}-ecs-exec-ingest"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_ingest_managed" {
  count      = var.enable_ingest_schedule ? 1 : 0
  role       = aws_iam_role.ecs_execution_ingest[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_ingest_secrets" {
  count = var.enable_ingest_schedule ? 1 : 0
  statement {
    sid     = "ReadIngestSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = concat([
      data.aws_secretsmanager_secret.database_url.arn,
      data.aws_secretsmanager_secret.slack_bot.arn,
      data.aws_secretsmanager_secret.google_oauth[0].arn,
    ], var.enable_scrape_tools ? [data.aws_secretsmanager_secret.vertex_sa[0].arn] : [])
  }
}

resource "aws_iam_role_policy" "ecs_execution_ingest_secrets" {
  count  = var.enable_ingest_schedule ? 1 : 0
  name   = "${var.project_name}-${var.environment}-ecs-exec-ingest-secrets"
  role   = aws_iam_role.ecs_execution_ingest[0].id
  policy = data.aws_iam_policy_document.ecs_execution_ingest_secrets[0].json
}

# --- タスクロール: RDS connect + Bedrock(embedding) + KMS Decrypt ---
data "aws_iam_policy_document" "ingest_task" {
  count = var.enable_ingest_schedule ? 1 : 0
  statement {
    sid       = "KmsDecryptForOauthTokens"
    actions   = ["kms:Decrypt"]
    resources = [data.aws_kms_alias.oauth_tokens.target_key_arn]
  }
  statement {
    sid = "BedrockInvokeForEmbedding"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = local.bedrock_resources
  }
}

resource "aws_iam_role" "ingest_task" {
  count              = var.enable_ingest_schedule ? 1 : 0
  name               = "${var.project_name}-${var.environment}-ingest-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "ingest_task" {
  count  = var.enable_ingest_schedule ? 1 : 0
  name   = "${var.project_name}-${var.environment}-ingest-task"
  role   = aws_iam_role.ingest_task[0].id
  policy = data.aws_iam_policy_document.ingest_task[0].json
}

# --- SG: ingress 無し（外部から到達不要・egress のみ） ---
resource "aws_security_group" "ingest" {
  count       = var.enable_ingest_schedule ? 1 : 0
  name        = "${var.project_name}-${var.environment}-ingest-sg"
  description = "ingest Scheduled Task (egress only: RDS/Secrets/Bedrock/Slack/Google API)"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project_name}-${var.environment}-ingest-sg" }
}

# RDS への 5432 を ingest SG から許可（既存 db_from_mcp と同型・純加算）
resource "aws_security_group_rule" "db_from_ingest" {
  count                    = var.enable_ingest_schedule ? 1 : 0
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.ingest[0].id
  security_group_id        = aws_security_group.db.id
  description              = "PostgreSQL from ingest Scheduled Task"
}

# --- Task Definition ---
# image は既存 teamagent-mcp（teamagent パッケージ同一・command で run_ingest_fargate.py を起動）。
# GOOGLE_OAUTH_JSON は JSON 形式で SM から注入し、scripts/run_ingest_fargate.py が parse して 3 env に展開。
resource "aws_ecs_task_definition" "ingest" {
  count                    = var.enable_ingest_schedule && var.mcp_image != "" ? 1 : 0
  family                   = "${var.project_name}-${var.environment}-ingest"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.fargate_ingest_cpu
  memory                   = var.fargate_ingest_memory
  execution_role_arn       = aws_iam_role.ecs_execution_ingest[0].arn
  task_role_arn            = aws_iam_role.ingest_task[0].arn
  skip_destroy             = true

  depends_on = [
    terraform_data.runtime_guard,
    terraform_data.production_image_release_gate,
  ]

  volume {
    name = "runtime-tmp"
  }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([merge(local.teamagent_runtime_container, {
    name      = "ingest"
    image     = var.mcp_image
    essential = true
    command   = [local.teamagent_python, "/app/scripts/run_ingest_fargate.py"]
    environment = concat([
      { name = "AWS_REGION", value = var.aws_region },
      { name = "HOME", value = "/tmp/home" },
      { name = "TMPDIR", value = "/tmp" },
      { name = "XDG_CACHE_HOME", value = "/tmp/.cache" },
      { name = "PYTHONPYCACHEPREFIX", value = "/tmp/.pycache" },
      { name = "INGEST_SOURCES", value = var.ingest_sources },
      { name = "INGEST_OWNER_EMAIL", value = var.ingest_owner_email },
      # §G 会社共有: これが無いと pipeline._company_acl_groups() が [] を返し、取込 document の
      # acl_groups が空＝owner しか RLS で見られない（管理画面では見えるが他社員の通常検索で不可視）。
      # MCP task-def（fargate.tf）と同変数を ingest task にも渡し、会社メンバーへ横共有する（Codex #215-3）。
      { name = "TEAMAGENT_SHARED_COMPANY_DOMAINS", value = var.shared_company_domains },
      { name = "STRUCTLOG_FORMAT", value = "json" },
      # §知識ベース（2026-06-22）: Drive 取り込み時に Bedrock で資料を自動分類
      # （案件/業界/種別/フェーズ→documents.metadata）。ingest_task は bedrock:InvokeModel 保持済。
      { name = "USE_DOC_CLASSIFY", value = "true" },
      # v0.3.1 名寄せ: 分類時に取引先/代理店/ブランド/コラボ名を Haiku 抽出し cls_entities に保持
      # （親クライアント検索で子コラボが出る・既定 false）。既存分は backfill_entities.py で別途。
      { name = "USE_ENTITY_TAGS", value = var.use_entity_tags ? "true" : "false" },
      # コスト方針(2026-06-29)=Haiku。未設定だとコード既定に落ちる（2026-07-13 実測: 週次分類が
      # Sonnet で 573回/週 走って月次課金の主因だった。CloudTrail で確定）。mcp と同一変数で管理。
      { name = "BEDROCK_MODEL_ID", value = var.mcp_model_id },
      # §コンテンツ拡充（2026-06-24）: 取り込みのたびに走る恒久処理（週次 ingest にも適用）。
      # ① rich-extract: Googleネイティブ(gdoc/gslide/gsheet)本文化＋pptxノート/表/group＋xlsx数式＋
      #    text/csv＋最小文字数ガード。② boilerplate: テンプレ(使い回し)箇所をコーパス統計で検出し
      #    chunks.metadata.boilerplate に印（検索/グラフが共通項扱いを除外）。③ doc-dedup: 資料まるごと
      #    near-dup(PDF≒PPTX)を文字n-gram Jaccardで検出し非正本に documents.metadata.suppressed の印。
      # 全て冪等・再取込のたび再評価＝今後追加される資料にも自動適用。既定OFFの機能をここでONにする。
      { name = "INGEST_RICH_EXTRACT", value = "true" },
      { name = "BOILERPLATE_DETECT", value = "true" },
      { name = "DOC_DEDUP_DETECT", value = "true" },
      # §知識ベース: 共有ドライブの走査/DL は「個人OAuth」を使う。これが無いと Vertex SA が
      # 選ばれ、SA は外部 Drive 非対応で walk が 0 件になる（OAuth3点は GOOGLE_OAUTH_JSON から展開済）。
      { name = "GOOGLE_FORCE_OAUTH", value = "1" },
      # §取り込み時間の短縮（2026-08-06）。未変更ファイルの download/extract/embed/
      # DB書き込みを丸ごと飛ばす。初回は cursor が無いのでフル走査になり、その走査
      # 開始前の token が次回基点になる。
      # ⚠️ OMP/MKL_NUM_THREADS は live の cpu に追随させる必要があるため、
      #    ここではなく CLI 側の task definition で設定する（terraform の既定 cpu と
      #    live が異なるため、ここで導出すると誤った値が焼かれる）。
      { name = "USE_INCREMENTAL_SYNC", value = "1" },
      ], var.enable_scrape_tools ? [
      { name = "VERTEX_SA_PATH", value = "/tmp/vertex-sa.json" },
      { name = "GEMINI_USE_VERTEX", value = "true" },
      { name = "GEMINI_VERTEX_PROJECT", value = var.gemini_vertex_project },
      { name = "GEMINI_VERTEX_LOCATION", value = var.gemini_vertex_location },
    ] : [])
    secrets = concat([
      { name = "DATABASE_URL", valueFrom = data.aws_secretsmanager_secret.database_url.arn },
      { name = "SLACK_BOT_TOKEN", valueFrom = data.aws_secretsmanager_secret.slack_bot.arn },
      { name = "GOOGLE_OAUTH_JSON", valueFrom = data.aws_secretsmanager_secret.google_oauth[0].arn },
      ], var.enable_scrape_tools ? [
      { name = "VERTEX_SA_JSON", valueFrom = data.aws_secretsmanager_secret.vertex_sa[0].arn },
    ] : [])
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.ingest.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "ingest"
      }
    }
    # Scheduled Task なので long-running ではない・healthCheck 不要（exit code が成否を語る）
  })])

  lifecycle {
    create_before_destroy = true
  }
}

# --- EventBridge → Lambda dispatcher → ECS RunTask ---
# ECS 直起動では前回タスクを確認できないため、Lambda が RUNNING の有無と上限時間を判定する。
data "archive_file" "ingest_dispatch" {
  count            = var.enable_ingest_schedule && var.mcp_image != "" ? 1 : 0
  type             = "zip"
  source_file      = "${path.module}/lambda/ingest_dispatch/handler.py"
  output_path      = "${path.module}/build/ingest_dispatch.zip"
  output_file_mode = "0644"
}

resource "aws_cloudwatch_log_group" "ingest_dispatch" {
  count             = var.enable_ingest_schedule && var.mcp_image != "" ? 1 : 0
  name              = "/aws/lambda/${var.project_name}-${var.environment}-ingest-dispatch"
  retention_in_days = 30

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_iam_role" "ingest_dispatch" {
  count              = var.enable_ingest_schedule && var.mcp_image != "" ? 1 : 0
  name               = "${var.project_name}-${var.environment}-ingest-dispatch"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "ingest_dispatch" {
  count = var.enable_ingest_schedule && var.mcp_image != "" ? 1 : 0

  statement {
    sid       = "ListIngestTasks"
    actions   = ["ecs:ListTasks"]
    resources = ["*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }
  statement {
    sid       = "DescribeAndStopIngestTasks"
    actions   = ["ecs:DescribeTasks", "ecs:StopTask"]
    resources = ["arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${aws_ecs_cluster.main.name}/*"]
  }
  statement {
    sid       = "RunIngestTask"
    actions   = ["ecs:RunTask"]
    resources = [replace(aws_ecs_task_definition.ingest[0].arn, "/:[0-9]+$/", ":*")]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }
  statement {
    sid     = "PassExecutionAndTaskRoles"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.ecs_execution_ingest[0].arn,
      aws_iam_role.ingest_task[0].arn,
    ]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.ingest_dispatch[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "ingest_dispatch" {
  count  = var.enable_ingest_schedule && var.mcp_image != "" ? 1 : 0
  name   = "${var.project_name}-${var.environment}-ingest-dispatch"
  role   = aws_iam_role.ingest_dispatch[0].id
  policy = data.aws_iam_policy_document.ingest_dispatch[0].json
}

resource "aws_lambda_function" "ingest_dispatch" {
  count            = var.enable_ingest_schedule && var.mcp_image != "" ? 1 : 0
  function_name    = "${var.project_name}-${var.environment}-ingest-dispatch"
  role             = aws_iam_role.ingest_dispatch[0].arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.ingest_dispatch[0].output_path
  source_code_hash = data.archive_file.ingest_dispatch[0].output_base64sha256
  timeout          = 30

  # EventBridge の重複配送が並行しても ListTasks 判定を直列化する。
  reserved_concurrent_executions = 1

  depends_on = [
    aws_cloudwatch_log_group.ingest_dispatch,
    aws_iam_role_policy.ingest_dispatch,
    terraform_data.runtime_guard,
  ]

  environment {
    variables = {
      CLUSTER_ARN              = aws_ecs_cluster.main.arn
      TASKDEF_ARN              = aws_ecs_task_definition.ingest[0].arn
      TASK_FAMILY              = aws_ecs_task_definition.ingest[0].family
      SUBNETS                  = join(",", data.aws_subnets.default.ids)
      SG_ID                    = aws_security_group.ingest[0].id
      INGEST_MAX_RUNTIME_HOURS = tostring(var.ingest_max_runtime_hours)
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# --- EventBridge rule: 毎週月 18:00 UTC = 火 03:00 JST（EC2 systemd timer と同タイミング） ---
variable "ingest_rule_enabled" {
  description = "週次 ingest の EventBridge ルールを ENABLED にするか。live は手動 DISABLED 運用のため既定 false（state 未指定だと apply のたびに手動 DISABLE が ENABLED に巻き戻る・2026-07-11 監査）。"
  type        = bool
  default     = false
}

resource "aws_cloudwatch_event_rule" "ingest_weekly" {
  count               = var.enable_ingest_schedule ? 1 : 0
  name                = "${var.project_name}-${var.environment}-ingest-weekly"
  description         = "週次 ingest（Slack/Drive/Sheets → pgvector）の Fargate 起動トリガ"
  schedule_expression = var.ingest_schedule_expression
  state               = var.ingest_rule_enabled ? "ENABLED" : "DISABLED"

  depends_on = [terraform_data.runtime_guard]

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudwatch_event_target" "ingest_run_task" {
  count = var.enable_ingest_schedule && var.mcp_image != "" ? 1 : 0
  rule  = aws_cloudwatch_event_rule.ingest_weekly[0].name
  arn   = aws_lambda_function.ingest_dispatch[0].arn

  depends_on = [
    aws_lambda_permission.ingest_weekly,
    terraform_data.runtime_guard,
  ]

  # 失敗時の retry（max 1 回・遅延 5 分）
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 1
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_lambda_permission" "ingest_weekly" {
  count         = var.enable_ingest_schedule && var.mcp_image != "" ? 1 : 0
  statement_id  = "AllowEventBridgeIngestWeekly"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest_dispatch[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ingest_weekly[0].arn
}

# ---------- Outputs ----------
output "ingest_task_definition_arn" {
  description = "ingest Scheduled Task の TaskDefinition ARN（手動 run-task 検証用）"
  value       = var.enable_ingest_schedule && var.mcp_image != "" ? aws_ecs_task_definition.ingest[0].arn : ""
}

output "ingest_log_group" {
  description = "CloudWatch Logs グループ"
  value       = aws_cloudwatch_log_group.ingest.name
}

output "ingest_event_rule" {
  description = "EventBridge rule 名（Test Event で起動検証）"
  value       = var.enable_ingest_schedule ? aws_cloudwatch_event_rule.ingest_weekly[0].name : ""
}
