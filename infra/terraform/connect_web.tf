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

# ---------- Slack 個人OAuth(xoxp) 用（2026-07-07 追加） ----------
variable "connect_slack_client_id_secret_name" {
  description = "CONNECT_SLACK_CLIENT_ID の Secrets Manager 名（Slack app OAuth client_id）"
  type        = string
  default     = "teamagent/dev/connect_slack_client_id"
}
variable "connect_slack_client_secret_name" {
  description = "CONNECT_SLACK_CLIENT_SECRET の Secrets Manager 名"
  type        = string
  default     = "teamagent/dev/connect_slack_secret"
}
variable "slack_oauth_state_secret_name" {
  description = "SLACK_OAUTH_STATE_SECRET の Secrets Manager 名（Slack state 署名・Google と分離）"
  type        = string
  default     = "teamagent/dev/slack_oauth_state_secret"
}
variable "slack_oauth_redirect_uri" {
  description = "SLACK_OAUTH_REDIRECT_URI（Slack app に登録した callback URL）"
  type        = string
  default     = "https://connect.newstv.co.jp/slack/oauth/callback"
}

variable "connect_oauth_kms_key_id" {
  description = "OAUTH_KMS_KEY_ID（oauth_tokens 暗号化に使う既存 KMS 鍵の ARN・既定 alias 解決）"
  type        = string
  default     = "" # 空なら data.aws_kms_alias.connect_oauth（alias/teamagent-oauth-tokens）で解決した key arn を使用
}

variable "connect_web_vpc_cidr" {
  description = "connect-web の ingress を許可する VPC CIDR（ALB→Fargate の経路・internal ALB は VPC 内）"
  type        = string
  default     = "172.31.0.0/16"
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
data "aws_secretsmanager_secret" "connect_slack_client_id" {
  count = var.enable_connect_web ? 1 : 0
  name  = var.connect_slack_client_id_secret_name
}
data "aws_secretsmanager_secret" "connect_slack_client_secret" {
  count = var.enable_connect_web ? 1 : 0
  name  = var.connect_slack_client_secret_name
}
data "aws_secretsmanager_secret" "slack_oauth_state_secret" {
  count = var.enable_connect_web ? 1 : 0
  name  = var.slack_oauth_state_secret_name
}

# KMS 鍵：`alias/teamagent-oauth-tokens`（oauth_tokens 暗号化）。もとは aiia と共用だった鍵で、
# aiia-mcp 退役（#128）後も connect-web が使い続けるため、退役時の aiia リソース掃除で削除しないこと。
data "aws_kms_alias" "connect_oauth" {
  count = var.enable_connect_web ? 1 : 0
  name  = "alias/teamagent-oauth-tokens"
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
    resources = [
      data.aws_secretsmanager_secret.connect_oauth_state[0].arn,
      data.aws_secretsmanager_secret.connect_google_client_secret[0].arn,
      data.aws_secretsmanager_secret.connect_slack_client_id[0].arn,
      data.aws_secretsmanager_secret.connect_slack_client_secret[0].arn,
      data.aws_secretsmanager_secret.slack_oauth_state_secret[0].arn,
      data.aws_secretsmanager_secret.database_url.arn,
      aws_secretsmanager_secret.report_link_hmac.arn,
    ]
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
# P4 で同一タスクに「資料検索 Web UI」を載せ、SearchSkill（Bedrock 要約 + Cohere Rerank）を
# プロセス内で実行するため Bedrock 権限を追加する（旧コメントの「Bedrock は不要」は P4 で失効）。
data "aws_iam_policy_document" "connect_web_task" {
  count = var.enable_connect_web ? 1 : 0
  statement {
    sid     = "KmsEncryptDecryptOauthTokens"
    actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
    resources = [
      data.aws_kms_alias.connect_oauth[0].target_key_arn,
    ]
  }
  # P4 資料検索 Web UI: SearchSkill の要約（Converse / InvokeModel）に必要。
  statement {
    sid = "BedrockInvokeForSearch"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
    ]
    resources = local.bedrock_resources
  }
  # P4 資料検索 Web UI: Cohere Rerank（USE_COHERE_RERANK 有効時の関連度並べ替え）。
  # bedrock:Rerank は InvokeModel とは別アクションなので明示付与が必要（fargate.tf の MCP と同型）。
  statement {
    sid       = "BedrockRerankForSearch"
    actions   = ["bedrock:Rerank"]
    resources = ["arn:aws:bedrock:${var.aws_region}::foundation-model/cohere.rerank-v3-5:0"]
  }
  # レポート短縮リンク(/r)が presigned を再生成するため、署名プリンシパル(=connect-web task role)に
  # 当該 prefix の GetObject が必要（無いとブラウザ取得時 403）。vseo-reports/=x_research 等、
  # vseo-proposals/=提案 PPTX/PDF。バケット全体でなく2 prefix に限定（最小権限）。
  # ※即時ロールアウトは bootstrap_vseo_s3_iam.sh の別名 inline policy で付与（apply で剥がれない）。
  statement {
    sid     = "VseoReportS3Read"
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.raw_files.arn}/vseo-reports/*",
      "${aws_s3_bucket.raw_files.arn}/vseo-proposals/*",
    ]
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

  lifecycle {
    precondition {
      condition     = local.report_link_hmac_transition_valid
      error_message = "HMAC rollout preflight failed for connect-web; direct/targeted task-definition apply is blocked."
    }
  }

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
    environment = concat([
      { name = "AWS_REGION", value = var.aws_region },
      # レポート短縮リンク(/r)が presigned を再生成する対象バケット。decode_report_token の
      # bucket allowlist にも使う（mcp 側 VSEO_REPORT_BUCKET と同一値＝トークンの bucket と一致）。
      { name = "VSEO_REPORT_BUCKET", value = aws_s3_bucket.raw_files.id },
      { name = "CONNECT_WEB_HOST", value = "0.0.0.0" },
      { name = "CONNECT_WEB_PORT", value = "8788" },
      { name = "OAUTH_REDIRECT_URI", value = var.connect_redirect_uri },
      { name = "SLACK_OAUTH_REDIRECT_URI", value = var.slack_oauth_redirect_uri },
      { name = "SLACK_TEAM_ID", value = var.slack_team_id },
      { name = "CONNECT_BASE_URL", value = var.connect_base_url },
      { name = "CONNECT_GOOGLE_CLIENT_ID", value = var.connect_google_client_id },
      # /app・/search を @vectorinc.co.jp の社員全員に開放（email_verified + 会社ドメイン hd 許可）。
      # 未設定だと既定 allowlist（s-komata 1名）のみ。2026-07-07 ユーザー承認（全社ナレッジ共有）。
      { name = "CONNECT_SEARCH_ALLOWED_HD", value = "vectorinc.co.jp" },
      # /app（Obsidian UI）HTML ホットスワップの受け口（07-12 ECS 直運用で追加）。app.py が
      # この URI の HTML を配信し、未設定だと「準備中」プレースホルダへ回帰する。tf に無かった
      # ため apply のたびに剥がれ /app 断となった（2026-07-13 td:42→43 応急復旧→44/45 で再発）。
      # 値は infra/deploy/publish_app_html.sh・deploy_connectweb_unified.sh の配置先と同一定数。
      # S3 読取は task role の inline policy `apphtml-s3-read`（bootstrap_apphtml_s3_iam.sh 付与・
      # 名前が別なので terraform apply では剥がれない）。tf 取込時は connect_web_task policy へ。
      { name = "CONNECT_APP_HTML_S3_URI", value = "s3://${aws_s3_bucket.raw_files.bucket}/codebuild/connect-web-app.html" },
      { name = "OAUTH_KMS_KEY_ID", value = var.connect_oauth_kms_key_id != "" ? var.connect_oauth_kms_key_id : data.aws_kms_alias.connect_oauth[0].target_key_arn },
      { name = "OAUTH_KMS_REGION", value = var.aws_region },
      { name = "STRUCTLOG_FORMAT", value = "json" },
      # コスト方針(2026-06-29)=Haiku。未設定だと /search の要約合成がコード既定へ落ちる
      # （2026-07-13 実測で Sonnet 呼び出しを確認）。品質を上げたい場合はこの値を明示変更する。
      { name = "BEDROCK_MODEL_ID", value = var.mcp_model_id },
      # P4 検索 UI（/search・/api/v1/search）が SearchSkill を新スキーマで動かすための
      # フラグ。USE_NEW_SCHEMA を入れ忘れると factory が既定 false → RLS 未適用の旧
      # proposals_chunks を引いてしまい RLS スコープが no-op になる（セキュリティ上必須）。
      { name = "USE_NEW_SCHEMA", value = "true" },
      # live パリティ（2026-07-11 反対尋問レビューで検出）: /search の Cohere 再ランクは live rev39 で
      # 明示 OFF（mcp 側=true とは別判断。/search は SEARCH_MIN_RELEVANCE=0.0 で全件表示＋UI 側で
      # relevance 判断させる設計のため、rerank コスト/レイテンシを掛けない）。true に戻すと apply で
      # 検索順位が変動し Bedrock 呼び出しが増える。
      { name = "USE_COHERE_RERANK", value = "false" },
      { name = "USE_CLIENT_BOOST", value = "true" },
      # 「資料の被り」対策（L1）: 営業資料はテンプレページ（表紙/会社紹介/料金）を使い回すため、
      # 検索結果でテンプレチャンクが複数資料から重複ヒット＆同一資料が結果を独占する。
      # rerank/min_relevance の後段で「近似重複の畳み込み＋同一資料の上限(既定2)」を噛ませる。
      { name = "SEARCH_DEDUP_RESULTS", value = "true" },
      # テンプレ箇所/まるごと重複を検索から除外（ingest の boilerplate/doc-dedup の印を読む）。
      # 印が付くのは再取込後なので、印が無いうちは no-op（後方互換）。
      { name = "BOILERPLATE_EXCLUDE_SEARCH", value = "true" },
      { name = "DOC_DEDUP_EXCLUDE_SEARCH", value = "true" },
      # 意味クラスタ・エッジ（L3A）: 資料の代表ベクトル(全チャンク平均)で kNN を取り、
      # 「タグは違うが意味的に近い」資料を弱い concept リンクで結ぶ＝AIならではの発見線。
      # ★初期は OFF で出荷。E5 系埋め込みは無関係ペアでも cosine ベースラインが高く、固定しきい値
      #   のまま点灯すると団子化（ハリネズミ）再発の恐れがあるため、実データで較正してから ON にする。
      #   ON の手順（再ビルド不要・この env を差し替えるだけ）:
      #     GRAPH_CONCEPT_EDGES=true / GRAPH_CONCEPT_THRESHOLD=0.90 等で点灯→グラフを見て上げ下げ。
      { name = "GRAPH_CONCEPT_EDGES", value = "false" },
      # live パリティ（2026-07-11 監査）: 以下8本は CLI 直登録の taskdef(rev39) にのみ存在し terraform に
      # 無かったため、apply すると剥がれて 16名共有の検索 UI の品質が一斉にデフォルト回帰する状態だった。
      # 値はすべて live rev39 と同値。terraform を唯一の正に戻す。
      # テンプレ文書そのものを検索対象から除外（boilerplate 除外より強い・資料単位）。
      { name = "TEMPLATE_EXCLUDE_SEARCH", value = "1" },
      # PR#177 ルール分類による doc_kind フィルタ（提案書/議事録等の種別絞り込み）。
      { name = "USE_DOC_KIND_RULES", value = "1" },
      # /search は @AiLa と異なり足切りなし（UI 側で relevance 表示・ユーザーが判断）。
      { name = "SEARCH_MIN_RELEVANCE", value = "0.0" },
      # クライアント名一致の優先ソートと予算ソート。
      { name = "SEARCH_CLIENT_MATCH_SORT", value = "1" },
      { name = "SEARCH_BUDGET_SORT", value = "true" },
      # pgvector HNSW の探索幅（mcp と同値・live=100）。
      { name = "SEARCH_HNSW_EF_SEARCH", value = "100" },
      # クエリプランナは live で明示 OFF（値ごと保存し将来の ON/OFF を taskdef 差し替えだけにする）。
      { name = "USE_QUERY_PLANNER", value = "false" },
      # ナレッジフィルタ UI（種別/期間などの絞り込み）。
      { name = "USE_KNOWLEDGE_FILTERS", value = "true" },
    ], local.report_link_hmac_environment)
    secrets = concat([
      { name = "OAUTH_STATE_SECRET", valueFrom = data.aws_secretsmanager_secret.connect_oauth_state[0].arn },
      { name = "CONNECT_GOOGLE_CLIENT_SECRET", valueFrom = data.aws_secretsmanager_secret.connect_google_client_secret[0].arn },
      { name = "CONNECT_SLACK_CLIENT_ID", valueFrom = data.aws_secretsmanager_secret.connect_slack_client_id[0].arn },
      { name = "CONNECT_SLACK_CLIENT_SECRET", valueFrom = data.aws_secretsmanager_secret.connect_slack_client_secret[0].arn },
      { name = "SLACK_OAUTH_STATE_SECRET", valueFrom = data.aws_secretsmanager_secret.slack_oauth_state_secret[0].arn },
      { name = "DATABASE_URL", valueFrom = data.aws_secretsmanager_secret.database_url.arn },
    ], local.report_link_hmac_secrets)
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
