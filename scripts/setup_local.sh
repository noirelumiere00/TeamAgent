#!/usr/bin/env bash
# ============================================================
# TeamAgent v3.0 — ローカル開発環境セットアップ (macOS / Linux)
# ============================================================
# 想定: 初回 clone 後にこのスクリプトを叩けば、ローカルで開発開始できる
#
# 前提:
#   - Python 3.11+
#   - Docker Desktop
#   - uv または pip
# ============================================================

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "============================================"
echo " TeamAgent v3.0 — Local Setup"
echo " Root: $ROOT"
echo "============================================"

# ---------- 1. .env 確認 ----------
if [ ! -f .env ]; then
    echo "[1/5] .env が見つからないので .env.example からコピー"
    cp .env.example .env
    echo "      ⚠ .env を編集して各種 API キーを入れてください"
else
    echo "[1/5] .env 確認: OK"
fi

# ---------- 2. Python 仮想環境 ----------
echo "[2/5] Python venv セットアップ"
if [ ! -d .venv ]; then
    python3.11 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# ---------- 3. 依存関係インストール ----------
echo "[3/5] 依存関係インストール"
if command -v uv &> /dev/null; then
    uv pip install -e ".[dev]"
else
    pip install -U pip
    pip install -e ".[dev]"
fi

# Playwright ブラウザ（HTML→PDF 変換用）
python -m playwright install chromium

# ---------- 4. Docker 起動 ----------
echo "[4/5] PostgreSQL + pgvector を Docker 起動"
docker compose -f infra/docker/docker-compose.yml up -d

# 起動待ち
echo "      DB 起動待ち..."
for i in $(seq 1 30); do
    if docker exec teamagent-postgres pg_isready -U teamagent &> /dev/null; then
        echo "      DB OK"
        break
    fi
    sleep 1
done

# ---------- 5. スモークテスト ----------
echo "[5/5] スモークテスト: pgvector 接続"
PGPASSWORD=teamagent psql -h localhost -U teamagent -d teamagent -c "SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';"

echo ""
echo "============================================"
echo " ✅ セットアップ完了"
echo "============================================"
echo " - PostgreSQL:  localhost:5432  (user: teamagent, pass: teamagent)"
echo " - Adminer:     http://localhost:8080"
echo " - MinIO:       http://localhost:9001  (teamagent / teamagent-local)"
echo ""
echo " 次のステップ:"
echo "   1. .env を編集して API キーを入れる"
echo "   2. source .venv/bin/activate"
echo "   3. pytest tests/  でテスト確認"
echo "============================================"
