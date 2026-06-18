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
    # タイムアウト必須: Secrets Manager エンドポイント不達時に **無限ハング** すると
    # ExecStart(source load_secrets.sh) ごと固まり、起動が永久に完了せず healthz=000 に
    # なる（2026-06-18 connect-web インシデントの真因）。connect/read を短く切って fail-fast。
    aws secretsmanager get-secret-value \
        --secret-id "$name" \
        --region "${AWS_REGION:-ap-northeast-1}" \
        --query SecretString \
        --output text \
        --cli-connect-timeout "${AWS_SM_CONNECT_TIMEOUT:-5}" \
        --cli-read-timeout "${AWS_SM_READ_TIMEOUT:-10}" \
        2>/dev/null
}

_require_env() {
    local var="$1"
    if [[ -z "${!var:-}" ]]; then
        _log "ERROR: 環境変数 $var が未設定です（.env.production を source してください）"
        return 1
    fi
}

_load() {
    # env 優先フォールバック: 既に env に値があれば Secrets Manager を引かない。これにより
    # SM が一時不達でも、運用側が DATABASE_URL / SLACK_* を env 注入しておけば起動できる
    # （SM 障害で起動ごと落ちるのを防ぐ・2026-06-18 インシデント対策）。fetch が要る secret
    # 名のみ _require_env する。

    # --- DATABASE_URL ---
    if [[ -n "${DATABASE_URL:-}" ]]; then
        _log "INFO: DATABASE_URL は env 既設（Secrets Manager fetch をスキップ）"
    else
        _require_env DB_PASSWORD_SECRET_NAME || return 1
        _require_env RDS_HOST || return 1
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
    fi

    # --- Slack tokens（未設定のときだけ SM から取得）---
    if [[ -z "${SLACK_BOT_TOKEN:-}" ]]; then
        _require_env SLACK_BOT_TOKEN_SECRET_NAME || return 1
        export SLACK_BOT_TOKEN="$(_get_secret "$SLACK_BOT_TOKEN_SECRET_NAME")"
    fi
    if [[ -z "${SLACK_APP_TOKEN:-}" ]]; then
        _require_env SLACK_APP_TOKEN_SECRET_NAME || return 1
        export SLACK_APP_TOKEN="$(_get_secret "$SLACK_APP_TOKEN_SECRET_NAME")"
    fi
    if [[ -z "${SLACK_BOT_TOKEN:-}" || -z "${SLACK_APP_TOKEN:-}" ]]; then
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

    # Connect-web 専用 Google OAuth (Web型クライアント) の client_secret。
    # CONNECT_GOOGLE_CLIENT_ID は env(teamagent.env.base)に平文、client_secret のみ
    # Secrets Manager。secret-string は client_secret の生文字列（JSONではない）。
    # 上の GOOGLE_* (Desktop型/共有サービス認証) を connect の認可フローで使うと redirect
    # 登録先(Web型)と client 不一致 → callback 500 になるため、connect 専用に分離する。
    # google_oauth_flow.connect_client_id_secret() が CONNECT_GOOGLE_* を優先する。
    if [[ -n "${CONNECT_GOOGLE_SECRET_NAME:-}" && -z "${CONNECT_GOOGLE_CLIENT_SECRET:-}" ]]; then
        local csec
        csec="$(_get_secret "$CONNECT_GOOGLE_SECRET_NAME" 2>/dev/null || true)"
        if [[ -n "$csec" ]]; then
            export CONNECT_GOOGLE_CLIENT_SECRET="$csec"
            _log "OK: CONNECT_GOOGLE_CLIENT_SECRET loaded (${CONNECT_GOOGLE_CLIENT_SECRET:0:8}…)"
        else
            _log "WARN: CONNECT_GOOGLE_SECRET_NAME 設定済だが取得失敗（connect の Google 連携が client 不一致になる可能性）"
        fi
    fi

    # CSRF state 署名鍵（connect-web の make_state/verify_state が必須）。未設定だと
    # /oauth2/callback が verify_state で ValueError → **生 500**（store の try/except の外で
    # 起きるため connect_callback_store_failed ログにも出ない）。OAUTH_STATE_SECRET_NAME が
    # あれば Secrets Manager から OAUTH_STATE_SECRET に展開（生文字列）。既に env で明示設定済なら
    # 上書きしない。署名鍵なので値は **一切ログに出さない**（長さのみ）。
    if [[ -n "${OAUTH_STATE_SECRET_NAME:-}" && -z "${OAUTH_STATE_SECRET:-}" ]]; then
        local ostate
        ostate="$(_get_secret "$OAUTH_STATE_SECRET_NAME" 2>/dev/null || true)"
        if [[ -n "$ostate" ]]; then
            export OAUTH_STATE_SECRET="$ostate"
            _log "OK: OAUTH_STATE_SECRET loaded (len=${#OAUTH_STATE_SECRET})"
        else
            _log "WARN: OAUTH_STATE_SECRET_NAME 設定済だが取得失敗（connect callback が 500 になる）"
        fi
    fi

    # Vertex SA JSON（Gemini 動画分析）— Secrets Manager から取得してファイル化。
    # EC2 向け。Mac は VERTEX_SA_SECRET_NAME 未設定なので skip し .env のローカルパスを使う。
    if [[ -n "${VERTEX_SA_SECRET_NAME:-}" ]]; then
        local sa_json sa_path
        sa_path="${VERTEX_SA_PATH:-/opt/teamagent/secrets/vertex-sa.json}"
        sa_json="$(_get_secret "$VERTEX_SA_SECRET_NAME" 2>/dev/null || true)"
        if [[ -n "$sa_json" ]]; then
            mkdir -p "$(dirname "$sa_path")"
            (umask 077; printf '%s' "$sa_json" >"$sa_path")  # 0600 で書き出し
            export GOOGLE_APPLICATION_CREDENTIALS="$sa_path"
            _log "OK: Vertex SA materialized → $sa_path"
        else
            _log "WARN: VERTEX_SA_SECRET_NAME 設定済だが取得失敗（Gemini 動画分析が動かない可能性）"
        fi
    fi
}

_load
