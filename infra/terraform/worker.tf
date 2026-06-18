# ============================================================
# EC2 常駐ワーカー（Slack Bot + VSEO/動画審査パイプライン）
# ============================================================
# 目的:
#   - 会社プロキシの外で yt-dlp を動かし TikTok CDN の SSL 傍受を根治
#   - Slack Socket Mode Bot を 24h 常駐（個人Mac非依存・チーム共用）
#   - レポートは S3 署名付きURLで配信（report_publish.py）
#
# 設計:
#   - Socket Mode は外向きのみ → SG は受信なし・送信全許可（踏み台と同型）
#   - SSM Session Manager 接続（SSHキー/22番不要）
#   - t4g.medium（arm64, 4GB）≒ $29/月。Chrome+ffmpeg 同時実行の余裕
#   - 純加算: 既存 RDS / 踏み台 / Lambda リソースには一切触れない
#
# 接続:  aws ssm start-session --target <id> --region ap-northeast-1
# ============================================================

variable "worker_instance_type" {
  description = "ワーカーEC2のインスタンスタイプ（t4g.medium≒$29/月。縮小はt4g.small≒$14）"
  type        = string
  default     = "t4g.medium"
}

variable "worker_root_gb" {
  description = "ルートEBS(gp3)サイズGB（動画一時/Chrome/venv用）"
  type        = number
  default     = 30
}

# ---------- IAM ロール（SSM + Secrets + S3 + Bedrock + Logs）----------
data "aws_iam_policy_document" "worker_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = "${var.project_name}-${var.environment}-worker"
  assume_role_policy = data.aws_iam_policy_document.worker_assume.json
}

resource "aws_iam_role_policy_attachment" "worker_ssm" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "worker_app" {
  # dev 配下の Secrets（Google OAuth / Slack / DB 等）を読む
  statement {
    sid       = "ReadDevSecrets"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [
      "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${var.project_name}/${var.environment}/*",
    ]
  }
  # レポート/生ファイル用 S3（report_publish.py の署名付きURL発行先）
  statement {
    sid       = "RawFilesBucket"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.raw_files.arn, "${aws_s3_bucket.raw_files.arn}/*"]
  }
  # Bedrock 推論（検索/LLM系スキル。Gemini は Google Vertex=AWS権限不要）
  statement {
    sid     = "BedrockInvoke"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream",
    ]
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
    ]
  }
  # アプリログ（任意・CloudWatch agent 用）
  statement {
    sid       = "AppLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["${aws_cloudwatch_log_group.app.arn}:*"]
  }
}

resource "aws_iam_role_policy" "worker_app" {
  name   = "${var.project_name}-${var.environment}-worker-app"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_app.json
}

resource "aws_iam_instance_profile" "worker" {
  name = "${var.project_name}-${var.environment}-worker"
  role = aws_iam_role.worker.name
}

# ---------- SG（受信なし・送信全許可。Socket Mode は外向きのみ）----------
resource "aws_security_group" "worker" {
  name        = "${var.project_name}-${var.environment}-worker-sg"
  description = "TeamAgent worker SG (egress only)"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-worker-sg"
  }
}

# RDS への 5432 を worker SG から許可（踏み台と同じ standalone ルール=純加算）
resource "aws_security_group_rule" "db_from_worker" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.worker.id
  security_group_id        = aws_security_group.db.id
  description              = "PostgreSQL from worker"
}

# ---------- ワーカー本体 ----------
resource "aws_instance" "worker" {
  ami                    = data.aws_ami.al2023_arm.id
  instance_type          = var.worker_instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.worker.id]
  iam_instance_profile   = aws_iam_instance_profile.worker.name

  root_block_device {
    volume_size = var.worker_root_gb
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens   = "required" # IMDSv2 必須（SSRF対策）
    http_endpoint = "enabled"
  }

  # 依存の事前インストール（コード/Chrome本体/secrets はデプロイ段階で投入）
  user_data = <<-EOF
    #!/bin/bash
    set -x
    exec > /var/log/teamagent-bootstrap.log 2>&1
    # システム依存
    dnf install -y python3.11 python3.11-pip git tar gzip xz gcc nodejs npm \
      nss nspr atk at-spi2-atk cups-libs libdrm mesa-libgbm libxkbcommon \
      libXcomposite libXdamage libXrandr libXScrnSaver libXtst pango cairo \
      alsa-lib gtk3 liberation-fonts || true
    # ffmpeg（AL2023 標準repoに無いので arm64 static）
    cd /tmp
    curl -fsSL -o ff.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz || true
    tar xf ff.tar.xz || true
    cp ffmpeg-*-static/ffmpeg ffmpeg-*-static/ffprobe /usr/local/bin/ 2>/dev/null || true
    # アプリ配置先
    mkdir -p /opt/teamagent/app
    # デプロイスクリプト（Phase2 で S3 から tarball を取得して起動）
    cat > /opt/teamagent/deploy.sh <<'DEP'
    #!/bin/bash
    set -euo pipefail
    BUCKET="${aws_s3_bucket.raw_files.id}"
    cd /opt/teamagent
    aws s3 cp "s3://$BUCKET/deploy/teamagent-bot.tar.gz" /tmp/app.tar.gz
    aws s3 cp "s3://$BUCKET/deploy/teamagent.env.base" /opt/teamagent/teamagent.env.base
    rm -rf /opt/teamagent/app && mkdir -p /opt/teamagent/app
    tar xzf /tmp/app.tar.gz -C /opt/teamagent/app
    cd /opt/teamagent/app
    python3.11 -m venv .venv
    ./.venv/bin/pip install -U pip
    ./.venv/bin/pip install -e . || ./.venv/bin/pip install -r requirements.txt || true
    npx --yes @puppeteer/browsers install chrome@stable --path /opt/teamagent/chrome || true
    systemctl restart teamagent-bot || true
    DEP
    chmod +x /opt/teamagent/deploy.sh
    # systemd ユニット（コード投入後に enable/start）
    cat > /etc/systemd/system/teamagent-bot.service <<'SVC'
    [Unit]
    Description=TeamAgent Slack Bot (Socket Mode)
    After=network-online.target
    Wants=network-online.target
    [Service]
    Type=simple
    WorkingDirectory=/opt/teamagent/app
    # env 既設なら load_secrets は skip し、SM 一時不達でも起動できるようにする（load_secrets
    # 内部にもタイムアウトと env 優先 fallback あり・2026-06-18 SM 不達インシデント対策）。
    ExecStart=/bin/bash -lc 'set -a; source /opt/teamagent/teamagent.env.base; if [[ -z "$DATABASE_URL" || -z "$SLACK_BOT_TOKEN" ]]; then source scripts/load_secrets.sh; fi; set +a; exec ./.venv/bin/python -m teamagent.runtime.slack_bot'
    Restart=always
    RestartSec=5
    TimeoutStartSec=60
    Environment=PYTHONUNBUFFERED=1
    [Install]
    WantedBy=multi-user.target
    SVC
    # connect-web は **起動時に DB/Secret を要求しない**（create_app は OAUTH_REDIRECT_URI のみ・
    # store(KMS/RDS) は /oauth2/callback 初回まで遅延）。よって load_secrets を起動 path から
    # 外し、env(.env.base) だけで起動できる構成にする（SM 不達で起動ごと無限ハングを防止・
    # 2026-06-18 インシデントの恒久対策）。call back 動作に必要な値は teamagent.env.base か
    # 一時 EnvironmentFile で注入。
    cat > /etc/systemd/system/teamagent-connect.service <<'SVC'
    [Unit]
    Description=TeamAgent Connect Web (per-user OAuth callback)
    After=network-online.target
    Wants=network-online.target
    [Service]
    Type=simple
    WorkingDirectory=/opt/teamagent/app
    EnvironmentFile=-/opt/teamagent/connect_web.env
    ExecStart=/bin/bash -lc 'set -a; source /opt/teamagent/teamagent.env.base; if [[ -n "$CONNECT_WEB_LOAD_SECRETS" && -z "$DATABASE_URL" ]]; then source scripts/load_secrets.sh || true; fi; set +a; exec ./.venv/bin/python -m teamagent.connect_web'
    Restart=always
    RestartSec=5
    TimeoutStartSec=60
    Environment=PYTHONUNBUFFERED=1
    [Install]
    WantedBy=multi-user.target
    SVC
    systemctl daemon-reload
    echo "bootstrap-done"
  EOF

  user_data_replace_on_change = false

  tags = {
    Name = "${var.project_name}-${var.environment}-worker"
    Role = "slack-bot-worker"
  }
}

output "worker_instance_id" {
  description = "ワーカー EC2 のインスタンス ID"
  value       = aws_instance.worker.id
}

output "worker_connect_command" {
  description = "ワーカーへの SSM 接続コマンド"
  value       = "aws ssm start-session --target ${aws_instance.worker.id} --region ${var.aws_region}"
}
