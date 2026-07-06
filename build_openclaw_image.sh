#!/usr/bin/env bash
# openclaw 外殻イメージを dev 作業ツリーから CodeBuild で arm64 ビルドして openclaw ECR に push する。
# Dockerfile.openclaw:30 が infra/openclaw/openclaw.config.json5 をビルド時に焼き込む＝toolFilter 変更が反映される。
# 出力の OPENCLAW_IMAGE=...@sha256:... を `bash infra/terraform/apply_openclaw.sh <それ>` に渡してデプロイ。
set -euo pipefail
cd "$(dirname "$0")"
export AWS_REGION=ap-northeast-1
PROJECT=teamagent-dev-image-builder
BUCKET=teamagent-dev-raw-files
OC_REPO="718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-openclaw"
TAG="dev-$(date +%Y%m%d-%H%M%S)" # dev 基点を明示（feature枝焼きを止める）

echo "== 1) 作業ツリーを zip → S3 =="
TMPZIP="/tmp/oc_src_$$.zip"
rm -f "$TMPZIP"
{ git ls-files; git ls-files --others --exclude-standard; } | zip -q "$TMPZIP" -@
echo "   zip=$TMPZIP ($(du -h "$TMPZIP" | cut -f1))"
aws s3 cp "$TMPZIP" "s3://$BUCKET/codebuild/openclaw-source.zip"

echo "== 2) CodeBuild で openclaw を arm64 ビルド（buildspec-override・~6-8分）=="
read -r -d '' BUILDSPEC <<'YAML' || true
version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $ECR_REGISTRY
  build:
    commands:
      - echo "Building openclaw ($IMAGE_TAG) arm64 on $(uname -m)"
      - docker build -f infra/docker/Dockerfile.openclaw --build-arg GIT_COMMIT="$GIT_COMMIT" --build-arg GIT_BRANCH="$GIT_BRANCH" -t $OC_REPO:$IMAGE_TAG .
      - docker push $OC_REPO:$IMAGE_TAG
  post_build:
    commands:
      - aws ecr describe-images --repository-name teamagent-openclaw --image-ids imageTag=$IMAGE_TAG --query 'imageDetails[0].imageDigest' --output text | tee /tmp/d.txt
      - echo "OC_DIGEST=$(cat /tmp/d.txt)"
YAML

GIT_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo working-tree)
GIT_BRANCH=$(git branch --show-current 2>/dev/null || true); GIT_BRANCH=${GIT_BRANCH:-unknown}
BUILD_ID=$(aws codebuild start-build --project-name "$PROJECT" \
  --source-type-override S3 \
  --source-location-override "$BUCKET/codebuild/openclaw-source.zip" \
  --buildspec-override "$BUILDSPEC" \
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
echo
echo "   ✅ build SUCCEEDED"

DIGEST=$(aws ecr describe-images --repository-name teamagent-openclaw --image-ids imageTag="$TAG" --query 'imageDetails[0].imageDigest' --output text)
echo
echo "OPENCLAW_IMAGE=$OC_REPO@$DIGEST"
echo "TAG=$TAG"
echo "→ 次: bash infra/terraform/apply_openclaw.sh $OC_REPO@$DIGEST"
rm -f "$TMPZIP"
