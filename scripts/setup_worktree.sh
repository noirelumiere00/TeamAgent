#!/usr/bin/env bash
# ============================================================
# TeamAgent — 任意のワークツリーを「実行できる」状態にする
# ============================================================
# git worktree を増やすと .venv / .env はワークツリー間で共有されない（各自必要）。
# このスクリプトを **対象ワークツリーの中で** 叩けば、その場所に
#   1) per-worktree の .venv（uv）           2) editable で teamagent をインストール
#   3) .env（無ければ .env.example から）    4) スモークテスト（外部I/O無しの pytest 一部）
# まで整い、テスト実行・Bot起動ができるようになる。冪等（何度叩いてもOK）。
#
# 使い方:
#   cd /path/to/other-worktree
#   bash /path/to/TeamAgent/scripts/setup_worktree.sh            # 通常
#   bash .../setup_worktree.sh --playwright                     # Chromium も入れる(HTML→PDF/scraper)
#   bash .../setup_worktree.sh --no-smoke                       # スモークテストを省略
# ============================================================

set -euo pipefail

DO_PLAYWRIGHT=0
DO_SMOKE=1
for arg in "$@"; do
  case "$arg" in
    --playwright) DO_PLAYWRIGHT=1 ;;
    --no-smoke)   DO_SMOKE=0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# 対象は「いま居るワークツリー」（スクリプトの置き場所ではない）。
ROOT="$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$ROOT" ]; then
  echo "❌ ここは git 作業ツリーではありません。対象ワークツリーの中で実行してください。" >&2
  exit 1
fi
cd "$ROOT"
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"

echo "============================================"
echo " TeamAgent — worktree setup"
echo " Root:   $ROOT"
echo " Branch: $BRANCH  ($(git rev-parse --short HEAD 2>/dev/null || echo '?'))"
echo "============================================"

if ! command -v uv >/dev/null 2>&1; then
  echo "❌ uv が見つかりません。'curl -LsSf https://astral.sh/uv/install.sh | sh' で導入してください。" >&2
  exit 1
fi

# ---------- 1. .env ----------
if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    echo "[1/4] .env を .env.example から作成（⚠ 実行前に値を埋める。テストだけなら未編集でOK）"
  else
    echo "[1/4] .env.example が無いため .env はスキップ"
  fi
else
  echo "[1/4] .env: 既存を使用"
fi

# ---------- 2. venv（per-worktree, uv） ----------
if [ ! -d .venv ]; then
  echo "[2/4] .venv を作成（uv venv）"
  uv venv
else
  echo "[2/4] .venv: 既存を使用（$(.venv/bin/python --version 2>/dev/null || echo '?')）"
fi

# ---------- 3. 依存（editable で teamagent + dev） ----------
echo "[3/4] 依存をインストール（uv pip install -e \".[dev]\"）— このワークツリーの src を指す editable"
VIRTUAL_ENV="$ROOT/.venv" uv pip install -e ".[dev]" >/dev/null
if [ "$DO_PLAYWRIGHT" -eq 1 ]; then
  echo "      Playwright chromium を導入（HTML→PDF / scraper 用）"
  VIRTUAL_ENV="$ROOT/.venv" .venv/bin/python -m playwright install chromium >/dev/null
fi

# ---------- 4. スモークテスト（外部I/O無し＝DB/トークン不要） ----------
if [ "$DO_SMOKE" -eq 1 ]; then
  echo "[4/4] スモークテスト（import + ルーティング/ack、外部I/O無し）"
  .venv/bin/python -c "import teamagent; from teamagent.skills.intent import detect_skill; print('   import OK / route:', detect_skill('A社の事例を調べて').skill)"
  .venv/bin/python -m pytest tests/skills/test_intent.py tests/runtime/test_slack_bot.py -q -p no:cacheprovider 2>&1 | tail -2
else
  echo "[4/4] スモークテスト: --no-smoke のためスキップ"
fi

echo ""
echo "============================================"
echo " ✅ このワークツリーは実行可能になりました"
echo "============================================"
echo " source .venv/bin/activate"
echo " # テスト（外部I/O無し・課金0）:"
echo "   python -m pytest -q"
echo " # Bot 起動（要 .env: SLACK_BOT_TOKEN / SLACK_APP_TOKEN / DATABASE_URL ほか）:"
echo "   python -m teamagent.runtime.slack_bot"
echo " # ローカルDB or RDSトンネルが要る場合:"
echo "   scripts/setup_local.sh   # docker で local postgres を立てる"
echo "============================================"
