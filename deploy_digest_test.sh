#!/usr/bin/env bash
# 使い捨て: 新コード(全返信＋スレッド反映の濃い下書き)を mcp イメージにビルドし、
# morning_digest を **s-komata@vectorinc.co.jp 1人だけ** に run-task する。
#   - 既存の mcp サービス／ライブ AiLa／テスター(江畑・久保木)には一切影響しない
#     （ライブは digest 固定・schedule は :11 固定・本 run は別 revision を明示実行）。
#   - working tree をそのまま zip（未コミットの新コード込み・gitignore は除外＝tfvars等は入らない）。
# 使い方:  bash deploy_digest_test.sh
set -euo pipefail
cd "$(dirname "$0")"
export AWS_REGION=ap-northeast-1

ACCT=718959508629
REG=$ACCT.dkr.ecr.ap-northeast-1.amazonaws.com
PROJECT=teamagent-dev-image-builder
BUCKET=teamagent-dev-raw-files
CLUSTER=teamagent-dev
FAMILY=teamagent-dev-morning-digest
BASE_REV=11
ME=s-komata@vectorinc.co.jp
SUBNETS=subnet-0c5982c60d38557ce,subnet-0d87f3016e96101a5,subnet-07e0d4e58b3b83b8a
SG=sg-0bf1b6c2231e90364
TAG="mailrich-$(date +%s)"   # ECR は immutable tag のため毎回ユニーク tag

echo "== 1) working tree を zip → S3 (source.zip) =="
TMPZIP=/tmp/digest_src_$$.zip
rm -f "$TMPZIP"
{ git ls-files; git ls-files --others --exclude-standard; } | zip -q "$TMPZIP" -@
echo "   zip = $TMPZIP ($(du -h "$TMPZIP" | cut -f1))"
aws s3 cp "$TMPZIP" "s3://$BUCKET/codebuild/source.zip"

echo "== 2) CodeBuild で mcp イメージをビルド (arm64・~8分) =="
GIT_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo working-tree)
GIT_BRANCH=$(git branch --show-current 2>/dev/null || true); GIT_BRANCH=${GIT_BRANCH:-unknown}
echo "   image tag = $TAG"
BUILD_ID=$(aws codebuild start-build --project-name "$PROJECT" \
  --environment-variables-override \
    "name=IMAGE_TAG,value=$TAG,type=PLAINTEXT" \
    "name=GIT_COMMIT,value=$GIT_COMMIT,type=PLAINTEXT" \
    "name=GIT_BRANCH,value=$GIT_BRANCH,type=PLAINTEXT" \
  --query 'build.id' --output text)
echo "   build = $BUILD_ID"
echo -n "   待機"
while true; do
  ST=$(aws codebuild batch-get-builds --ids "$BUILD_ID" --query 'builds[0].buildStatus' --output text)
  echo -n " .$ST"
  [ "$ST" = "SUCCEEDED" ] && break
  if [ "$ST" != "IN_PROGRESS" ]; then echo; echo "   ❌ build $ST"; exit 1; fi
  sleep 20
done
echo; echo "   ✅ build SUCCEEDED"

echo "== 3) 新 mcp digest を取得し、digest 用 task def 新 revision を登録 =="
DIGEST=$(aws ecr describe-images --repository-name teamagent-mcp \
  --image-ids imageTag=$TAG --query 'imageDetails[0].imageDigest' --output text)
NEW_IMAGE="$REG/teamagent-mcp@$DIGEST"
echo "   new image = $NEW_IMAGE"
aws ecs describe-task-definition --task-definition "$FAMILY:$BASE_REV" \
  --query 'taskDefinition' --output json > /tmp/td_base.json
python3 - "$NEW_IMAGE" <<'PY'
import json, sys
td = json.load(open('/tmp/td_base.json'))
for k in ('taskDefinitionArn','revision','status','requiresAttributes',
          'compatibilities','registeredAt','registeredBy'):
    td.pop(k, None)
td['containerDefinitions'][0]['image'] = sys.argv[1]
json.dump(td, open('/tmp/td_new.json','w'))
PY
NEWREV=$(aws ecs register-task-definition --cli-input-json file:///tmp/td_new.json \
  --query 'taskDefinition.taskDefinitionArn' --output text)
echo "   new task def = $NEWREV"

echo "== 4) morning_digest を s-komata 1人だけ run-task =="
TASK_ARN=$(aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE \
  --task-definition "$NEWREV" \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --overrides "{\"containerOverrides\":[{\"name\":\"morning-digest\",\"environment\":[{\"name\":\"MORNING_DIGEST_USERS\",\"value\":\"$ME\"}]}]}" \
  --query 'tasks[0].taskArn' --output text)
echo "   ✅ task = $TASK_ARN"
echo
echo "ログ追従:  AWS_REGION=ap-northeast-1 aws logs tail /teamagent/dev/morning-digest --follow --since 2m"
rm -f "$TMPZIP"
