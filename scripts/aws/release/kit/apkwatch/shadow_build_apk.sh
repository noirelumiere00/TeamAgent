#!/usr/bin/env bash
# media の apk 段（--target runtime-packages）を CodeBuild と同じ条件で手元で組み立て、実インベントリを media-apk.lock と照合する
# 「確実な」在庫確認（2026-09-24・独立レビュー F1/F5 の対処）。
#   - check_media_apk_pins.py は索引だけを見るので「新しい版の出現・依存の変化」を確定できない。apk の依存解決を実際に走らせて確かめる
#   - CodeBuild は毎回新しい builder（索引は常に最新）。手元の desktop-linux builder は apk の cache mount（--cache-max-age 1440）を
#     持ち越すので、ビルド前に cache mount を捨て、runtime-packages 段は --no-cache-filter で必ず実行し直す
#   - 作業ツリーは作らない（git show で Dockerfile と lock だけを一時ディレクトリへ）。発射台にも触らない
# 使い方: bash shadow_build_apk.sh [<git ref>=origin/dev]
# 終了コード: 0=lock と一致 / 1=DRIFT（解決不能 or lock と食い違い＝段 3 で落ちる）/ 2=確認できない（Docker なし・ベース取得失敗など）
set -uo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/../common.sh" || exit 1
REF="${1:-origin/dev}"
REPO="$RELEASE_PAD"
HERE="$RELEASE_WORK_DIR/apkwatch"
TS="$(date +%Y%m%d-%H%M%S)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
CA="${AWS_CA_BUNDLE:-$(python3 -c 'import certifi;print(certifi.where())' 2>/dev/null)}"
[ -s "$CA" ] || { echo "UNKNOWN: CA バンドル（certifi）が見つからない"; exit 2; }
docker info >/dev/null 2>&1 || { echo "UNKNOWN: Docker が起動していない（open -a Docker で起動してから再実行）"; exit 2; }

git -C "$REPO" fetch -q origin 2>/dev/null || echo "WARN: git fetch に失敗（手元の $REF で判定）"
mkdir -p "$TMP/infra/docker"
git -C "$REPO" show "$REF:infra/docker/Dockerfile.teamagent-media-worker" > "$TMP/infra/docker/Dockerfile.teamagent-media-worker" \
  && git -C "$REPO" show "$REF:infra/docker/media-apk.lock" > "$TMP/media-apk.lock" \
  || { echo "UNKNOWN: $REF から Dockerfile / lock を読めない"; exit 2; }
echo "== apk 段の実ビルド（$REF $(git -C "$REPO" rev-parse --short "$REF")・索引は最新）"

# 手元 builder の apk cache mount を捨てる（最大 24 時間前の索引を使わないため）。ローカルのビルドキャッシュだけが対象
docker buildx prune --filter type=exec.cachemount --force >/dev/null 2>&1 || true
if ! docker buildx build --platform linux/arm64 \
      --file "$TMP/infra/docker/Dockerfile.teamagent-media-worker" --target runtime-packages \
      --no-cache-filter runtime-packages --secret id=teamagent_ca,src="$CA" \
      --load -t "media-apk-shadow:$TS" "$TMP" > "$TMP/build.log" 2>&1; then
  if grep -qE "unable to select packages|breaks: world" "$TMP/build.log"; then
    echo "DRIFT: apk が固定した版を解決できない（段 3 で必ず落ちる）"
    grep -E "unable to select|breaks:|satisfies:" "$TMP/build.log" | sed 's/^#[0-9]* [0-9.]* /  /' | head -12
    exit 1
  fi
  if grep -qE "sha256sum: WARNING|FAILED$|: FAILED" "$TMP/build.log"; then
    echo "DRIFT: apk 段のバイナリ照合（python3 / node / ffmpeg / chromium の sha256）が合わない"
    grep -E "FAILED|WARNING" "$TMP/build.log" | head -8
    exit 1
  fi
  echo "UNKNOWN: apk 段のビルドが別の理由で失敗（ネットワーク・ベースイメージの取得など）"
  grep -E "ERROR|error:" "$TMP/build.log" | head -8
  exit 2
fi
docker run --rm --platform linux/arm64 --entrypoint cat "media-apk-shadow:$TS" \
  /usr/share/teamagent/provenance/apk-installed.lock > "$TMP/actual.lock" || { echo "UNKNOWN: 実インベントリを取り出せない"; exit 2; }
docker image rm -f "media-apk-shadow:$TS" >/dev/null 2>&1 || true
if cmp -s "$TMP/media-apk.lock" "$TMP/actual.lock"; then
  echo "OK: 実インベントリは media-apk.lock と一致（$(wc -l < "$TMP/actual.lock" | tr -d ' ') 行）"
  exit 0
fi
mkdir -p "$HERE/reports"; cp "$TMP/actual.lock" "$HERE/reports/actual-apk-$TS.lock"
echo "DRIFT: 実インベントリが media-apk.lock と食い違う（段 3 の最終 cmp で落ちる）。実測は reports/actual-apk-$TS.lock"
diff "$TMP/media-apk.lock" "$TMP/actual.lock" | sed 's/^/  /' | head -30
exit 1
