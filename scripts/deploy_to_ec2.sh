#!/usr/bin/env bash
# ============================================================
# TeamAgent Bot を EC2 worker へデプロイ
# （コード tarball + 非秘密 env.base を S3 → SSM で展開・起動）
#
# 北極星: Mac 非常時稼働・会社プロキシ外で TikTok DL が SSL 根治 → VSEO 素で 10/10。
#
# 前提（runbook docs/v3.2/ec2_cutover_runbook.md 参照）:
#   1. Mac 側 Bot を停止済み（Socket Mode 二重接続を避ける）
#   2. EC2 worker 起動済み: aws ec2 start-instances --instance-ids i-0feaa3c103ab6ef91
#   3. Vertex SA を Secrets Manager に投入済み: teamagent/dev/vertex_sa
#   4. ローカルに .env.production がある（env.base の素）
#   5. aws CLI が worker を操作できる IAM（SSM / S3）
#
# Usage:
#   scripts/deploy_to_ec2.sh           # DRY-RUN: tarball/env.base を作って検証のみ（S3/SSM 不変更）
#   scripts/deploy_to_ec2.sh --go      # 実デプロイ: S3 upload + SSM で展開・bot 起動
# ============================================================
set -euo pipefail

REGION="${AWS_REGION:-ap-northeast-1}"
INSTANCE_ID="${WORKER_INSTANCE_ID:-i-0feaa3c103ab6ef91}"
BUCKET="${DEPLOY_BUCKET:-teamagent-dev-raw-files}"
GO=0
[[ "${1:-}" == "--go" ]] && GO=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== 1. コード tarball（git archive HEAD = 追跡ファイルのみ→秘密/.env/node_modules は非同梱）=="
git archive --format=tar.gz -o "$WORK/teamagent-bot.tar.gz" HEAD
echo "   size: $(du -h "$WORK/teamagent-bot.tar.gz" | cut -f1)"

echo "== 2. teamagent.env.base 生成（.env.production + infra/deploy/ec2.overrides.env）=="
[[ -f .env.production ]] || { echo "ERROR: .env.production が見つからない"; exit 1; }
{
  cat .env.production
  echo ""
  echo "# ===== EC2 overrides (infra/deploy/ec2.overrides.env) ====="
  cat infra/deploy/ec2.overrides.env
} >"$WORK/teamagent.env.base"

echo "== 3. 秘密混入チェック（env.base は *_SECRET_NAME のみ・実値ゼロ）=="
if grep -Eiq 'xoxb-|xapp-|AKIA[0-9A-Z]{15}|-----BEGIN|PRIVATE KEY' "$WORK/teamagent.env.base"; then
  echo "ERROR: env.base に秘密らしき実値を検出。中止（Secrets Manager 経由にすること）。"
  exit 1
fi
echo "   OK（実値なし）"

echo "== 3b. 連携 必須 env 存在チェック（沈黙故障=全員未連携 を防ぐ）=="
_MISSING=""
for k in OAUTH_REDIRECT_URI OAUTH_KMS_KEY_ID OAUTH_KMS_REGION OAUTH_STATE_SECRET_NAME CONNECT_WEB_HOST; do
  grep -qE "^${k}=" "$WORK/teamagent.env.base" || _MISSING="$_MISSING $k"
done
if [[ -n "$_MISSING" ]]; then
  echo "ERROR: env.base に連携必須キーが不足:${_MISSING}"
  echo "       無いと Bot は起動するが TokenStore が InMemory に落ち『全員未連携』の沈黙故障になる。"
  echo "       infra/deploy/ec2.overrides.env を確認してください。"
  exit 1
fi
echo "   OK（連携 env 一式あり）"

if [[ "$GO" -ne 1 ]]; then
  echo ""
  echo "== DRY-RUN 完了 =="
  echo "   生成物（破棄されます）: $WORK"
  echo "   env.base 行数: $(wc -l <"$WORK/teamagent.env.base")"
  echo "   実デプロイ: $0 --go"
  exit 0
fi

echo "== 4. S3 アップロード =="
aws s3 cp "$WORK/teamagent-bot.tar.gz" "s3://$BUCKET/deploy/teamagent-bot.tar.gz" --region "$REGION"
aws s3 cp "$WORK/teamagent.env.base" "s3://$BUCKET/deploy/teamagent.env.base" --region "$REGION"

echo "== 5. SSM でリモート展開（venv/pip + scraper npm ci + Chrome 解決 + systemd 起動）=="
REMOTE=$(cat <<'RSH'
set -euo pipefail
BUCKET="__BUCKET__"
cd /opt/teamagent
aws s3 cp "s3://$BUCKET/deploy/teamagent-bot.tar.gz" /tmp/app.tar.gz
aws s3 cp "s3://$BUCKET/deploy/teamagent.env.base" /opt/teamagent/teamagent.env.base
rm -rf app && mkdir -p app && tar xzf /tmp/app.tar.gz -C app
cd app
# /tmp は tmpfs(2GB) で torch 等の展開が溢れる → TMPDIR をディスクへ
mkdir -p /opt/teamagent/piptmp
export TMPDIR=/opt/teamagent/piptmp
python3.11 -m venv .venv
./.venv/bin/pip install -q -U pip
./.venv/bin/pip install -q -e .
# search の e5 embedder(torch含む・重い)。pyproject 外なので明示導入
./.venv/bin/pip install -q --no-cache-dir sentence-transformers
# TikTok スクレイパの node 依存（node_modules は tarball 非同梱）
( cd tools/tiktok_scraper && npm ci --no-audit --no-fund )
# Chrome: Chrome-for-Testing に arm64 Linux 版が無い(t4g=arm64)→ Playwright chromium(arm64) を使う
export PLAYWRIGHT_BROWSERS_PATH=/opt/teamagent/pw
./.venv/bin/python -m playwright install chromium >/tmp/pw_install.log 2>&1 || true
PW_CHROME="$(find /opt/teamagent/pw -type f -name chrome 2>/dev/null | head -1)"
if [[ -n "$PW_CHROME" ]]; then
  sed -i '/^CHROMIUM_PATH=/d' /opt/teamagent/teamagent.env.base
  echo "CHROMIUM_PATH=$PW_CHROME" >> /opt/teamagent/teamagent.env.base
fi
rm -rf /opt/teamagent/piptmp
# OOM 安全網: swap 4GB（e5 ロード時のメモリ逼迫対策・永続）
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=4096 status=none
  chmod 600 /swapfile && mkswap /swapfile >/dev/null 2>&1 && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
# connect_web(OAuth コールバック /oauth2/callback) を常駐させる unit を冪等に設置。
# 既存インスタンスは user_data を再実行しないため、デプロイ毎にここで ensure する。
# これが無いと ALB ターゲットが永久 UNHEALTHY＝連携リンクは出ても完了しない。
cat > /etc/systemd/system/teamagent-connect.service <<'CONNSVC'
[Unit]
Description=TeamAgent connect_web (OAuth callback receiver)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5
[Service]
Type=simple
WorkingDirectory=/opt/teamagent/app
ExecStart=/bin/bash -lc 'set -a; source /opt/teamagent/teamagent.env.base; source scripts/load_secrets.sh; set +a; exec ./.venv/bin/python -m teamagent.connect_web'
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
[Install]
WantedBy=multi-user.target
CONNSVC
# 旧 connect_web(systemd管理外の手動起動)が 8788 を掴んでいると unit が bind 失敗するため先に止める。
systemctl stop teamagent-connect 2>/dev/null || true
( command -v fuser >/dev/null && fuser -k 8788/tcp ) 2>/dev/null || pkill -f 'teamagent.connect_web' 2>/dev/null || true
sleep 2
systemctl daemon-reload
systemctl enable teamagent-bot teamagent-connect
systemctl restart teamagent-bot teamagent-connect
sleep 6
echo "----- teamagent-bot -----"; systemctl status teamagent-bot --no-pager | tail -15
echo "----- teamagent-connect -----"; systemctl status teamagent-connect --no-pager | tail -15
ss -ltnp 2>/dev/null | grep -q ':8788' && echo "OK: connect_web listening on 8788" || echo "WARN: connect_web が 8788 を listen していない（連携不可）"
RSH
)
REMOTE="${REMOTE/__BUCKET__/$BUCKET}"
B64="$(printf '%s' "$REMOTE" | base64 | tr -d '\n')"
CID="$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript --comment "TeamAgent bot deploy" \
  --parameters commands="echo $B64 | base64 -d | bash" \
  --query Command.CommandId --output text 2>/dev/null || true)"
if [[ -z "${CID:-}" || "${CID}" == "None" ]]; then
  echo "ERROR: SSM send-command が CommandId を返しませんでした（IAM/接続/サイズを確認）" >&2
  exit 1
fi
echo "   CommandId=${CID}（完了待ち・最大10分）"
for i in $(seq 1 60); do
  sleep 10
  ST=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CID" --instance-id "$INSTANCE_ID" --query Status --output text 2>/dev/null || echo Pending)
  echo "   [$((i * 10))s] $ST"
  [[ "$ST" == "Success" || "$ST" == "Failed" || "$ST" == "TimedOut" ]] && break
done
echo "----- STDOUT -----"
aws ssm get-command-invocation --region "$REGION" --command-id "$CID" --instance-id "$INSTANCE_ID" --query StandardOutputContent --output text
echo "----- STDERR(tail) -----"
aws ssm get-command-invocation --region "$REGION" --command-id "$CID" --instance-id "$INSTANCE_ID" --query StandardErrorContent --output text | tail -8
echo "== 完了 == Slack で『@TeamAgent VSEO分析 新宿 ランチ』を確認。問題あれば runbook のロールバックへ。"
