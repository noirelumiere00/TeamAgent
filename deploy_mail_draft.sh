#!/usr/bin/env bash
# 「下書きを作成」ボタンを動かす — mail_draft 配線を dev に反映する使い捨てデプロイ。
#
# やること:
#   1) 結合ソース zip を作る = poc worktree の全ソース(未コミットWIP込み・search.dedup等)
#      ＋ 私の mail_draft 変更(skill/factory/SOUL/openclaw.config) を overlay。
#      ※ fargate.tf は poc の WIP を尊重し overlay しない（env は下の task def 側で注入）。
#   2) CodeBuild で MCP イメージ と OpenClaw イメージ を arm64 ビルド（OpenClaw は buildspec-override）。
#   3) MCP task def 新 rev（新イメージ＋USE_MAIL_DRAFT_TOOL=true）／OpenClaw task def 新 rev（新イメージ）。
#   4) teamagent-dev-mcp / teamagent-dev-openclaw を update-service（ローリング）。
#
# 影響: dev の AiLa（パイロット利用者含む）に反映される。末尾にロールバック手順あり。
# 使い方:  bash deploy_mail_draft.sh
set -euo pipefail
export AWS_REGION=ap-northeast-1

ACCT=718959508629
REG=$ACCT.dkr.ecr.ap-northeast-1.amazonaws.com
PROJECT=teamagent-dev-image-builder
BUCKET=teamagent-dev-raw-files
CLUSTER=teamagent-dev
POC=/Users/s-komata/Documents/teamagent-orchestrator-poc
MINE=/Users/s-komata/Documents/teamagent-mail-draft-handler
TAG="maildraft-$(date +%s)"
# ロールバック基準（現行）
MCP_BASE_REV=32     # teamagent-dev-mcp:32
OC_BASE_REV=21      # teamagent-dev-openclaw:21

echo "== 1) 結合ソース zip =="
TMPZIP=/tmp/maildraft_src_$$.zip; rm -f "$TMPZIP"
( cd "$POC" && { git ls-files; git ls-files --others --exclude-standard; } | zip -q "$TMPZIP" -@ )
( cd "$MINE" && zip -q "$TMPZIP" \
    src/teamagent/skills/mail_draft/__init__.py \
    src/teamagent/skills/mail_draft/schema.py \
    src/teamagent/skills/mail_draft/skill.py \
    tests/skills/mail_draft/__init__.py \
    tests/skills/mail_draft/test_mail_draft.py \
    src/teamagent/orchestrator/factory.py \
    infra/openclaw/SOUL.md \
    infra/openclaw/openclaw.config.json5 )
echo "   zip=$TMPZIP ($(du -h "$TMPZIP"|cut -f1))  tag=$TAG"
aws s3 cp "$TMPZIP" "s3://$BUCKET/codebuild/source.zip"

run_build () {  # $1=説明 $2..=start-build追加引数。BUILD_ID をechoし完了待ち。
  local desc="$1"; shift
  local bid
  bid=$(aws codebuild start-build --project-name "$PROJECT" \
    --environment-variables-override "name=IMAGE_TAG,value=$TAG,type=PLAINTEXT" \
      "name=WITH_SCRAPE_TOOLS,value=false,type=PLAINTEXT" \
      "name=GIT_COMMIT,value=$TAG,type=PLAINTEXT" "name=GIT_BRANCH,value=feat/mail-draft-handler,type=PLAINTEXT" \
    "$@" --query 'build.id' --output text)
  echo "   $desc build=$bid"; echo -n "   待機"
  while true; do
    local st; st=$(aws codebuild batch-get-builds --ids "$bid" --query 'builds[0].buildStatus' --output text)
    echo -n " .$st"; [ "$st" = "SUCCEEDED" ] && { echo; break; }
    [ "$st" != "IN_PROGRESS" ] && { echo; echo "   ❌ $desc build $st"; exit 1; }
    sleep 20
  done
}

echo "== 2a) MCP イメージ（標準 buildspec）=="
run_build "MCP"

echo "== 2b) OpenClaw イメージ（buildspec-override で Dockerfile.openclaw）=="
OC_SPEC='version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $ECR_REGISTRY
  build:
    commands:
      - docker build -f infra/docker/Dockerfile.openclaw --build-arg GIT_COMMIT=$GIT_COMMIT --build-arg GIT_BRANCH=$GIT_BRANCH -t $OC_REPO:$IMAGE_TAG .
  post_build:
    commands:
      - docker push $OC_REPO:$IMAGE_TAG'
run_build "OpenClaw" --buildspec-override "$OC_SPEC"

echo "== 3) 新イメージ digest 取得 =="
MCP_DIGEST=$(aws ecr describe-images --repository-name teamagent-mcp      --image-ids imageTag=$TAG --query 'imageDetails[0].imageDigest' --output text)
OC_DIGEST=$(aws ecr describe-images --repository-name teamagent-openclaw --image-ids imageTag=$TAG --query 'imageDetails[0].imageDigest' --output text)
MCP_IMG="$REG/teamagent-mcp@$MCP_DIGEST"; OC_IMG="$REG/teamagent-openclaw@$OC_DIGEST"
echo "   MCP=$MCP_IMG"; echo "   OC =$OC_IMG"

echo "== 4) MCP task def 新 rev（image 差し替え＋USE_MAIL_DRAFT_TOOL=true）=="
aws ecs describe-task-definition --task-definition "teamagent-dev-mcp:$MCP_BASE_REV" --query 'taskDefinition' --output json > /tmp/td_mcp.json
python3 - "$MCP_IMG" <<'PY'
import json
td=json.load(open('/tmp/td_mcp.json'))
allowed={'family','taskRoleArn','executionRoleArn','networkMode','containerDefinitions','volumes',
 'placementConstraints','requiresCompatibilities','cpu','memory','tags','pidMode','ipcMode',
 'proxyConfiguration','inferenceAccelerators','ephemeralStorage','runtimePlatform','enableFaultInjection'}
td={k:v for k,v in td.items() if k in allowed}
c=td['containerDefinitions'][0]; c['image']=__import__('sys').argv[1]
env=c.setdefault('environment',[])
if not any(e.get('name')=='USE_MAIL_DRAFT_TOOL' for e in env):
    env.append({'name':'USE_MAIL_DRAFT_TOOL','value':'true'})
json.dump(td,open('/tmp/td_mcp_new.json','w'))
PY
MCP_NEW=$(aws ecs register-task-definition --cli-input-json file:///tmp/td_mcp_new.json --query 'taskDefinition.taskDefinitionArn' --output text)
echo "   new MCP td = $MCP_NEW"

echo "== 4b) OpenClaw task def 新 rev（image 差し替え）=="
aws ecs describe-task-definition --task-definition "teamagent-dev-openclaw:$OC_BASE_REV" --query 'taskDefinition' --output json > /tmp/td_oc.json
python3 - "$OC_IMG" <<'PY'
import json,sys
td=json.load(open('/tmp/td_oc.json'))
allowed={'family','taskRoleArn','executionRoleArn','networkMode','containerDefinitions','volumes',
 'placementConstraints','requiresCompatibilities','cpu','memory','tags','pidMode','ipcMode',
 'proxyConfiguration','inferenceAccelerators','ephemeralStorage','runtimePlatform','enableFaultInjection'}
td={k:v for k,v in td.items() if k in allowed}
td['containerDefinitions'][0]['image']=sys.argv[1]
json.dump(td,open('/tmp/td_oc_new.json','w'))
PY
OC_NEW=$(aws ecs register-task-definition --cli-input-json file:///tmp/td_oc_new.json --query 'taskDefinition.taskDefinitionArn' --output text)
echo "   new OpenClaw td = $OC_NEW"

echo "== 5) サービス更新（ローリング）=="
aws ecs update-service --cluster "$CLUSTER" --service teamagent-dev-mcp      --task-definition "$MCP_NEW" --query 'service.serviceName' --output text
aws ecs update-service --cluster "$CLUSTER" --service teamagent-dev-openclaw --task-definition "$OC_NEW"  --query 'service.serviceName' --output text
rm -f "$TMPZIP" /tmp/td_mcp*.json /tmp/td_oc*.json

cat <<EOF

✅ 反映開始（ローリング）。1-3分で RUNNING。確認:
   aws ecs describe-services --cluster $CLUSTER --services teamagent-dev-mcp teamagent-dev-openclaw \\
     --query 'services[].deployments[0].{svc:serviceName,status:rolloutState,running:runningCount}' --output table
   AWS_REGION=ap-northeast-1 aws logs tail /teamagent/dev/openclaw --since 3m --follow   # 押下後の interaction/mail_draft 処理を見る

▶ E2E: Slack の朝ダイジェストで「✏️下書きを作成」を押す → 数秒で Reply-All 下書きが Gmail に作成され
   AiLa が「✅作成しました → 📨Gmailで開く」を返信すれば成功。
   （古いダイジェストの token は失効し得るので、必要なら新しい digest を出してから押す）

⏪ ロールバック（元に戻す）:
   aws ecs update-service --cluster $CLUSTER --service teamagent-dev-mcp      --task-definition teamagent-dev-mcp:$MCP_BASE_REV
   aws ecs update-service --cluster $CLUSTER --service teamagent-dev-openclaw --task-definition teamagent-dev-openclaw:$OC_BASE_REV
EOF
