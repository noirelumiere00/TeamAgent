#!/usr/bin/env bash
# r99（動画ライブラリの URL 取り込み #817・#818、事例推定の毎日 ON #816・ロック修正 #815）を 1 本で流す（11-04・Claude、小俣さん GO）。MCP 便だけ・OC 便は不要・env は r88 のものを継承。
#   ① 空撃ち（--dry-only）→ ② 本番の撃ち → ③ args.env の digest で apply_mcp_tds.sh
# 前提: #815〜#818 が dev にマージ済み・releases/r99.json が改名済み・MFA セッション有効。各段 fail-closed。
set -euo pipefail
A=/Users/s-komata/Documents/Claude/Artifacts/aico-inputs-20260911
ARGS=/private/tmp/r67-fire/args.env
R98_CORE=sha256:ad2c436791e59b866ba28e23eb7297eb03d96a71fa29f69d17169bb9d1b27c51
cd "$A"
[ -f releases/r99.json ] || { echo "FATAL: releases/r99.json が無い（.draft の改名がまだ）"; exit 1; }

echo "== ① 空撃ち"
bash fire_and_authorize.sh releases/r99.json --dry-only

echo
echo "== ② 本番の撃ち"
bash fire_and_authorize.sh releases/r99.json "APPROVED: r99 動画ライブラリ URL 取り込み・事例推定の毎日 ON（小俣さん GO 11-04）"

CORE_DIGEST="$(grep '^CORE_DIGEST=' "$ARGS" | cut -d= -f2-)"
MEDIA_DIGEST="$(grep '^MEDIA_DIGEST=' "$ARGS" | cut -d= -f2-)"
[[ "$CORE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ && "$MEDIA_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || { echo "FATAL: args.env の digest が読めない"; exit 1; }
[ "$CORE_DIGEST" != "$R98_CORE" ] || { echo "FATAL: args.env が r98 の core のまま（撃ちが終わっていない）"; exit 1; }

echo
echo "== ③ apply_mcp_tds.sh core=$CORE_DIGEST media=$MEDIA_DIGEST"
bash "$A/apply_mcp_tds.sh" "$CORE_DIGEST" "$MEDIA_DIGEST"

echo
echo "== 完了: /library/import が開けること・connect-web の image が #818 を含むことを確かめる"
