#!/usr/bin/env bash
# ============================================================
# TeamAgent — AWS Secrets Manager から本番 secret を取得して
# 環境変数に展開するスクリプト。
#
# Usage:
#   set -a; source .env.production; set +a
#   source scripts/load_secrets.sh
#   python -m teamagent.runtime.slack_bot
#
# 前提：
#   - aws CLI と適切な IAM 認証（プロファイル or Role）
#   - .env.production が事前に読み込まれていること
#     （RDS_HOST / *_SECRET_NAME 等が定義済み）
# ============================================================

set -u

_log() { echo "[load_secrets] $*" >&2; }

_get_secret() {
    local name="$1"
    aws secretsmanager get-secret-value \
        --secret-id "$name" \
        --region "${AWS_REGION:-ap-northeast-1}" \
        --query SecretString \
        --output text 2>/dev/null
}

_require_env() {
    local var="$1"
    if [[ -z "${!var:-}" ]]; then
        _log "ERROR: 環境変数 $var が未設定です（.env.production を source してください）"
        return 1
    fi
}

_load() {
    _require_env DB_PASSWORD_SECRET_NAME || return 1
    _require_env SLACK_BOT_TOKEN_SECRET_NAME || return 1
    _require_env SLACK_APP_TOKEN_SECRET_NAME || return 1
    _require_env RDS_HOST || return 1

    local db_pass
    db_pass="$(_get_secret "$DB_PASSWORD_SECRET_NAME")"
    if [[ -z "$db_pass" ]]; then
        _log "ERROR: $DB_PASSWORD_SECRET_NAME から DB パスワードを取得できませんでした"
        return 1
    fi

    # DATABASE_URL を組み立て（SSL を必ず有効化）
    # password に @ や : が含まれていても安全なように urlencode
    local enc_pass
    enc_pass="$(python3 -c 'import urllib.parse, sys; print(urllib.parse.quote(sys.stdin.read().strip(), safe=""))' <<<"$db_pass")"
    export DATABASE_URL="postgresql://${RDS_USER:-teamagent}:${enc_pass}@${RDS_HOST}:${RDS_PORT:-5432}/${RDS_DBNAME:-teamagent}?sslmode=${RDS_SSL_MODE:-require}"
    _log "OK: DATABASE_URL を組み立て（host=${RDS_HOST}, ssl=${RDS_SSL_MODE:-require}）"

    export SLACK_BOT_TOKEN="$(_get_secret "$SLACK_BOT_TOKEN_SECRET_NAME")"
    export SLACK_APP_TOKEN="$(_get_secret "$SLACK_APP_TOKEN_SECRET_NAME")"

    if [[ -z "$SLACK_BOT_TOKEN" || -z "$SLACK_APP_TOKEN" ]]; then
        _log "ERROR: Slack トークン取得失敗"
        return 1
    fi
    _log "OK: Slack tokens loaded (bot=${SLACK_BOT_TOKEN:0:8}…, app=${SLACK_APP_TOKEN:0:8}…)"

    # Sentry DSN（任意 — 未投入なら skip）
    if [[ -n "${SENTRY_DSN_SECRET_NAME:-}" ]]; then
        local dsn
        dsn="$(_get_secret "$SENTRY_DSN_SECRET_NAME" 2>/dev/null || true)"
        if [[ -n "$dsn" ]]; then
            export SENTRY_DSN="$dsn"
            _log "OK: SENTRY_DSN loaded"
        else
            _log "INFO: Sentry DSN は未投入（skip）"
        fi
    fi
}

_load
