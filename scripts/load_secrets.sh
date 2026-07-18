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

_prepare_hmac_loader() {
    # Required domains are a fixed caller contract. A base env or parent process may not choose
    # them and may never preload runtime HMAC values. Clear all payload slots before deciding
    # whether to fail so a rejected source cannot leave attacker-controlled key material behind.
    local fixed_domains="${1:-}"
    local poisoned=0
    local runtime_var
    if declare -p TEAMAGENT_HMAC_REQUIRED_DOMAINS >/dev/null 2>&1; then
        poisoned=1
    fi
    for runtime_var in \
        MAIL_ACTION_HMAC_SECRET \
        MAIL_ACTION_HMAC_PREVIOUS_SECRET \
        MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET \
        REPORT_LINK_HMAC_SECRET \
        REPORT_LINK_HMAC_PREVIOUS_SECRET; do
        if declare -p "$runtime_var" >/dev/null 2>&1; then
            poisoned=1
        fi
        unset "$runtime_var"
    done
    unset TEAMAGENT_HMAC_REQUIRED_DOMAINS
    if [[ "$poisoned" -ne 0 ]]; then
        _log "ERROR: inherited HMAC runtime configuration is forbidden"
        return 1
    fi
    case "$fixed_domains" in
        "")
            ;;
        MAIL_ACTION|REPORT_LINK|MAIL_ACTION,REPORT_LINK)
            export TEAMAGENT_HMAC_REQUIRED_DOMAINS="$fixed_domains"
            ;;
        *)
            _log "ERROR: fixed HMAC domain contract is invalid"
            return 1
            ;;
    esac
}

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

_get_secret_version() {
    local name="$1"
    local version_id="$2"
    aws secretsmanager get-secret-value \
        --secret-id "$name" \
        --version-id "$version_id" \
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

_load_hmac_keyring() {
    local prefix="$1"
    local primary_name_var="${prefix}_HMAC_SECRET_NAME"
    local primary_version_var="${prefix}_HMAC_PRIMARY_VERSION_ID"
    local primary_generation_var="${prefix}_HMAC_PRIMARY_GENERATION"
    local previous_name_var="${prefix}_HMAC_PREVIOUS_SECRET_NAME"
    local previous_version_var="${prefix}_HMAC_PREVIOUS_VERSION_ID"
    local previous_generation_var="${prefix}_HMAC_PREVIOUS_GENERATION"
    local previous_t0_var="${prefix}_HMAC_PREVIOUS_ROTATION_STARTED_AT"
    local previous_legacy_var="${prefix}_HMAC_PREVIOUS_IS_LEGACY"
    local legacy_worker_name_var="${prefix}_HMAC_LEGACY_WORKER_SECRET_NAME"
    local legacy_worker_version_var="${prefix}_HMAC_LEGACY_WORKER_VERSION_ID"
    local legacy_worker_generation_var="${prefix}_HMAC_LEGACY_WORKER_GENERATION"

    _require_env "$primary_name_var" || return 1
    _require_env "$primary_version_var" || return 1
    _require_env "$primary_generation_var" || return 1

    local primary_name="${!primary_name_var:-}"
    local primary_version="${!primary_version_var:-}"
    local primary_generation="${!primary_generation_var:-}"
    if [[ ! "$primary_version" =~ ^[A-Za-z0-9_-]{32,64}$ ]]; then
        _log "ERROR: $prefix primary VersionId is invalid"
        return 1
    fi
    if [[ "$primary_generation" =~ [[:space:]] || "$primary_generation" != *"@$primary_version" ]]; then
        _log "ERROR: $prefix primary generation metadata is invalid"
        return 1
    fi
    if [[ "$primary_name" == "${DB_PASSWORD_SECRET_NAME:-}" || "$primary_name" == */database-url ]]; then
        _log "ERROR: $prefix primary cannot be a database credential secret"
        return 1
    fi

    local primary_value
    primary_value="$(_get_secret_version "$primary_name" "$primary_version")"
    if [[ -z "$primary_value" || "$primary_value" == "None" ]]; then
        _log "ERROR: $prefix dedicated primary version could not be loaded"
        return 1
    fi
    local primary_target="${prefix}_HMAC_SECRET"
    printf -v "$primary_target" '%s' "$primary_value"
    export "$primary_target"
    unset primary_value

    local previous_name="${!previous_name_var:-}"
    local previous_version="${!previous_version_var:-}"
    local previous_generation="${!previous_generation_var:-}"
    local previous_t0="${!previous_t0_var:-}"
    local previous_legacy="${!previous_legacy_var:-}"
    if [[ -n "$previous_name$previous_version$previous_generation$previous_t0$previous_legacy" ]]; then
        if [[ -z "$previous_name" || -z "$previous_version" || -z "$previous_generation" || -z "$previous_t0" ]]; then
            _log "ERROR: $prefix previous generation and T0 must be configured atomically"
            return 1
        fi
        if [[ -n "$previous_legacy" && "$previous_legacy" != "1" ]]; then
            _log "ERROR: $prefix legacy marker is invalid"
            return 1
        fi
        if [[ ! "$previous_version" =~ ^[A-Za-z0-9_-]{32,64}$ ]]; then
            _log "ERROR: $prefix previous VersionId is invalid"
            return 1
        fi
        if [[ "$previous_generation" =~ [[:space:]] || "$previous_generation" != *"@$previous_version" ]]; then
            _log "ERROR: $prefix previous generation metadata is invalid"
            return 1
        fi
        if [[ ! "$previous_t0" =~ ^[0-9]{1,10}$ || "$previous_generation" == "$primary_generation" ]]; then
            _log "ERROR: $prefix previous generation or fixed T0 is invalid"
            return 1
        fi
        if [[ "$previous_legacy" == "1" && "$previous_name" != */database-url ]]; then
            _log "ERROR: $prefix legacy previous must be the pinned database-url secret"
            return 1
        fi
        if [[ -z "$previous_legacy" && "$previous_name" == */database-url ]]; then
            _log "ERROR: $prefix database credential previous requires the bounded legacy marker"
            return 1
        fi

        local previous_value
        previous_value="$(_get_secret_version "$previous_name" "$previous_version")"
        if [[ -z "$previous_value" || "$previous_value" == "None" ]]; then
            _log "ERROR: $prefix previous version could not be loaded"
            return 1
        fi
        local previous_target="${prefix}_HMAC_PREVIOUS_SECRET"
        printf -v "$previous_target" '%s' "$previous_value"
        export "$previous_target"
        if [[ "$previous_legacy" == "1" ]]; then
            printf -v "$previous_legacy_var" '%s' "1"
            export "$previous_legacy_var"
        else
            unset "$previous_legacy_var"
        fi
        unset previous_value
        _log "OK: $prefix HMAC keyring loaded (bounded previous enabled)"
    else
        unset "${prefix}_HMAC_PREVIOUS_SECRET"
        unset "$previous_generation_var"
        unset "$previous_t0_var"
        unset "$previous_legacy_var"
        _log "OK: $prefix HMAC keyring loaded (primary only)"
    fi

    if [[ "$prefix" == "MAIL_ACTION" ]]; then
        local legacy_worker_name="${!legacy_worker_name_var:-}"
        local legacy_worker_version="${!legacy_worker_version_var:-}"
        local legacy_worker_generation="${!legacy_worker_generation_var:-}"
        if [[ -n "$legacy_worker_name$legacy_worker_version$legacy_worker_generation" ]]; then
            if [[ "$previous_legacy" != "1" ]]; then
                _log "ERROR: legacy worker verification key is valid only for MAIL_ACTION migration"
                return 1
            fi
            if [[ -z "$legacy_worker_name" || -z "$legacy_worker_version" || -z "$legacy_worker_generation" ]]; then
                _log "ERROR: legacy worker generation metadata must be configured atomically"
                return 1
            fi
            if [[ "$legacy_worker_name" != "${SLACK_BOT_TOKEN_SECRET_NAME:-}" || "$legacy_worker_name" != *slack* ]]; then
                _log "ERROR: legacy worker key must be the pinned Slack bot secret"
                return 1
            fi
            if [[ ! "$legacy_worker_version" =~ ^[A-Za-z0-9_-]{32,64}$ ]]; then
                _log "ERROR: legacy worker VersionId is invalid"
                return 1
            fi
            if [[ "$legacy_worker_generation" =~ [[:space:]] || "$legacy_worker_generation" != *"@$legacy_worker_version" || "$legacy_worker_generation" == "$primary_generation" || "$legacy_worker_generation" == "$previous_generation" ]]; then
                _log "ERROR: legacy worker generation metadata is invalid"
                return 1
            fi
            local legacy_worker_value
            legacy_worker_value="$(_get_secret_version "$legacy_worker_name" "$legacy_worker_version")"
            if [[ -z "$legacy_worker_value" || "$legacy_worker_value" == "None" ]]; then
                _log "ERROR: legacy worker version could not be loaded"
                return 1
            fi
            printf -v MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET '%s' "$legacy_worker_value"
            export MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET
            unset legacy_worker_value
            _log "OK: MAIL_ACTION legacy worker compatibility loaded (bounded=true)"
        else
            unset MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET
            unset "$legacy_worker_name_var"
            unset "$legacy_worker_version_var"
            unset "$legacy_worker_generation_var"
        fi
    fi
}

_load_required_hmac_keyrings() {
    case "${TEAMAGENT_HMAC_REQUIRED_DOMAINS:-}" in
        "")
            # Shared helpers such as run_ingest.sh do not issue or verify capability tokens.
            # They must not gain HMAC access merely because they source this common loader.
            return 0
            ;;
        MAIL_ACTION)
            _load_hmac_keyring MAIL_ACTION
            ;;
        REPORT_LINK)
            _load_hmac_keyring REPORT_LINK
            ;;
        MAIL_ACTION,REPORT_LINK)
            _load_hmac_keyring MAIL_ACTION && _load_hmac_keyring REPORT_LINK
            ;;
        *)
            _log "ERROR: TEAMAGENT_HMAC_REQUIRED_DOMAINS is invalid"
            return 1
            ;;
    esac
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
    _log "OK: Slack credentials loaded (bot=true, app=true)"

    # Purpose-separated token keyrings. VersionIds and T0 are non-secret deployment metadata;
    # payloads are fetched by exact version and are never printed. The database credential is
    # permitted only as a bounded previous generation during migration, never as a primary.
    _load_required_hmac_keyrings || return 1

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

    # ingest #ops 通知用 Slack Incoming Webhook（任意 — 未投入なら ingest pipeline の通知が no-op）
    if [[ -n "${OPS_SLACK_WEBHOOK_SECRET_NAME:-}" ]]; then
        local ops_webhook
        ops_webhook="$(_get_secret "$OPS_SLACK_WEBHOOK_SECRET_NAME" 2>/dev/null || true)"
        if [[ -n "$ops_webhook" ]]; then
            export OPS_SLACK_WEBHOOK_URL="$ops_webhook"
            _log "OK: OPS_SLACK_WEBHOOK_URL loaded"
        else
            _log "INFO: OPS_SLACK_WEBHOOK_SECRET_NAME は設定済だが secret 未投入（ingest alerts は disabled）"
        fi
    fi

    # OAuth state HMAC secret（連携 connect の CSRF/本人性検証用・平文禁止＝Secrets Manager 経由）。
    # Bot(make_state) と connect_web(verify_state) で **同一値** を共有する必要がある。
    if [[ -n "${OAUTH_STATE_SECRET_NAME:-}" ]]; then
        local osecret
        osecret="$(_get_secret "$OAUTH_STATE_SECRET_NAME" 2>/dev/null || true)"
        if [[ -n "$osecret" ]]; then
            export OAUTH_STATE_SECRET="$osecret"
            _log "OK: OAUTH_STATE_SECRET loaded"
        else
            _log "WARN: OAUTH_STATE_SECRET_NAME 設定済だが取得失敗（連携が verify_state で失敗）"
        fi
    fi

    # 連携(per-user web)専用クライアントの secret（共有 desktop クライアントと分離・B案）。
    if [[ -n "${CONNECT_GOOGLE_CLIENT_SECRET_NAME:-}" ]]; then
        local csecret
        csecret="$(_get_secret "$CONNECT_GOOGLE_CLIENT_SECRET_NAME" 2>/dev/null || true)"
        if [[ -n "$csecret" ]]; then
            export CONNECT_GOOGLE_CLIENT_SECRET="$csecret"
            _log "OK: CONNECT_GOOGLE_CLIENT_SECRET loaded"
        else
            _log "WARN: CONNECT_GOOGLE_CLIENT_SECRET_NAME 設定済だが取得失敗（連携が client未設定で失敗）"
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
                _log "OK: Google OAuth credentials loaded (configured=true)"
            else
                _log "WARN: Google OAuth secret は取得できたが JSON parse 失敗"
            fi
        else
            _log "INFO: Google OAuth secret は未投入（skip）"
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

if ! _prepare_hmac_loader "${1:-}"; then
    return 1 2>/dev/null || exit 1
fi
_load
