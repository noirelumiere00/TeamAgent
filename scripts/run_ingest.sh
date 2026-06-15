#!/usr/bin/env bash
# ============================================================
# TeamAgent — 社内ナレッジの定期 ingest ラッパ。
#
# data/ingest_sources.yaml に登録済みのソース（Slack / Google Drive /
# Google Sheets）を pgvector(documents/chunks) に取り込む。
# documents は ON CONFLICT (source_type, external_id) DO UPDATE で冪等なので、
# 毎回フル走査しても重複は溜まらず、新規追加・更新差し替えだけが反映される。
#
# 起動の env ロードは teamagent-bot.service の ExecStart と同じ流儀に揃えている：
#   set -a; source teamagent.env.base; source scripts/load_secrets.sh; set +a
# これで Bot と同一の RDS 接続情報・Google 認証情報をそのまま流用する
# （= GCP credentials を ingest 用に別途用意する必要が無い）。
#
# Usage:
#   本番 worker EC2 (systemd 経由):
#     teamagent-ingest.service が WorkingDirectory=/opt/teamagent/app で本スクリプトを呼ぶ
#   手動 / ローカル (SSM tunnel):
#     set -a; source .env.local; set +a
#     scripts/run_ingest.sh
#
# 環境変数（未設定なら worker のパスを既定値に）:
#   TEAMAGENT_ENV_BASE  env ファイル（既定 /opt/teamagent/teamagent.env.base）
#   INGEST_SOURCES      取り込むソース（既定 slack,gdrive,gsheets）
#   INGEST_OWNER_EMAIL  ドキュメント所有者（既定 shogo@vectorinc.co.jp）
#   INGEST_PYTHON       使う python（既定 ./.venv/bin/python）
#   INGEST_DRY_RUN      "1" なら --commit を付けず dry-run（検証用）
# ============================================================
set -euo pipefail

# --- リポジトリルートへ移動（scripts/ の親）。systemd の WorkingDirectory に依存しない ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

ENV_BASE="${TEAMAGENT_ENV_BASE:-/opt/teamagent/teamagent.env.base}"
SOURCES="${INGEST_SOURCES:-slack,gdrive,gsheets}"
OWNER_EMAIL="${INGEST_OWNER_EMAIL:-shogo@vectorinc.co.jp}"
PYTHON="${INGEST_PYTHON:-./.venv/bin/python}"

log() { echo "[run_ingest] $(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"; }

log "start sources=${SOURCES} owner=${OWNER_EMAIL} repo=${REPO_ROOT}"

# --- env / secrets ロード（teamagent-bot.service と同一手順）---
set -a
# env.base は worker のみ存在。ローカルは事前に .env.local を source 済み想定なので無くても続行。
if [[ -f "${ENV_BASE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_BASE}"
else
  log "warn: ${ENV_BASE} not found — 事前に .env.* を source 済みとして続行"
fi
# shellcheck disable=SC1091
source scripts/load_secrets.sh
set +a

# --- commit / dry-run 切替（既定は本投入）---
COMMIT_FLAG="--commit"
if [[ "${INGEST_DRY_RUN:-0}" == "1" ]]; then
  COMMIT_FLAG=""
  log "DRY-RUN mode: DB へは書き込みません"
fi

# 終了コードを確実に拾うため、この呼び出しだけ set -e を一時解除する
set +e
# shellcheck disable=SC2086
"${PYTHON}" scripts/ingest_sources.py ${COMMIT_FLAG} \
  --sources "${SOURCES}" \
  --owner-email "${OWNER_EMAIL}"
rc=$?
set -e

log "done exit=${rc}"
exit ${rc}
