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

# ── AiLa PDCA loop 物理ガード（RULES.md §1.4 Q7 / SHOGO_ACTIONS action 5）──────────
# PDCA loop（Maker subagent）は dev/prod 問わず deploy 系を実行禁止。
# Skill が PDCA_LOOP_MODE=1 を強制 env で立てるので、ここで物理的に弾く。
if [ "${PDCA_LOOP_MODE:-}" = "1" ]; then
  echo "blocked: deploy_to_ec2.sh は PDCA_LOOP_MODE=1 では実行禁止（RULES.md §1.4 Q7）" >&2
  exit 1
fi

REGION="${AWS_REGION:-ap-northeast-1}"
INSTANCE_ID="${WORKER_INSTANCE_ID:-i-0feaa3c103ab6ef91}"
BUCKET="${DEPLOY_BUCKET:-teamagent-dev-raw-files}"
HMAC_PREFLIGHT_MANIFEST="${HMAC_PREFLIGHT_MANIFEST:-}"
HMAC_ROLLOUT_CONTROL="${HMAC_ROLLOUT_CONTROL:-}"
HMAC_WORKER_ENV="${HMAC_WORKER_ENV:-}"
HMAC_WORKER_ARTIFACT="${HMAC_WORKER_ARTIFACT:-}"
HMAC_WORKER_ROLLBACK_ARTIFACT="${HMAC_WORKER_ROLLBACK_ARTIFACT:-}"
HMAC_WORKER_ROLLBACK_ENV="${HMAC_WORKER_ROLLBACK_ENV:-}"
HMAC_WORKER_PROVENANCE_RECEIPT="${HMAC_WORKER_PROVENANCE_RECEIPT:-}"
HMAC_WORKER_PROVENANCE_SIGNATURE="${HMAC_WORKER_PROVENANCE_SIGNATURE:-}"
HMAC_WORKER_ROLLBACK_PROVENANCE_RECEIPT="${HMAC_WORKER_ROLLBACK_PROVENANCE_RECEIPT:-}"
HMAC_WORKER_ROLLBACK_PROVENANCE_SIGNATURE="${HMAC_WORKER_ROLLBACK_PROVENANCE_SIGNATURE:-}"
HMAC_WORKER_PROVENANCE_KEY_ARN="${HMAC_WORKER_PROVENANCE_KEY_ARN:-}"
HMAC_WORKER_EXPECTED_HASHES="${HMAC_WORKER_EXPECTED_HASHES:-}"
HMAC_WORKER_MODE="${HMAC_WORKER_MODE:-candidate}"
HMAC_CLEANUP_DOMAIN="${HMAC_CLEANUP_DOMAIN:-}"
HMAC_WORKER_ADVANCE_STAGE="${HMAC_WORKER_ADVANCE_STAGE:-1}"
GO=0
[[ "${1:-}" == "--go" ]] && GO=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PREFLIGHT_PY="${PREFLIGHT_PY:-$ROOT/.venv/bin/python}"

if [[ "$GO" == "1" ]]; then
  [[ "${TEAMAGENT_HMAC_DEPLOY_FROM_TERRAFORM:-}" == "1" \
    && "${TEAMAGENT_APPLY_ATTEMPT_ID:-}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ \
    && -n "${TEAMAGENT_SAVED_PLAN_PATH:-}" \
    && -f "$TEAMAGENT_SAVED_PLAN_PATH" ]] || {
    echo "ERROR: worker mutation requires the same one-use saved Terraform plan/shared lock" >&2
    exit 2
  }
fi

[[ -n "$HMAC_PREFLIGHT_MANIFEST" && -f "$HMAC_PREFLIGHT_MANIFEST" ]] || {
  echo "ERROR: HMAC_PREFLIGHT_MANIFEST（secret-free reviewed JSON）が必須" >&2
  exit 2
}
[[ -n "$HMAC_ROLLOUT_CONTROL" && -f "$HMAC_ROLLOUT_CONTROL" ]] || {
  echo "ERROR: HMAC_ROLLOUT_CONTROL（secret-free live control JSON）が必須" >&2
  exit 2
}
[[ -n "$HMAC_WORKER_ROLLBACK_ARTIFACT" && -f "$HMAC_WORKER_ROLLBACK_ARTIFACT" ]] || {
  echo "ERROR: HMAC_WORKER_ROLLBACK_ARTIFACT（prebuilt rollback）が必須" >&2
  exit 2
}
[[ -n "$HMAC_WORKER_PROVENANCE_RECEIPT" && -f "$HMAC_WORKER_PROVENANCE_RECEIPT" \
  && -n "$HMAC_WORKER_PROVENANCE_SIGNATURE" && -f "$HMAC_WORKER_PROVENANCE_SIGNATURE" ]] || {
  echo "ERROR: clean-origin signed candidate worker provenance is required" >&2
  exit 2
}
[[ -n "$HMAC_WORKER_ROLLBACK_PROVENANCE_RECEIPT" \
  && -f "$HMAC_WORKER_ROLLBACK_PROVENANCE_RECEIPT" \
  && -n "$HMAC_WORKER_ROLLBACK_PROVENANCE_SIGNATURE" \
  && -f "$HMAC_WORKER_ROLLBACK_PROVENANCE_SIGNATURE" ]] || {
  echo "ERROR: clean-origin signed rollback worker provenance is required" >&2
  exit 2
}
[[ "$HMAC_WORKER_PROVENANCE_KEY_ARN" =~ ^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-f-]{36}$ ]] || {
  echo "ERROR: exact worker provenance KMS key ARN is required" >&2
  exit 2
}
case "$HMAC_WORKER_MODE" in
  candidate)
    [[ -n "$HMAC_WORKER_ENV" && -f "$HMAC_WORKER_ENV" ]] || {
      echo "ERROR: HMAC_WORKER_ENV（secret-free rendered hmac.env）が必須" >&2
      exit 2
    }
    [[ -n "$HMAC_WORKER_ARTIFACT" && -f "$HMAC_WORKER_ARTIFACT" ]] || {
      echo "ERROR: candidate mode requires a prebuilt signed worker archive" >&2
      exit 2
    }
    SELECTED_WORKER_ARTIFACT="$HMAC_WORKER_ARTIFACT"
    SELECTED_PROVENANCE_RECEIPT="$HMAC_WORKER_PROVENANCE_RECEIPT"
    SELECTED_PROVENANCE_SIGNATURE="$HMAC_WORKER_PROVENANCE_SIGNATURE"
    SELECTED_WORKER_ENV="$HMAC_WORKER_ENV"
    ;;
  cleanup)
    [[ -n "$HMAC_WORKER_ENV" && -f "$HMAC_WORKER_ENV" ]] || {
      echo "ERROR: cleanup mode requires the exact primary-only HMAC_WORKER_ENV" >&2
      exit 2
    }
    [[ -n "$HMAC_WORKER_ARTIFACT" && -f "$HMAC_WORKER_ARTIFACT" ]] || {
      echo "ERROR: cleanup mode requires the exact prepared worker archive" >&2
      exit 2
    }
    SELECTED_WORKER_ARTIFACT="$HMAC_WORKER_ARTIFACT"
    [[ "$HMAC_WORKER_ADVANCE_STAGE" == "0" ]] || {
      echo "ERROR: cleanup mode may not advance the issuer-cutover ledger" >&2
      exit 2
    }
    SELECTED_PROVENANCE_RECEIPT="$HMAC_WORKER_PROVENANCE_RECEIPT"
    SELECTED_PROVENANCE_SIGNATURE="$HMAC_WORKER_PROVENANCE_SIGNATURE"
    SELECTED_WORKER_ENV="$HMAC_WORKER_ENV"
    ;;
  rollback)
    [[ -n "$HMAC_WORKER_ROLLBACK_ENV" && -f "$HMAC_WORKER_ROLLBACK_ENV" ]] || {
      echo "ERROR: rollback mode requires the exact prebuilt HMAC_WORKER_ROLLBACK_ENV" >&2
      exit 2
    }
    [[ "$HMAC_WORKER_ADVANCE_STAGE" == "0" ]] || {
      echo "ERROR: rollback mode may not advance the rollout ledger" >&2
      exit 2
    }
    SELECTED_WORKER_ARTIFACT="$HMAC_WORKER_ROLLBACK_ARTIFACT"
    SELECTED_PROVENANCE_RECEIPT="$HMAC_WORKER_ROLLBACK_PROVENANCE_RECEIPT"
    SELECTED_PROVENANCE_SIGNATURE="$HMAC_WORKER_ROLLBACK_PROVENANCE_SIGNATURE"
    SELECTED_WORKER_ENV="$HMAC_WORKER_ROLLBACK_ENV"
    ;;
  *)
    echo "ERROR: HMAC_WORKER_MODE must be candidate, rollback, or cleanup" >&2
    exit 2
    ;;
esac
[[ "$HMAC_WORKER_ADVANCE_STAGE" == "0" || "$HMAC_WORKER_ADVANCE_STAGE" == "1" ]] || {
  echo "ERROR: HMAC_WORKER_ADVANCE_STAGE must be 0 or 1" >&2
  exit 2
}
[[ -x "$PREFLIGHT_PY" ]] || { echo "ERROR: preflight Python が実行できない" >&2; exit 2; }
if [[ "$GO" == "1" ]]; then
"$PREFLIGHT_PY" scripts/terraform_hmac_payload.py verify-worker-bindings
EXPECTED_WORKER_BINDING_KEYS='[
  "base_environment",
  "candidate_artifact",
  "candidate_env",
  "candidate_receipt",
  "candidate_signature",
  "deploy_overrides",
  "deploy_script",
  "provenance_verifier",
  "reviewed_manifest",
  "rollback_artifact",
  "rollback_env",
  "rollback_receipt",
  "rollback_signature",
  "rollout_control"
]'
if ! jq -e --argjson expected "$EXPECTED_WORKER_BINDING_KEYS" \
  'type == "object"
   and keys == ($expected | sort)
   and all(.[]; type == "string" and test("^[a-f0-9]{64}$"))' \
  <<<"$HMAC_WORKER_EXPECTED_HASHES" >/dev/null; then
  echo "ERROR: exact saved-plan worker file bindings are required" >&2
  exit 2
fi
verify_bound_worker_file() {
  local name="$1" path="$2" expected actual
  [[ -n "$path" && -f "$path" ]] || return 1
  expected="$(jq -er --arg name "$name" '.[$name]' <<<"$HMAC_WORKER_EXPECTED_HASHES")"
  actual="$(sha256sum "$path" | awk '{print $1}')"
  [[ "$actual" == "$expected" ]]
}
verify_bound_worker_file base_environment "$ROOT/.env.production" \
  && verify_bound_worker_file candidate_artifact "$HMAC_WORKER_ARTIFACT" \
  && verify_bound_worker_file candidate_env "$HMAC_WORKER_ENV" \
  && verify_bound_worker_file candidate_receipt "$HMAC_WORKER_PROVENANCE_RECEIPT" \
  && verify_bound_worker_file candidate_signature "$HMAC_WORKER_PROVENANCE_SIGNATURE" \
  && verify_bound_worker_file deploy_overrides "$ROOT/infra/deploy/ec2.overrides.env" \
  && verify_bound_worker_file deploy_script "$ROOT/scripts/deploy_to_ec2.sh" \
  && verify_bound_worker_file provenance_verifier \
    "$ROOT/scripts/verify_worker_bundle_provenance.py" \
  && verify_bound_worker_file reviewed_manifest "$HMAC_PREFLIGHT_MANIFEST" \
  && verify_bound_worker_file rollback_artifact "$HMAC_WORKER_ROLLBACK_ARTIFACT" \
  && verify_bound_worker_file rollback_env "$HMAC_WORKER_ROLLBACK_ENV" \
  && verify_bound_worker_file rollback_receipt \
    "$HMAC_WORKER_ROLLBACK_PROVENANCE_RECEIPT" \
  && verify_bound_worker_file rollback_signature \
    "$HMAC_WORKER_ROLLBACK_PROVENANCE_SIGNATURE" \
  && verify_bound_worker_file rollout_control "$HMAC_ROLLOUT_CONTROL" || {
  echo "ERROR: a worker input differs from the one-use saved plan" >&2
  exit 2
}
fi
"$PREFLIGHT_PY" scripts/preflight_hmac_rotation.py \
  --manifest "$HMAC_PREFLIGHT_MANIFEST" \
  --refresh-manifest-now \
  --worker-env "$SELECTED_WORKER_ENV"
cp "$SELECTED_WORKER_ENV" "$WORK/hmac.env"

echo "== 1. コード tarball（review済みartifactのみ）=="
cp "$SELECTED_WORKER_ARTIFACT" "$WORK/teamagent-bot.tar.gz"
"$PREFLIGHT_PY" scripts/verify_worker_bundle_provenance.py \
  --artifact "$WORK/teamagent-bot.tar.gz" \
  --receipt "$SELECTED_PROVENANCE_RECEIPT" \
  --signature "$SELECTED_PROVENANCE_SIGNATURE" \
  --key-arn "$HMAC_WORKER_PROVENANCE_KEY_ARN"
"$PREFLIGHT_PY" scripts/verify_worker_bundle_provenance.py \
  --artifact "$HMAC_WORKER_ROLLBACK_ARTIFACT" \
  --receipt "$HMAC_WORKER_ROLLBACK_PROVENANCE_RECEIPT" \
  --signature "$HMAC_WORKER_ROLLBACK_PROVENANCE_SIGNATURE" \
  --key-arn "$HMAC_WORKER_PROVENANCE_KEY_ARN"
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
if grep -Eiq 'xoxb-|xapp-|AKIA[0-9A-Z]{15}|-----BEGIN|PRIVATE KEY' \
  "$WORK/teamagent.env.base" "$WORK/hmac.env"; then
  echo "ERROR: env.base に秘密らしき実値を検出。中止（Secrets Manager 経由にすること）。"
  exit 1
fi
if grep -Eq \
  '^[[:space:]]*(export[[:space:]]+)?(TEAMAGENT_HMAC_REQUIRED_DOMAINS|MAIL_ACTION_HMAC_(PREVIOUS_|LEGACY_WORKER_)?SECRET|REPORT_LINK_HMAC_(PREVIOUS_)?SECRET)=' \
  "$WORK/teamagent.env.base"; then
  echo "ERROR: env.base に禁止された HMAC runtime 値/required domains を検出。中止。" >&2
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

echo "== 3c. live HMAC gate（upload直前）=="
"$PREFLIGHT_PY" scripts/hmac_rollout_gate.py \
  --manifest "$HMAC_PREFLIGHT_MANIFEST" \
  --refresh-manifest-now \
  --control "$HMAC_ROLLOUT_CONTROL" \
  --action pre-worker-upload \
  --mode "$HMAC_WORKER_MODE" \
  --worker-artifact "$WORK/teamagent-bot.tar.gz" \
  --worker-rollback-artifact "$HMAC_WORKER_ROLLBACK_ARTIFACT" \
  --worker-env "$SELECTED_WORKER_ENV" \
  --worker-rollback-env "$HMAC_WORKER_ROLLBACK_ENV"

echo "== 4. S3 アップロード =="
ARTIFACT_DIGEST="$(sha256sum "$WORK/teamagent-bot.tar.gz" | awk '{print $1}')"
BASE_ENV_DIGEST="$(sha256sum "$WORK/teamagent.env.base" | awk '{print $1}')"
HMAC_ENV_DIGEST="$(sha256sum "$WORK/hmac.env" | awk '{print $1}')"
RELEASE_DIGEST="$(
  printf '%s\n%s\n%s\n' "$ARTIFACT_DIGEST" "$BASE_ENV_DIGEST" "$HMAC_ENV_DIGEST" \
    | sha256sum | awk '{print $1}'
)"
RELEASE_KEY_PREFIX="deploy/releases/$RELEASE_DIGEST/${TEAMAGENT_APPLY_ATTEMPT_ID:-dry-run}"
ARTIFACT_KEY="$RELEASE_KEY_PREFIX/teamagent-bot.tar.gz"
BASE_ENV_KEY="$RELEASE_KEY_PREFIX/teamagent.env.base"
HMAC_ENV_KEY="$RELEASE_KEY_PREFIX/teamagent.hmac.env"
ARTIFACT_VERSION="$(aws s3api put-object --region "$REGION" --bucket "$BUCKET" \
  --key "$ARTIFACT_KEY" --body "$WORK/teamagent-bot.tar.gz" --query VersionId --output text)"
BASE_ENV_VERSION="$(aws s3api put-object --region "$REGION" --bucket "$BUCKET" \
  --key "$BASE_ENV_KEY" --body "$WORK/teamagent.env.base" --query VersionId --output text)"
HMAC_ENV_VERSION="$(aws s3api put-object --region "$REGION" --bucket "$BUCKET" \
  --key "$HMAC_ENV_KEY" --body "$WORK/hmac.env" --query VersionId --output text)"
for version in "$ARTIFACT_VERSION" "$BASE_ENV_VERSION" "$HMAC_ENV_VERSION"; do
  [[ -n "$version" && "$version" != "None" && "$version" != "null" ]] || {
    echo "ERROR: versioned worker release upload did not return an S3 VersionId" >&2
    exit 1
  }
done

echo "== 5. SSM でリモート展開（venv/pip + scraper npm ci + Chrome 解決 + systemd 起動）=="
REMOTE=$(cat <<'RSH'
set -euo pipefail
BUCKET="__BUCKET__"
ARTIFACT_KEY="__ARTIFACT_KEY__"
BASE_ENV_KEY="__BASE_ENV_KEY__"
HMAC_ENV_KEY="__HMAC_ENV_KEY__"
ARTIFACT_VERSION="__ARTIFACT_VERSION__"
BASE_ENV_VERSION="__BASE_ENV_VERSION__"
HMAC_ENV_VERSION="__HMAC_ENV_VERSION__"
RELEASE_DIGEST="__RELEASE_DIGEST__"
ARTIFACT_DIGEST="__ARTIFACT_DIGEST__"
BASE_ENV_DIGEST="__BASE_ENV_DIGEST__"
HMAC_ENV_DIGEST="__HMAC_ENV_DIGEST__"
RELEASE_ROOT=/opt/teamagent/releases
FINAL_RELEASE="$RELEASE_ROOT/$RELEASE_DIGEST"
STAGING_RELEASE="$RELEASE_ROOT/.staging-$RELEASE_DIGEST-$$"
mkdir -p "$RELEASE_ROOT"
[[ ! -e "$FINAL_RELEASE" ]] || exit 1
mkdir -p "$STAGING_RELEASE/app"
trap 'rm -rf -- "$STAGING_RELEASE"' EXIT
aws s3api get-object --bucket "$BUCKET" --key "$ARTIFACT_KEY" \
  --version-id "$ARTIFACT_VERSION" "$STAGING_RELEASE/app.tar.gz" >/dev/null
aws s3api get-object --bucket "$BUCKET" --key "$BASE_ENV_KEY" \
  --version-id "$BASE_ENV_VERSION" "$STAGING_RELEASE/teamagent.env.base" >/dev/null
aws s3api get-object --bucket "$BUCKET" --key "$HMAC_ENV_KEY" \
  --version-id "$HMAC_ENV_VERSION" "$STAGING_RELEASE/hmac.env" >/dev/null
[[ "$(sha256sum "$STAGING_RELEASE/app.tar.gz" | awk '{print $1}')" == "$ARTIFACT_DIGEST" ]]
[[ "$(sha256sum "$STAGING_RELEASE/teamagent.env.base" | awk '{print $1}')" == "$BASE_ENV_DIGEST" ]]
[[ "$(sha256sum "$STAGING_RELEASE/hmac.env" | awk '{print $1}')" == "$HMAC_ENV_DIGEST" ]]
chmod 0644 "$STAGING_RELEASE/hmac.env" "$STAGING_RELEASE/teamagent.env.base"
source "$STAGING_RELEASE/hmac.env"
[[ "$TEAMAGENT_HMAC_ARTIFACT_SHA256" == "$ARTIFACT_DIGEST" ]] || exit 1
tar xzf "$STAGING_RELEASE/app.tar.gz" -C "$STAGING_RELEASE/app"
[[ -f "$STAGING_RELEASE/app/scripts/worker_atomic_release_switch.sh" ]]
chmod 0755 "$STAGING_RELEASE/app/scripts/worker_atomic_release_switch.sh"
cd "$STAGING_RELEASE/app"
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
  sed -i '/^CHROMIUM_PATH=/d' "$STAGING_RELEASE/teamagent.env.base"
  echo "CHROMIUM_PATH=$PW_CHROME" >> "$STAGING_RELEASE/teamagent.env.base"
fi
rm -rf /opt/teamagent/piptmp
# OOM 安全網: swap 4GB（e5 ロード時のメモリ逼迫対策・永続）
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=4096 status=none
  chmod 600 /swapfile && mkswap /swapfile >/dev/null 2>&1 && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
printf '%s\n' "$ARTIFACT_DIGEST" > "$STAGING_RELEASE/.artifact-sha256"
printf '%s\n' "$RELEASE_DIGEST" > "$STAGING_RELEASE/.release-sha256"
# bot と connect_web の両方へ同じ reviewed HMAC keyring metadata を注入する。
# 既存インスタンスは user_data を再実行しないため、デプロイ毎に unit を冪等に設置する。
cat > "$STAGING_RELEASE/teamagent-bot.service" <<'BOTSVC'
[Unit]
Description=TeamAgent Slack Bot (Socket Mode)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=/opt/teamagent/current/app
EnvironmentFile=-/opt/teamagent/restart.env
ExecStart=/bin/bash -lc 'set -a; source /opt/teamagent/current/teamagent.env.base; source /opt/teamagent/current/hmac.env; source scripts/load_secrets.sh MAIL_ACTION,REPORT_LINK || exit $?; ./.venv/bin/python scripts/check_hmac_runtime_state.py --domains MAIL_ACTION,REPORT_LINK || exit $?; set +a; exec ./.venv/bin/python -m teamagent.runtime.slack_bot'
ExecStartPost=/bin/bash -lc 'set -a; source /opt/teamagent/current/teamagent.env.base; source /opt/teamagent/current/hmac.env; source /opt/teamagent/restart.env; source /opt/teamagent/current/app/scripts/load_secrets.sh MAIL_ACTION,REPORT_LINK || exit $?; export TEAMAGENT_HMAC_SERVICE=bot TEAMAGENT_HMAC_SERVICE_HEALTH=1 TEAMAGENT_HMAC_MAIN_PID=$MAINPID; kill -0 "$MAINPID"; /opt/teamagent/current/app/.venv/bin/python /opt/teamagent/current/app/scripts/check_hmac_runtime_state.py --domains MAIL_ACTION,REPORT_LINK --worker-attestation'
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
[Install]
WantedBy=multi-user.target
BOTSVC
# connect_web(OAuth コールバック /oauth2/callback) を常駐させる unit を冪等に設置。
# 既存インスタンスは user_data を再実行しないため、デプロイ毎にここで ensure する。
# これが無いと ALB ターゲットが永久 UNHEALTHY＝連携リンクは出ても完了しない。
cat > "$STAGING_RELEASE/teamagent-connect.service" <<'CONNSVC'
[Unit]
Description=TeamAgent connect_web (OAuth callback receiver)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5
[Service]
Type=simple
WorkingDirectory=/opt/teamagent/current/app
EnvironmentFile=-/opt/teamagent/restart.env
ExecStart=/bin/bash -lc 'set -a; source /opt/teamagent/current/teamagent.env.base; source /opt/teamagent/current/hmac.env; source scripts/load_secrets.sh REPORT_LINK || exit $?; ./.venv/bin/python scripts/check_hmac_runtime_state.py --domains REPORT_LINK || exit $?; set +a; exec ./.venv/bin/python -m teamagent.connect_web'
ExecStartPost=/bin/bash -lc 'for _attempt in $(seq 1 60); do curl -fsS http://127.0.0.1:8788/healthz >/dev/null && ss -H -ltnp "sport = :8788" | grep -F "pid=$MAINPID," >/dev/null && break; sleep 1; done; set -a; source /opt/teamagent/current/teamagent.env.base; source /opt/teamagent/current/hmac.env; source /opt/teamagent/restart.env; source /opt/teamagent/current/app/scripts/load_secrets.sh MAIL_ACTION,REPORT_LINK || exit $?; export TEAMAGENT_HMAC_SERVICE=connect TEAMAGENT_HMAC_SERVICE_HEALTH=1 TEAMAGENT_HMAC_MAIN_PID=$MAINPID; curl -fsS http://127.0.0.1:8788/healthz >/dev/null; ss -H -ltnp "sport = :8788" | grep -F "pid=$MAINPID," >/dev/null; /opt/teamagent/current/app/.venv/bin/python /opt/teamagent/current/app/scripts/check_hmac_runtime_state.py --domains MAIL_ACTION,REPORT_LINK --worker-attestation'
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
[Install]
WantedBy=multi-user.target
CONNSVC
chmod 0644 "$STAGING_RELEASE/teamagent-bot.service" \
  "$STAGING_RELEASE/teamagent-connect.service"
mv -- "$STAGING_RELEASE" "$FINAL_RELEASE"
trap - EXIT
(
  set -a
  source "$FINAL_RELEASE/teamagent.env.base"
  source "$FINAL_RELEASE/hmac.env"
  source "$FINAL_RELEASE/app/scripts/load_secrets.sh" MAIL_ACTION,REPORT_LINK
  "$FINAL_RELEASE/app/.venv/bin/python" "$FINAL_RELEASE/app/scripts/check_hmac_runtime_state.py" \
    --domains MAIL_ACTION,REPORT_LINK --worker-attestation
)
echo "worker_readiness=true"
RSH
)
REMOTE="${REMOTE/__BUCKET__/$BUCKET}"
REMOTE="${REMOTE/__ARTIFACT_KEY__/$ARTIFACT_KEY}"
REMOTE="${REMOTE/__BASE_ENV_KEY__/$BASE_ENV_KEY}"
REMOTE="${REMOTE/__HMAC_ENV_KEY__/$HMAC_ENV_KEY}"
REMOTE="${REMOTE/__ARTIFACT_VERSION__/$ARTIFACT_VERSION}"
REMOTE="${REMOTE/__BASE_ENV_VERSION__/$BASE_ENV_VERSION}"
REMOTE="${REMOTE/__HMAC_ENV_VERSION__/$HMAC_ENV_VERSION}"
REMOTE="${REMOTE/__RELEASE_DIGEST__/$RELEASE_DIGEST}"
REMOTE="${REMOTE/__ARTIFACT_DIGEST__/$ARTIFACT_DIGEST}"
REMOTE="${REMOTE/__BASE_ENV_DIGEST__/$BASE_ENV_DIGEST}"
REMOTE="${REMOTE/__HMAC_ENV_DIGEST__/$HMAC_ENV_DIGEST}"
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
[[ "${ST:-}" == "Success" ]] || {
  echo "ERROR: worker prepare/readiness failed (status=${ST:-unknown}); remote output is not echoed" >&2
  exit 1
}
echo "   worker_prepare=true readiness=true output_redacted=true"

if [[ "$HMAC_WORKER_ADVANCE_STAGE" == "1" ]]; then
  "$PREFLIGHT_PY" scripts/hmac_rollout_gate.py \
    --manifest "$HMAC_PREFLIGHT_MANIFEST" \
    --refresh-manifest-now \
    --control "$HMAC_ROLLOUT_CONTROL" \
    --action worker-verified \
    --worker-rollback-artifact "$HMAC_WORKER_ROLLBACK_ARTIFACT"
fi
PRE_RESTART_RESULT="$("$PREFLIGHT_PY" scripts/hmac_rollout_gate.py \
  --manifest "$HMAC_PREFLIGHT_MANIFEST" \
  --refresh-manifest-now \
  --control "$HMAC_ROLLOUT_CONTROL" \
  --action pre-restart \
  --mode "$HMAC_WORKER_MODE" \
  --worker-rollback-artifact "$HMAC_WORKER_ROLLBACK_ARTIFACT")"
RESTART_NONCE="$(printf '%s' "$PRE_RESTART_RESULT" | "$PREFLIGHT_PY" -c \
  'import json,sys; print(json.load(sys.stdin)["restart_nonce"])')"
[[ "$RESTART_NONCE" =~ ^[a-f0-9]{64}$ ]] || {
  echo "ERROR: pre-restart did not return a bounded one-use nonce" >&2
  exit 1
}

RESTART_REMOTE=$(cat <<'RSH'
set -euo pipefail
RELEASE_DIGEST="__RELEASE_DIGEST__"
RESTART_NONCE="__RESTART_NONCE__"
TRANSACTION_ID="__TRANSACTION_ID__"
FINAL_RELEASE="/opt/teamagent/releases/$RELEASE_DIGEST"
[[ -d "$FINAL_RELEASE/app" ]]
[[ "$(cat "$FINAL_RELEASE/.release-sha256")" == "$RELEASE_DIGEST" ]]
bash "$FINAL_RELEASE/app/scripts/worker_atomic_release_switch.sh" \
  switch "$TRANSACTION_ID" "$FINAL_RELEASE" "$RELEASE_DIGEST" "$RESTART_NONCE"
RSH
)
RESTART_REMOTE="${RESTART_REMOTE/__RELEASE_DIGEST__/$RELEASE_DIGEST}"
RESTART_REMOTE="${RESTART_REMOTE/__RESTART_NONCE__/$RESTART_NONCE}"
RESTART_REMOTE="${RESTART_REMOTE/__TRANSACTION_ID__/$TEAMAGENT_APPLY_ATTEMPT_ID}"
RESTART_B64="$(printf '%s' "$RESTART_REMOTE" | base64 | tr -d '\n')"
RESTART_CID="$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript --comment "TeamAgent HMAC-gated restart" \
  --parameters commands="echo $RESTART_B64 | base64 -d | bash" \
  --query Command.CommandId --output text 2>/dev/null || true)"
[[ -n "${RESTART_CID:-}" && "$RESTART_CID" != "None" ]] || {
  echo "ERROR: gated restart command was not accepted" >&2
  exit 1
}
RESTART_STATUS=Pending
for i in $(seq 1 30); do
  sleep 10
  RESTART_STATUS=$(aws ssm get-command-invocation --region "$REGION" \
    --command-id "$RESTART_CID" --instance-id "$INSTANCE_ID" \
    --query Status --output text 2>/dev/null || echo Pending)
  [[ "$RESTART_STATUS" == "Success" || "$RESTART_STATUS" == "Failed" || "$RESTART_STATUS" == "TimedOut" ]] && break
done
[[ "$RESTART_STATUS" == "Success" ]] || {
  echo "ERROR: worker restart failed (status=$RESTART_STATUS); remote output is not echoed" >&2
  exit 1
}

run_release_transaction_action() {
  local action="$1" action_remote action_b64 action_cid action_status i
  action_remote="bash /opt/teamagent/releases/$RELEASE_DIGEST/app/scripts/worker_atomic_release_switch.sh $action $TEAMAGENT_APPLY_ATTEMPT_ID"
  action_b64="$(printf '%s' "$action_remote" | base64 | tr -d '\n')"
  action_cid="$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE_ID" \
    --document-name AWS-RunShellScript --comment "TeamAgent worker release $action" \
    --parameters commands="echo $action_b64 | base64 -d | bash" \
    --query Command.CommandId --output text 2>/dev/null || true)"
  [[ -n "$action_cid" && "$action_cid" != "None" ]] || return 1
  action_status=Pending
  for i in $(seq 1 30); do
    sleep 10
    action_status="$(aws ssm get-command-invocation --region "$REGION" \
      --command-id "$action_cid" --instance-id "$INSTANCE_ID" \
      --query Status --output text 2>/dev/null || echo Pending)"
    [[ "$action_status" == "Success" || "$action_status" == "Failed" \
      || "$action_status" == "TimedOut" ]] && break
  done
  [[ "$action_status" == "Success" ]]
}

if ! "$PREFLIGHT_PY" scripts/hmac_rollout_gate.py \
  --manifest "$HMAC_PREFLIGHT_MANIFEST" \
  --refresh-manifest-now \
  --control "$HMAC_ROLLOUT_CONTROL" \
  --action post-restart \
  --mode "$HMAC_WORKER_MODE"; then
  if ! run_release_transaction_action rollback; then
    echo "ERROR: post-restart failed and automatic rollback needs reconciliation" >&2
    exit 70
  fi
  echo "ERROR: post-restart attestation failed; prior release restored" >&2
  exit 1
fi
if ! run_release_transaction_action commit; then
  if ! run_release_transaction_action rollback; then
    echo "ERROR: release commit and automatic rollback need reconciliation" >&2
    exit 70
  fi
  echo "ERROR: release commit failed; prior release restored" >&2
  exit 1
fi
echo "   worker_restart=true services_active=true port_8788_listening=true fresh_attestation=true output_redacted=true"
echo "== 完了 == Slack で『@TeamAgent VSEO分析 新宿 ランチ』を確認。問題あれば runbook のロールバックへ。"
