#!/usr/bin/env bash
# 隔離リポジトリに残った候補タグを片付ける。
# 候補タグ（candidate-<commit>-core / -media）は便を撃つたびに作り直されるもので、
# 本番の参照には使われない。ECR は immutable tag なので、同じコミットで撃ち直すと
# 'tag already exists ... cannot be overwritten' で push が拒否される。
#
# 使い方: bash purge_candidate_tags.sh <commit-sha>

set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/common.sh" || exit 1

export AWS_REGION=ap-northeast-1
export AWS_PAGER=""

COMMIT="${1:?使い方: bash purge_candidate_tags.sh <commit-sha>}"

# 候補イメージは 2 つのリポジトリに分かれて push される。
#   core  -> teamagent-mcp-quarantine
#   media -> teamagent-media-worker-quarantine
# 片方だけ消しても、もう片方で push が拒否されて段3 が落ちる（2026-09-11 に実際に踏んだ）。
REPOS="teamagent-mcp-quarantine teamagent-media-worker-quarantine"

echo "== 対象コミット: $COMMIT"

TOTAL_LEFT=0
for REPO in $REPOS; do
  echo
  echo "================ $REPO"
  echo "-- 現在のタグ"
  aws ecr list-images --repository-name "$REPO" --filter tagStatus=TAGGED \
    --query 'imageIds[].imageTag' --output json | jq -r '.[] // empty' | sed 's/^/   /'

  TAGS="$(aws ecr list-images --repository-name "$REPO" --filter tagStatus=TAGGED \
    --query 'imageIds[].imageTag' --output json \
    | jq -r --arg c "$COMMIT" '.[] // empty | select(startswith("candidate-" + $c + "-"))')"

  if [ -z "$TAGS" ]; then
    echo "-- このコミットの候補タグはありません。"
    continue
  fi

  echo "-- 削除するタグ"
  echo "$TAGS" | sed 's/^/   /'

  IDS="$(echo "$TAGS" | jq -R -s -c 'split("\n") | map(select(length>0)) | map({imageTag: .})')"
  echo "-- 削除を実行"
  aws --profile "$RELEASE_PROFILE" ecr batch-delete-image --repository-name "$REPO" --image-ids "$IDS" --output json

  LEFT="$(aws ecr list-images --repository-name "$REPO" --filter tagStatus=TAGGED \
    --query 'imageIds[].imageTag' --output json \
    | jq -r --arg c "$COMMIT" '[.[] // empty | select(startswith("candidate-" + $c + "-"))] | length')"
  echo "-- 残り: $LEFT 件"
  TOTAL_LEFT=$((TOTAL_LEFT + LEFT))
done

echo
if [ "$TOTAL_LEFT" = "0" ]; then
  echo "OK: 両リポジトリともこのコミットの候補タグは残っていません。"
else
  echo "FATAL: 合計 $TOTAL_LEFT 件残っています。"
  exit 1
fi
