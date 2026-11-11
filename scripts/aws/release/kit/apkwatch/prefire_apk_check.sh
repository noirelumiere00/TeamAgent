#!/usr/bin/env bash
# 発射前の apk 在庫確認（2026-09-24・改善 1）。fire_and_authorize_rNN.sh の STEP 1a から呼ぶ。
#   1. 索引での確認（必須）: 0 以外なら撃たない
#   2. apk 段の実ビルド（Docker が起動していれば必須）: DRIFT なら撃たない。Docker が無い／確認できないときは WARN で続行
#      （速い確認だけでは「新しい版の出現・依存の変化」を確定できない。確定させたいときは Docker を起動してから撃つ）
# 使い方: bash prefire_apk_check.sh <発射台のパス>   # 発射台の HEAD（ff 後の origin/dev と同じ）を対象にする
set -uo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/../common.sh" || exit 1
PAD="${1:?usage: prefire_apk_check.sh <発射台>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REF="$(git -C "$PAD" rev-parse HEAD)"

python3 "$HERE/check_media_apk_pins.py" --repo "$PAD" --ref "$REF"
RC1=$?
[ "$RC1" = "0" ] || { echo "FATAL: 索引での確認が OK でない（exit ${RC1}）。DRIFT なら apkwatch/apply_apklock.sh で追随してから撃つ"; exit 1; }

if docker info >/dev/null 2>&1; then
  bash "$HERE/shadow_build_apk.sh" "$REF"
  RC2=$?
  case "$RC2" in
    0) ;;
    1) echo "FATAL: apk 段の実ビルドが lock と食い違う（段 3 で落ちる）。apkwatch/apply_apklock.sh で追随してから撃つ"; exit 1 ;;
    *) echo "WARN: apk 段の実ビルドで確認できなかった（exit ${RC2}）。索引での確認だけで続行する" ;;
  esac
else
  echo "WARN: Docker が起動していないので apk 段の実ビルドは省略（索引での確認は OK）。確定させたいなら open -a Docker のあと撃ち直す"
fi
echo "OK: apk 在庫確認を通過（commit ${REF:0:8}）"
