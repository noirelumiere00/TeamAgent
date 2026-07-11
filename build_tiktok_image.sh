#!/usr/bin/env bash
# 使い捨て Fargate(tiktok-acquire)用 arm64 イメージを ~/tiktok-data-service から CodeBuild で
# ビルドして tiktok ECR に push する（proxy-free・build_mcp_image.sh の tiktok 版）。
#
# 前提（この順で先に済ませること）:
#   - 手順1: terraform で aws_ecr_repository.tiktok_acquire[0] を作成済（ECR リポジトリが存在）。
#   - codebuild.tf の push 権限に tiktok ECR を追加済＆apply 済（2026-06-26 追記分）。
# 出力の TIKTOK_IMAGE=...@sha256:... を terraform.tfvars の tiktok_acquire_image に入れる。
set -euo pipefail
export AWS_REGION=ap-northeast-1
ACCOUNT=718959508629
PROJECT=teamagent-dev-image-builder
BUCKET=teamagent-dev-raw-files
SRC="${HOME}/tiktok-data-service"
REPO="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/teamagent-dev-tiktok-acquire"
TAG="tk-$(date +%s)" # ECR immutable tag

echo "== 1) ~/tiktok-data-service を zip → S3（node_modules/.git/videos 除外）=="
TMPZIP="/tmp/tiktok_src_$$.zip"
rm -f "$TMPZIP"
( cd "$SRC" && zip -qr "$TMPZIP" . -x '*/node_modules/*' -x '.git/*' -x 'videos/*' -x '*/__pycache__/*' )
echo "   zip=$TMPZIP ($(du -h "$TMPZIP" | cut -f1))"
aws s3 cp "$TMPZIP" "s3://$BUCKET/codebuild/tiktok-source.zip"

echo "== 2) CodeBuild で arm64 ビルド（buildspec-override・~5-8分）=="
read -r -d '' BUILDSPEC <<'YAML' || true
version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $ECR_REGISTRY
  build:
    commands:
      - echo "Building tiktok-acquire ($IMAGE_TAG) arm64"
      - docker build -f Dockerfile.acquire -t $TIKTOK_REPO:$IMAGE_TAG .
      - docker push $TIKTOK_REPO:$IMAGE_TAG
  post_build:
    commands:
      - aws ecr describe-images --repository-name teamagent-dev-tiktok-acquire --image-ids imageTag=$IMAGE_TAG --query 'imageDetails[0].imageDigest' --output text | tee /tmp/d.txt
      - echo "TIKTOK_DIGEST=$(cat /tmp/d.txt)"
YAML

BUILD_ID=$(aws codebuild start-build --project-name "$PROJECT" \
  --source-type-override S3 \
  --source-location-override "$BUCKET/codebuild/tiktok-source.zip" \
  --buildspec-override "$BUILDSPEC" \
  --environment-variables-override \
    "name=IMAGE_TAG,value=$TAG,type=PLAINTEXT" \
    "name=TIKTOK_REPO,value=$REPO,type=PLAINTEXT" \
  --query 'build.id' --output text)
echo "   build=$BUILD_ID"
echo -n "   待機"
while true; do
  ST=$(aws codebuild batch-get-builds --ids "$BUILD_ID" --query 'builds[0].buildStatus' --output text)
  echo -n " .$ST"
  [ "$ST" = "SUCCEEDED" ] && break
  if [ "$ST" != "IN_PROGRESS" ]; then echo; echo "   ❌ build $ST（CloudWatch /aws/codebuild/$PROJECT を確認）"; exit 1; fi
  sleep 20
done
echo
echo "   ✅ build SUCCEEDED"

DIGEST=$(aws ecr describe-images --repository-name teamagent-dev-tiktok-acquire \
  --image-ids imageTag="$TAG" --query 'imageDetails[0].imageDigest' --output text)
echo
echo "TIKTOK_IMAGE=$REPO@$DIGEST"
echo "TAG=$TAG"
echo "→ この TIKTOK_IMAGE を terraform.tfvars の tiktok_acquire_image に設定して手順4へ。"
rm -f "$TMPZIP"
