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
systemctl daemon-reload
systemctl enable teamagent-bot
systemctl restart teamagent-bot
sleep 6
systemctl status teamagent-bot --no-pager | tail -20
RSH
)
REMOTE="${REMOTE/__BUCKET__/$BUCKET}"
B64="$(printf '%s' "$REMOTE" | base64 | tr -d '\n')"
CID=$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript --comment "TeamAgent bot deploy" \
  --parameters commands="echo $B64 | base64 -d | bash" \
  --query Command.CommandId --output text)
echo "   CommandId=$CID（完了待ち・最大10分）"
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
