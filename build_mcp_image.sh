#!/usr/bin/env bash
# 作業ツリー（資料検索の新コード込み）から mcp イメージをビルドし digest を出すだけ。
# 再取込 run-task / connect-web 用に使う。本番データは触らない。
set -euo pipefail
cd "$(dirname "$0")"
export AWS_REGION=ap-northeast-1
PROJECT=teamagent-dev-image-builder
BUCKET=teamagent-dev-raw-files
TAG="search-$(date +%s)"   # ECR immutable tag のため毎回ユニーク

echo "== 1) working tree を zip → S3 =="
TMPZIP=/tmp/search_src_$$.zip
rm -f "$TMPZIP"
{ git ls-files; git ls-files --others --exclude-standard; } | zip -q "$TMPZIP" -@
echo "   zip=$TMPZIP ($(du -h "$TMPZIP" | cut -f1))"
aws s3 cp "$TMPZIP" "s3://$BUCKET/codebuild/source.zip"

echo "== 2) CodeBuild で mcp ビルド (tag=${TAG}・~8分) =="
GIT_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo working-tree)
GIT_BRANCH=$(git branch --show-current 2>/dev/null || true); GIT_BRANCH=${GIT_BRANCH:-unknown}
BUILD_ID=$(aws codebuild start-build --project-name "$PROJECT" \
  --environment-variables-override \
    "name=IMAGE_TAG,value=$TAG,type=PLAINTEXT" \
    "name=GIT_COMMIT,value=$GIT_COMMIT,type=PLAINTEXT" \
    "name=GIT_BRANCH,value=$GIT_BRANCH,type=PLAINTEXT" \
  --query 'build.id' --output text)
echo "   build=$BUILD_ID"
echo -n "   待機"
while true; do
  ST=$(aws codebuild batch-get-builds --ids "$BUILD_ID" --query 'builds[0].buildStatus' --output text)
  echo -n " .$ST"
  [ "$ST" = "SUCCEEDED" ] && break
  if [ "$ST" != "IN_PROGRESS" ]; then echo; echo "   ❌ build $ST"; exit 1; fi
  sleep 20
done
echo; echo "   ✅ build SUCCEEDED"

DIGEST=$(aws ecr describe-images --repository-name teamagent-mcp \
  --image-ids imageTag="$TAG" --query 'imageDetails[0].imageDigest' --output text)
echo
echo "MCP_IMAGE=718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@$DIGEST"
echo "TAG=$TAG"
rm -f "$TMPZIP"
