#!/usr/bin/env bash
# r100（動画ライブラリの自社と他社の区別・他社事例 #825）を 1 本で流す（11-05・Claude、小俣さん GO）。MCP 便だけ・OC 便は不要・env は r88 のものを継承。
#   ① 空撃ち（--dry-only）→ ② 本番の撃ち → ③ args.env の digest で apply_mcp_tds.sh
# 前提: #825 が dev にマージ済み・0041 適用済み・releases/r100.json が改名済み・MFA セッション有効。各段 fail-closed。
set -euo pipefail
A=/Users/s-komata/Documents/Claude/Artifacts/aico-inputs-20260911
ARGS=/private/tmp/r67-fire/args.env
R98_CORE=sha256:43c175c33ea799b5476540437f1f51f8cce9703047b8016c1713d192bc90101f
cd "$A"
[ -f releases/r100.json ] || { echo "FATAL: releases/r100.json が無い（.draft の改名がまだ）"; exit 1; }

echo "== ① 空撃ち"
bash fire_and_authorize.sh releases/r100.json --dry-only

echo
echo "== ② 本番の撃ち"
bash fire_and_authorize.sh releases/r100.json "APPROVED: r100 動画ライブラリ 自社と他社の区別・他社事例（小俣さん GO 11-05）"

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
