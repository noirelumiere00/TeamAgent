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
  account_id       = data.aws_caller_identity.current.account_id
  teamagent_python = "/app/.venv/bin/python"
  teamagent_runtime_container = {
    user                   = "10001:10001"
    readonlyRootFilesystem = true
    privileged             = false
    linuxParameters = {
      initProcessEnabled = true
      capabilities = {
        drop = ["ALL"]
      }
    }
    mountPoints = [{
      sourceVolume  = "runtime-tmp"
      containerPath = "/tmp"
      readOnly      = false
    }]
  }

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
# Python ラッパが SA JSON を task-scoped /tmp にファイル化して ADC に渡す。
data "aws_secretsmanager_secret" "vertex_sa" {
  count = var.enable_scrape_tools ? 1 : 0
  name  = var.vertex_sa_secret_name
}

# ---------- クラスタ ----------
resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-${var.environment}"

  setting {
    name = "containerInsights"
    # コスト最適化(2026-06-29): Container Insights を無効化（テレメトリのみ・アプリログは別途維持。約-$11/月）。
    # 既にCLIで無効化済み。詳細監視を戻す場合は "enabled"。
    value = "disabled"
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
      # §U ハイブリッド identity: mcp task が SLACK_BOT_TOKEN を注入できるよう取得権限を付与。
      data.aws_secretsmanager_secret.slack_bot.arn,
      # §U: 本人 token リフレッシュ用 Web クライアント client_secret の注入権限。
      data.aws_secretsmanager_secret.connect_google_client_secret[0].arn,
      # §U: oauth_connect の state 署名鍵の注入権限。
      data.aws_secretsmanager_secret.connect_oauth_state[0].arn,
      # §Slack個人連携(xoxp): DM 認可URL生成用（client_id / state 署名鍵）の注入権限。
      data.aws_secretsmanager_secret.connect_slack_client_id[0].arn,
      data.aws_secretsmanager_secret.slack_oauth_state_secret[0].arn,
      # §知識ベース: knowledge_deliver が共有 Drive(個人OAuth)で実ファイルを DL する token。
      data.aws_secretsmanager_secret.google_oauth[0].arn,
      ], var.enable_scrape_tools ? [data.aws_secretsmanager_secret.vertex_sa[0].arn] : [],
      # カタログ①〜⑤: Apify トークン注入権限（tiktok/apify-token 共用）。
      (var.enable_x_research && var.tiktok_apify_secret_arn != "") ? [var.tiktok_apify_secret_arn] : []
    )
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
      # DynamoDB 全操作を明示 Deny（aiia-mcp 退役後は不要だが、将来の誤付与への保険として残す）。
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
  # v0.3 Task10: 分析結果キャッシュ（analysis-cache/ prefix 限定・presigned 不要＝Get/Put のみ）。
  dynamic "statement" {
    for_each = var.use_analysis_cache ? [1] : []
    content {
      sid     = "AnalysisCacheS3"
      actions = ["s3:PutObject", "s3:GetObject"]
      resources = [
        "${aws_s3_bucket.raw_files.arn}/analysis-cache/*",
      ]
    }
  }
  # v0.3 Task8: 長文ペイロード退避（payload-offload/ prefix 限定・presigned 用）。
  # ⚠️ この文が無いと USE_PAYLOAD_OFFLOAD=1 でも AccessDenied→fail-open で黙って無効
  # （「フラグは中身を保証しない」型の地雷・レビュー F1）。
  dynamic "statement" {
    for_each = var.use_payload_offload ? [1] : []
    content {
      sid     = "PayloadOffloadS3"
      actions = ["s3:PutObject", "s3:GetObject", "s3:GetBucketLocation"]
      resources = [
        aws_s3_bucket.raw_files.arn,
        "${aws_s3_bucket.raw_files.arn}/payload-offload/*",
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
  # §知識ベース: Cohere Rerank（検索の真の関連度で並べ替え＝業界/客の違いを区別）。
  # bedrock:Rerank は InvokeModel とは別アクションなので明示付与が必要。
  statement {
    sid       = "BedrockRerank"
    actions   = ["bedrock:Rerank"]
    resources = ["arn:aws:bedrock:${var.aws_region}::foundation-model/cohere.rerank-v3-5:0"]
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
  depends_on         = [terraform_data.production_image_release_gate]

  volume {
    name = "runtime-tmp"
  }

  lifecycle {
    precondition {
      condition     = !var.enable_scrape_tools || local.media_enabled == 1
      error_message = "enable_scrape_tools requires the generic media worker; hardened core contains no browser/ffmpeg/yt-dlp fallback."
    }
  }

  container_definitions = jsonencode([merge(local.teamagent_runtime_container, {
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
      # §5-C4: 他ワークスペースユーザーの fail-closed 拒否（空=検証skip・WARN。多人数運用では設定必須）。
      { name = "SLACK_TEAM_ID", value = var.slack_team_id },
      # v0.3 Task6: AiLaVault リンク注入の発火条件（未設定だと build_search_web_links が
      # 空 dict を返し web_url/app_url が一切載らない）。2026-07-10 の実機確認で live に
      # 無いことを確認済み＝この行が入って初めて Slack に検索 UI リンクが出る。
      { name = "CONNECT_BASE_URL", value = var.connect_base_url },
      # レポート短縮リンク(/r)の段階ゲート。connect-web に /r+vseo-s3-read が揃うまで false=従来 presigned。
      { name = "USE_REPORT_SHORTURL", value = var.enable_report_shorturl ? "1" : "0" },
      # §U: 5名運用の pgvector pool ウォームアップ。起動時に2接続確立し初回検索のレイテンシを下げる
      # （max=8 で5並行に余裕＝枯渇なし）。
      { name = "PGVECTOR_POOL_MIN", value = "2" },
      # §U: per-user OAuth token の復号鍵。これが無いと factory._build_token_store() が
      # InMemoryTokenStore（空＝全員未連携）にフォールバックし、mail_* が「連携してください」で
      # 落ちる（RDS に token があっても見えない）。connect-web/morning-digest と同じ alias を共用。
      # mcp_task role は kms:Decrypt on key/* を保持済（KmsDecrypt statement）。
      { name = "OAUTH_KMS_KEY_ID", value = "alias/teamagent-oauth-tokens" },
      # §U: 本人 refresh token から Gmail クライアントを構築する OAuth Web 型クライアント（pgd1mj4）。
      # これが無いと build_user_credentials(google_auth.py) が ValueError → mail_summary が
      # PermissionError「認証情報を解決できません/再認可」で落ちる（token 取得・KMS 復号は成功してても）。
      # connect-web と同じ client（token を mint した本体）でないと refresh できない。secret は下の secrets。
      { name = "CONNECT_GOOGLE_CLIENT_ID", value = var.connect_google_client_id },
      # §U: oauth_connect skill が個別 OAuth URL を生成するための設定。
      # OAUTH_REDIRECT_URI は connect-web と同じ公開 callback URL。OAUTH_STATE_SECRET（下の secrets）
      # は connect-web と同一値必須＝mcp が make_state で署名→connect-web callback が verify_state で検証。
      { name = "OAUTH_REDIRECT_URI", value = var.connect_redirect_uri },
      # §Slack個人連携(xoxp): DM『連携』が Slack 認可URLを生成するのに必要（未設定なら Slack リンク非表示）。
      { name = "SLACK_OAUTH_REDIRECT_URI", value = var.slack_oauth_redirect_uri },
      { name = "USE_OAUTH_CONNECT_TOOL", value = "true" },
      # 構造化ログを JSON Lines 化（CloudWatch metric filter `{ $.cost_usd = * }` 等が
      # バインドして cost/error/spoof アラームが発火する。observability/logging_config.py）。
      { name = "STRUCTLOG_FORMAT", value = "json" },
      # §U-PartA-Step A: メール機能（readonly 3 つ）を OpenClaw mention 経由で公開（@AiLa メール要約等）。
      # factory.py の _envflag() ベースで ToolSpec 登録される（既に dev branch に実装済・PR #119）。
      # per-user OAuth は RdsTokenStore（昨日復旧した connect.newstv.co.jp 経由 token）を使う。
      { name = "USE_MAIL_SUMMARY_TOOL", value = "true" },
      { name = "USE_FOLLOWUP_TOOL", value = "true" },
      { name = "USE_MAIL_LINK_TOOL", value = "true" },
      # §U-Part3-Step C: 返信下書き生成（gmail.modify drafts.create のみ・送信は人間）。
      # G4' で send/delete は denylist 物理封鎖（mail_reply/skill.py）。
      { name = "USE_MAIL_REPLY_TOOL", value = "true" },
      # 朝ダイジェスト Skill。EventBridge Scheduled Task（平日 9:30 JST）が
      # scripts/run_morning_digest_fargate.py 経由で呼ぶ。mention 経由でも露出（ローカル検証用）。
      { name = "USE_MORNING_DIGEST_TOOL", value = "true" },
      # 朝ダイジェストの「✏️下書きを作成」ボタン押下処理ツール（OpenClaw が interaction を system
      # event で渡し、SOUL 指示で本ツールを呼ぶ→当該スレッドへ Reply-All 下書き作成・送信しない）。
      { name = "USE_MAIL_DRAFT_TOOL", value = "true" },
      # v0.3 Task3: 📅カレンダー登録（既定 false・tfvars で ON。ボタン描画フラグより先に ON にする）。
      { name = "USE_CALENDAR_EVENT_TOOL", value = var.use_calendar_event_tool ? "true" : "false" },
      # v0.3 Task4: 🗓日程候補提案（既定 false・ボタン描画フラグより先に ON）。
      { name = "USE_SCHEDULE_PROPOSE_TOOL", value = var.use_schedule_propose_tool ? "true" : "false" },
      # v0.3 Task6: AiLaVault ディープリンク注入（既定 false）。ON で検索結果に /app#client:<名前>
      # を付与。前提: connect-web が実 app.html 配信中（healthz source=s3）。app.html 側の
      # applyHashTarget は実装済み（build_app_html.py）＝この env の ON だけで機能する。
      { name = "USE_AILAVAULT_DEEPLINKS", value = var.use_ailavault_deeplinks ? "true" : "false" },
      # v0.3.1 Task7: ツール実行中の進捗表示（既定 false・fail-open）。ON で重いツールの実行前に
      # 「📂 資料を検索しています…」等を Slack へ投稿し完了後に削除する。SLACK_BOT_TOKEN 共用。
      { name = "ENABLE_PROGRESS_NOTIFY", value = var.enable_progress_notify ? "true" : "false" },
      # v0.3 Task8: 長文ペイロードの S3 退避（既定 false・allowlist対象のみ・PII系は対象外）。
      { name = "USE_PAYLOAD_OFFLOAD", value = var.use_payload_offload ? "true" : "false" },
      # 退避先 bucket（scrape 系の VSEO_REPORT_BUCKET と独立に指定＝両立時のキー衝突なし）。
      { name = "PAYLOAD_OFFLOAD_BUCKET", value = var.use_payload_offload ? aws_s3_bucket.raw_files.bucket : "" },
      # v0.3 Task10: 月間クォータ（既定 false・**migration 0017 の本番適用が前提**）。
      { name = "VIDEO_QUOTA_ENABLED", value = var.video_quota_enabled ? "true" : "false" },
      { name = "VIDEO_MONTHLY_QUOTA", value = tostring(var.video_monthly_quota) },
      # v0.3 Task10: 分析結果キャッシュ（既定 false・IAM 文とセット＝フラグだけでは動かない F1 型の回避）。
      { name = "ANALYSIS_CACHE_ENABLED", value = var.use_analysis_cache ? "true" : "false" },
      { name = "ANALYSIS_CACHE_BUCKET", value = var.use_analysis_cache ? aws_s3_bucket.raw_files.bucket : "" },
      # §知識ベース（2026-06-22）: 新スキーマ(documents/chunks=本番794件)を検索対象にし、
      # 資料種別フィルタ(knowledge_filters)と実ファイル配信(knowledge_deliver)を有効化。
      # USE_NEW_SCHEMA が無いと search は旧 proposals_chunks(3件)しか見ず知識機能が空振りする。
      { name = "USE_NEW_SCHEMA", value = "true" },
      { name = "USE_KNOWLEDGE_FILTERS", value = "true" },
      { name = "USE_KNOWLEDGE_DELIVER", value = "true" },
      # §知識ベース: knowledge_deliver の Drive DL は共有「個人OAuth」を使う。これが無いと
      # GOOGLE_APPLICATION_CREDENTIALS(Vertex SA) が選ばれ、SA は外部 Drive 非対応で DL 失敗する。
      { name = "GOOGLE_FORCE_OAUTH", value = "1" },
      # §知識ベース: Cohere 再ランク（クエリと資料本文の真の関連度で並べ替え＝「PR提案資料」が
      # 全部 cos~0.6 で似て見える問題を解消・無関係業界/客は低 relevance で落ちる）。
      { name = "USE_COHERE_RERANK", value = "true" },
      # 検索の関連性しきい値＋配信スコアゲート（rerank relevance スケール 0-1）。
      # 無関係/本文なしファイルを添付しない。飲料系検索で無関係客の PDF を誤添付した対策。
      { name = "SEARCH_MIN_RELEVANCE", value = "0.4" },
      { name = "KNOWLEDGE_DELIVER_MIN_SCORE", value = "0.5" },
      # 「資料の被り」対策（L1）: @AiLa の社内資料検索でも、テンプレページ由来の重複ヒットと
      # 同一資料の結果独占を抑える（近似重複の畳み込み＋同一資料の上限・既定2）。env で OFF 可。
      { name = "SEARCH_DEDUP_RESULTS", value = "true" },
      # テンプレ箇所/まるごと重複を検索から除外（ingest の boilerplate/doc-dedup の印を読む）。
      # 印が付くのは再取込後なので、印が無いうちは no-op（後方互換）。
      { name = "BOILERPLATE_EXCLUDE_SEARCH", value = "true" },
      { name = "DOC_DEDUP_EXCLUDE_SEARCH", value = "true" },
      # live パリティ（2026-07-11 監査）: 以下2本は CLI 直登録の taskdef(rev40) にのみ存在し terraform に
      # 無かったため、apply すると剥がれて機能劣化する状態だった。terraform を唯一の正に戻す。
      # mcp スキル/オーケストレーターの Bedrock モデル指定（コスト方針 2026-06-29 = Haiku 既定）。
      # var.bedrock_model_id（Lambda 用・tfvars=Sonnet）とは分離した mcp 専用変数を使う。
      { name = "BEDROCK_MODEL_ID", value = var.mcp_model_id },
      # pgvector HNSW の探索幅（検索品質/レイテンシのチューニング値・live=100）。
      { name = "SEARCH_HNSW_EF_SEARCH", value = "100" },
      # ナレッジ回答の末尾に「資料リンク」を付与（@AiLa=openclaw が markdown を装飾リンクへ
      # 変換する mcp のみ ON。connect-web(/app) は未設定＝生テキスト化しないため付けない）。
      { name = "SEARCH_ANSWER_SOURCE_LINKS", value = "1" },
      ], local.media_enabled == 1 ? [
      # Generic media delegation.  Legacy TIKTOK_* aliases point at the same
      # queue/table/bucket so the existing skill schema remains compatible.
      { name = "USE_TIKTOK_ACQUIRE", value = "1" },
      { name = "MEDIA_JOBS_TABLE", value = aws_dynamodb_table.tiktok_jobs[0].name },
      { name = "MEDIA_JOB_BUCKET", value = aws_s3_bucket.media_jobs[0].bucket },
      { name = "MEDIA_TASK_QUEUE", value = aws_sqs_queue.tiktok_jobs[0].url },
      { name = "MEDIA_ARTIFACT_TTL_SECONDS", value = tostring(var.media_artifact_ttl_seconds) },
      { name = "TIKTOK_JOBS_TABLE", value = aws_dynamodb_table.tiktok_jobs[0].name },
      { name = "TIKTOK_S3_BUCKET", value = aws_s3_bucket.media_jobs[0].bucket },
      { name = "TIKTOK_TASK_QUEUE", value = aws_sqs_queue.tiktok_jobs[0].url },
      ] : [], var.enable_x_research ? [
      # カタログ①〜⑤: X(Twitter)リサーチ/検索面チェック/コメントマイニング（2026-07 組み込み）。
      # Apify actor は Apify インフラで実行（MCP からは REST のみ）。VSEO_REPORT_BUCKET
      # （レポート署名URL発行先）は enable_scrape_tools 側で定義済み＝両フラグ併用が前提。
      { name = "USE_X_RESEARCH_TOOLS", value = "1" },
      { name = "USE_SEARCH_SURFACE_TOOL", value = "1" },
      { name = "USE_TIKTOK_COMMENT_TOOLS", value = "1" },
      # Part4 X投稿者の界隈マルチラベル分類。既定 false=bio送信も分類も表示もしない（完全no-op）。
      # 分類品質を実データ(apidojo の bio 取得率含む)で検証してから true にする。
      { name = "USE_KAIWAI_CLASSIFY", value = var.enable_kaiwai_classify ? "1" : "0" },
      # Part1 施策研究の永続記録(pgvector→AiLaVault)。RLS admin INSERT を owner/別社員/社外の
      # 3者の通常検索で検証してから true にする（既定 false=完全 no-op・後方互換）。
      { name = "USE_RESEARCH_PERSIST", value = var.enable_research_persist ? "1" : "0" },
      { name = "X_TASK_QUEUE", value = aws_sqs_queue.x_jobs[0].url },
      { name = "X_JOBS_TABLE", value = aws_dynamodb_table.x_jobs[0].name },
      { name = "X_S3_BUCKET", value = aws_s3_bucket.raw_files.bucket },
      # 分析(②分類/④山分析)は Sonnet を明示注入（暗黙昇格禁止＝既定 mcp_model_id は Haiku）。
      { name = "X_ANALYSIS_MODEL_ID", value = var.x_analysis_model_id },
      # SaaSコスト台帳（予算超過 fail-close・80%警告）。値変更は taskdef env 差し替えのみ。
      { name = "COST_GUARD_TABLE", value = aws_dynamodb_table.cost_usage[0].name },
      { name = "COST_APIFY_MONTHLY_USD", value = var.cost_apify_monthly_usd },
      # 段階公開（skill層 gate・空=全員許可）。stage1=小俣のみ→数名→空。CLI taskdef 差替で遷移。
      { name = "X_RESEARCH_ALLOWED_EMAILS", value = var.pr_research_allowed_emails },
      { name = "SEARCH_SURFACE_ALLOWED_EMAILS", value = var.pr_research_allowed_emails },
      { name = "COMMENT_MINING_ALLOWED_EMAILS", value = var.pr_research_allowed_emails },
      ] : [], var.enable_scrape_tools ? [
      # §M: video_algorithm が VSEO レポートを発行する非公開S3 bucket（presigned URL を出力に載せる）。
      { name = "VSEO_REPORT_BUCKET", value = aws_s3_bucket.raw_files.id },
      { name = "USE_VIDEO_TOOLS", value = "1" },
      { name = "USE_TIKTOK_TOOLS", value = "1" },
      # video_algorithm の既定「深掘り分析」本数（5〜10）。同期処理なので OpenClaw timeout(300s)と整合。
      # 値変更は taskdef env 差し替えのみで反映可（再ビルド不要）。
      { name = "VIDEO_ALGO_MAX_VIDEOS", value = var.video_algo_max_videos },
      # video_algorithm の「取得（上位ボード）」本数（5〜30・既定30）。メタのみ＝軽い。深掘りとは別管理。
      { name = "VIDEO_ALGO_BOARD_SIZE", value = var.video_algo_board_size },
      # §M改: Gemini は Vertex（本番EC2 .env.production と同値・SA JSON は entrypoint がファイル化）。
      { name = "GEMINI_USE_VERTEX", value = "true" },
      { name = "GEMINI_VERTEX_PROJECT", value = var.gemini_vertex_project },
      { name = "GEMINI_VERTEX_LOCATION", value = var.gemini_vertex_location },
    ] : [])
    secrets = concat([
      { name = "TEAMAGENT_MCP_BEARER", valueFrom = data.aws_secretsmanager_secret.bearer.arn },
      { name = "DATABASE_URL", valueFrom = data.aws_secretsmanager_secret.database_url.arn },
      # レポート短縮リンク(/r)の署名鍵。connect-web(connect_web.tf) と同一値=database_url を共用し
      # 発行(mcp/x_research skill)↔復号(connect-web /r)で鍵一致させる。新規 secret 不要
      # (database_url の GetSecretValue は mcp exec role に付与済み)。
      # ※短縮URLの実発行は env USE_REPORT_SHORTURL(既定OFF)で段階ゲート。connect-web に /r ルート
      #   ＋vseo-s3-read(bootstrap_vseo_s3_iam.sh)が揃い実機で /r→302 を確認した後に ON にする。
      #   本鍵/フラグ未設定でも skill は従来 presigned を返すため既存挙動は壊れない。
      { name = "MAIL_ACTION_HMAC_SECRET", valueFrom = data.aws_secretsmanager_secret.database_url.arn },
      # §U ハイブリッド identity: mcp が slack_user_id → 会社メールを server-side 解決して
      # per-user OAuth(mail_*/morning_digest) の token を引くために必要（users:read.email scope）。
      # openclaw と同じ secret を共用。会社共有グループ(search)はこれ無しでも動く（graceful degrade）。
      { name = "SLACK_BOT_TOKEN", valueFrom = data.aws_secretsmanager_secret.slack_bot.arn },
      # §U: 本人 token リフレッシュ用 Web クライアントの client_secret（connect-web と同じ data source）。
      { name = "CONNECT_GOOGLE_CLIENT_SECRET", valueFrom = data.aws_secretsmanager_secret.connect_google_client_secret[0].arn },
      # §U: oauth_connect の state 署名鍵（connect-web と同一値＝署名/検証を共有）。
      { name = "OAUTH_STATE_SECRET", valueFrom = data.aws_secretsmanager_secret.connect_oauth_state[0].arn },
      # §Slack個人連携(xoxp): DM 認可URL生成に必要（client_secret は connect-web callback 側のみ）。
      { name = "CONNECT_SLACK_CLIENT_ID", valueFrom = data.aws_secretsmanager_secret.connect_slack_client_id[0].arn },
      { name = "SLACK_OAUTH_STATE_SECRET", valueFrom = data.aws_secretsmanager_secret.slack_oauth_state_secret[0].arn },
      # §知識ベース: 共有「個人OAuth」3点を google_oauth(JSON) から個別キー抽出して注入。
      # knowledge_deliver が GDriveClient.from_env で本人 Drive token を組み立て実ファイルを DL。
      { name = "GOOGLE_CLIENT_ID", valueFrom = "${data.aws_secretsmanager_secret.google_oauth[0].arn}:client_id::" },
      { name = "GOOGLE_CLIENT_SECRET", valueFrom = "${data.aws_secretsmanager_secret.google_oauth[0].arn}:client_secret::" },
      { name = "GOOGLE_OAUTH_REFRESH_TOKEN", valueFrom = "${data.aws_secretsmanager_secret.google_oauth[0].arn}:refresh_token::" },
      ], var.enable_scrape_tools ? [
      { name = "VERTEX_SA_JSON", valueFrom = data.aws_secretsmanager_secret.vertex_sa[0].arn },
      ] : [], (var.enable_x_research && var.tiktok_apify_secret_arn != "") ? [
      # カタログ①〜⑤: Apify トークン（tiktok/apify-token を共用＝新設しない・計画裁定）。
      { name = "APIFY_API_TOKEN", valueFrom = var.tiktok_apify_secret_arn },
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
      command     = ["CMD", local.teamagent_python, "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4).close()"]
      interval    = 30
      timeout     = 5
      retries     = 5
      startPeriod = 40
    }
    command = [local.teamagent_python, "/app/scripts/run_mcp_vertex_entrypoint.py"]
  })])
}

resource "aws_ecs_task_definition" "openclaw" {
  # Empty keeps an intentionally unmanaged legacy deployment out of this
  # module. Any Terraform-managed image revision uses the one-time production
  # release gate below; direct CLI task-definition registration is retired.
  count                    = var.openclaw_image == "" ? 0 : 1
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
  depends_on         = [terraform_data.production_image_release_gate]

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
      # §U: DM 許可リスト（カンマ区切り Slack user_id）。entrypoint が起動時に allowFrom へ注入。
      # メンバー追加は本 var を編集 + apply（task 再デプロイ）だけ＝image rebuild 不要・15名まで可動。
      { name = "SLACK_DM_ALLOWLIST", value = var.slack_dm_allowlist },
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
  task_definition = aws_ecs_task_definition.openclaw[0].arn
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
