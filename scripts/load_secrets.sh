#!/usr/bin/env bash
# ============================================================
# TeamAgent — AWS Secrets Manager から本番 secret を取得して
# 環境変数に展開するスクリプト。
#
# Usage (本番 EC2 / Lambda):
#   set -a; source .env.production; set +a
#   source scripts/load_secrets.sh
#   python -m teamagent.runtime.slack_bot
#
# Usage (ローカル Mac、SSM tunnel 経由):
#   # 別 Terminal で tunnel 起動
#   set -a; source .env.local; set +a
#   source scripts/load_secrets.sh
#   python -m teamagent.runtime.slack_bot
#
# 前提：
#   - aws CLI と適切な IAM 認証（プロファイル or Role）
#   - .env.* が事前に読み込まれていること（RDS_HOST / *_SECRET_NAME 等）
#
# 異常検知（自動 fail）：
#   - プレースホルダ（__XXX__ / <xxx> 形式）が残っている
#   - 必須 env が未設定
#   - Secrets Manager から空文字が返る
# ============================================================

set -u

_log() { echo "[load_secrets] $*" >&2; }

# プレースホルダ検知（テンプレ値が残っているか）
_check_placeholder() {
    local var="$1"
    local val="${!var:-}"
    if [[ "$val" =~ ^__.+__$ ]] || [[ "$val" =~ \<.+\>$ ]]; then
        _log "ERROR: $var にプレースホルダ '$val' が残っています。"
        _log "       .env.* ファイルを編集して実値に置換してください。"
        return 1
    fi
    return 0
}

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

    # プレースホルダ残り検知
    _check_placeholder RDS_HOST || return 1

    # tunnel モード / 本番モードを構造化ログに記載
    if [[ "$RDS_HOST" == "localhost" || "$RDS_HOST" == "127.0.0.1" ]]; then
        _log "MODE: local (SSM tunnel 経由想定 / RDS_HOST=$RDS_HOST:${RDS_PORT:-5432})"
        _log "      別 Terminal で aws ssm start-session が起動済みであること"
    else
        _log "MODE: direct (本番 EC2/Lambda 想定 / RDS_HOST=$RDS_HOST:${RDS_PORT:-5432})"
    fi

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

    # Google OAuth (Drive + Gmail) — JSON 形式の単一 secret から 3 値を展開
    # secret-string は {"client_id":..., "client_secret":..., "refresh_token":...} 形式
    if [[ -n "${GOOGLE_OAUTH_SECRET_NAME:-}" ]]; then
        local gjson
        gjson="$(_get_secret "$GOOGLE_OAUTH_SECRET_NAME" 2>/dev/null || true)"
        if [[ -n "$gjson" ]]; then
            # python3 で JSON を parse して 3 値を export
            # set -u 配下で空文字代入を避けるため if 内で eval
            local gvals
            gvals="$(python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
print(f\"export GOOGLE_CLIENT_ID='{d['client_id']}'\")
print(f\"export GOOGLE_CLIENT_SECRET='{d['client_secret']}'\")
print(f\"export GOOGLE_OAUTH_REFRESH_TOKEN='{d['refresh_token']}'\")
" <<<"$gjson" 2>/dev/null || true)"
            if [[ -n "$gvals" ]]; then
                eval "$gvals"
                _log "OK: Google OAuth loaded (client_id=${GOOGLE_CLIENT_ID:0:20}…, refresh_token=${GOOGLE_OAUTH_REFRESH_TOKEN:0:8}…)"
            else
                _log "WARN: Google OAuth secret は取得できたが JSON parse 失敗"
            fi
        else
            _log "INFO: Google OAuth secret は未投入（skip）"
        fi
    fi
}

_load
