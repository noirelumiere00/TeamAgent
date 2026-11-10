#!/usr/bin/env bash
# mcp 便の本番適用（image だけ差し替え・env は現行を継承）を 1 本で行う汎用版（2026-09-24・改善 2）。
# apply_r27/r28_tds.sh は便ごとに「前便の digest」「TD 番号」「モデル名」を手で書き換えていたが、
# 本スクリプトは本番の現況（mcp の PRIMARY TD・tiktok-acquire の dispatch 先）から前便の値を読み取る。
#
#   0. 前提確認: 渡された core / media が本番リポジトリの active タグ付きで同じコミット由来か・
#      そのコミットが現行本番のコミットを含む（祖先）か・現行と違う digest か
#   ① mcp: register_mcp_td_image_env.sh <core>（image のみ・env 継承・実行中ジョブ確認・rollout 完了待ち・OC bounce）
#   ② consumers 4 つ: swap_mcp_consumers_td.sh <core>
#   ③ media: deploy_media_td.sh <media>（既に新 media なら飛ばす）
#   最後に verify_r28_live.sh（期待モデルは現行 mcp の env から取る＝このスクリプトはモデルを変えない）
#
# 使い方: bash apply_mcp_tds.sh <sha256:core> <sha256:media> [--from=1|2|3]
#   途中で落ちたら落ちた段から --from=N で再開（済んだ段を流し直さない）。
set -euo pipefail
# shellcheck source=common.sh
source "$(cd "$(dirname "$0")" && pwd)/common.sh" || exit 1
CORE="${1:?usage: apply_mcp_tds.sh <sha256:core> <sha256:media> [--from=1|2|3]}"
MEDIA="${2:?usage: apply_mcp_tds.sh <sha256:core> <sha256:media> [--from=1|2|3]}"
FROM=1
case "${3:-}" in
  "") ;;
  --from=1|--from=2|--from=3) FROM="${3#--from=}" ;;
  *) echo "FATAL: 第 3 引数は --from=1|2|3"; exit 2 ;;
esac
[[ "$CORE" =~ ^sha256:[0-9a-f]{64}$ ]] && [[ "$MEDIA" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "FATAL: digest 形式が不正"; exit 1; }
TOOLS="$KIT_DIR"
A="$KIT_DIR"
PAD="$RELEASE_PAD"
CLUSTER=teamagent-dev
echo "== 実行主体"; aws sts get-caller-identity --query Arn --output text | sed -E 's/[0-9]{12}/<acct>/'

commit_of() {  # <repo> <digest> <core|media>
  aws ecr describe-images --repository-name "$1" --image-ids imageDigest="$2" --query 'imageDetails[0].imageTags' --output text \
    | tr '\t' '\n' | sed -nE "s/^active-([0-9a-f]{40})-$3\$/\1/p" | head -1
}

echo; echo "=== 0. 前提確認"
MCP_TD="$(aws ecs describe-services --cluster "$CLUSTER" --services teamagent-dev-mcp --query 'services[0].deployments[?status==`PRIMARY`].taskDefinition | [0]' --output text)"
LIVE_CORE="$(aws ecs describe-task-definition --task-definition "$MCP_TD" --query 'taskDefinition.containerDefinitions[0].image' --output text | sed 's/.*@//')"
LIVE_MODEL="$(aws ecs describe-task-definition --task-definition "$MCP_TD" --query "taskDefinition.containerDefinitions[0].environment[?name=='GEMINI_MODEL_ID'].value | [0]" --output text)"
LIVE_LOC="$(aws ecs describe-task-definition --task-definition "$MCP_TD" --query "taskDefinition.containerDefinitions[0].environment[?name=='GEMINI_VERTEX_LOCATION'].value | [0]" --output text)"
COMMIT="$(commit_of teamagent-mcp "$CORE" core)"
[ -n "$COMMIT" ] || { echo "FATAL: core に active-<commit>-core タグが無い"; exit 1; }
[ "$(commit_of teamagent-media-worker "$MEDIA" media)" = "$COMMIT" ] || { echo "FATAL: media が core と同じコミットの active タグを持たない（別の便）"; exit 1; }
git -C "$PAD" fetch -q origin
git -C "$PAD" cat-file -e "$COMMIT^{commit}" 2>/dev/null || { echo "FATAL: コミット $COMMIT が発射台に無い"; exit 1; }
if [ "$FROM" -le 1 ]; then
  [ "$CORE" != "$LIVE_CORE" ] || { echo "FATAL: core が現行本番（${MCP_TD##*/}）と同じ digest。--from=2 以降で再開するなら指定する"; exit 1; }
  # 09-24 レビュー D-06: 現行 core にタグが無いと祖先判定が黙って飛ぶ・mcp がデプロイ途中でも進む、を止める
  LIVE_COMMIT="$(commit_of teamagent-mcp "$LIVE_CORE" core)"
  [ -n "$LIVE_COMMIT" ] || { echo "FATAL: 現行本番の core（${LIVE_CORE:7:12}…）に active-<commit>-core タグが無い（便以外の経路で差し替わった？）"; exit 1; }
  git -C "$PAD" merge-base --is-ancestor "$LIVE_COMMIT" "$COMMIT" \
    || { echo "FATAL: 新コミット ${COMMIT:0:8} が現行本番 ${LIVE_COMMIT:0:8} を含まない（巻き戻しになる）"; exit 1; }
  DEPLOYS="$(aws ecs describe-services --cluster "$CLUSTER" --services teamagent-dev-mcp --query 'length(services[0].deployments)' --output text)"
  [ "$DEPLOYS" = "1" ] || { echo "FATAL: mcp がデプロイ途中（deployments=${DEPLOYS}）。落ち着いてから再実行"; exit 1; }
else
  # 09-24 レビュー D-01: --from=2/3 は ① 済みが前提。稼働中の mcp が渡された core でなければ、取り違えた digest で
  # consumer と media だけを差し替えてしまうので止める
  [ "$LIVE_CORE" = "$CORE" ] || { echo "FATAL: --from=${FROM} だが稼働中の mcp（${MCP_TD##*/}）は ${LIVE_CORE:7:12}…＝渡された core ではない"; exit 1; }
fi
echo "  commit ${COMMIT:0:8} / core ${CORE:7:12}… / media ${MEDIA:7:12}…（現行 mcp ${MCP_TD##*/}・Gemini ${LIVE_MODEL}@${LIVE_LOC} は変えない）"

if [ "$FROM" -le 1 ]; then
  echo; echo "=== ① mcp（image のみ・env は継承）"
  bash "$TOOLS/mcp-release/register_mcp_td_image_env.sh" "$CORE"
fi
if [ "$FROM" -ge 2 ]; then
  # 09-24 レビュー D-02: ① が update-service の後で落ちると OC bounce が抜ける。再開時は OC の最新デプロイが
  # mcp の PRIMARY より古ければ bounce する（ツール一覧を新しい mcp から取り直させる）
  MCP_AT="$(aws ecs describe-services --cluster "$CLUSTER" --services teamagent-dev-mcp --query 'services[0].deployments[?status==`PRIMARY`] | [0].createdAt' --output text)"
  OC_AT="$(aws ecs describe-services --cluster "$CLUSTER" --services teamagent-dev-openclaw --query 'services[0].deployments[?status==`PRIMARY`] | [0].createdAt' --output text)"
  if python3 -c 'import sys,datetime as d; f=lambda s: d.datetime.fromisoformat(s); sys.exit(0 if f(sys.argv[1]) < f(sys.argv[2]) else 1)' "$OC_AT" "$MCP_AT"; then
    echo "  OC の最新デプロイ（${OC_AT}）が mcp（${MCP_AT}）より古い → OC bounce"
    aws --profile "$RELEASE_PROFILE" ecs update-service --cluster "$CLUSTER" --service teamagent-dev-openclaw --force-new-deployment --query 'service.taskDefinition' --output text >/dev/null
    aws ecs wait services-stable --cluster "$CLUSTER" --services teamagent-dev-openclaw
  fi
fi
if [ "$FROM" -le 2 ]; then
  echo; echo "=== ② consumers 4 つ（image のみ）"
  bash "$A/swap_mcp_consumers_td.sh" "$CORE"
fi
if [ "$FROM" -le 3 ]; then
  echo; echo "=== ③ media（tiktok-acquire）"
  CUR_ARN="$(aws lambda get-function-configuration --function-name teamagent-dev-tiktok-acquire-dispatch --query 'Environment.Variables.TASKDEF_ARN' --output text)"
  CUR_MEDIA="$(aws ecs describe-task-definition --task-definition "$CUR_ARN" --query 'taskDefinition.containerDefinitions[0].image' --output text | sed 's/.*@//')"
  if [ "$CUR_MEDIA" = "$MEDIA" ]; then
    echo "  済み（dispatch 先 ${CUR_ARN##*/} が新 media）"
  else
    bash "$TOOLS/media-release/deploy_media_td.sh" "$MEDIA"
  fi
fi

echo; echo "=== 最終確認（本番 6 か所）"
EXPECT_MODEL="$LIVE_MODEL" EXPECT_LOC="$LIVE_LOC" bash "$A/verify_r28_live.sh" "$CORE" "$MEDIA"
echo "🏁 mcp 便の本番適用 完了（commit ${COMMIT:0:8}・Gemini ${LIVE_MODEL}@${LIVE_LOC} のまま）。次: DM で実機確認"
