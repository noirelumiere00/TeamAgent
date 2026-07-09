#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# 一度きり・冪等: exec ロールに Slack OAuth secret の読取(GetSecretValue)を付与。
#   これが無いと、Slack secrets を参照する統合タスク定義が起動時に AccessDenied →
#   ECS が自動ロールバックする。deploy_launch.sh step1 と同一内容を切り出したもの。
#   put-role-policy は冪等（同名 policy を上書き）なので何度実行しても安全。
#   ※terraform 非経由（ドリフト回避）。将来 tf 側に取り込む時は connect_web.tf / fargate.tf へ。
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
CID_ARN="arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/connect_slack_client_id-aTZTb2"
CSEC_ARN="arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/connect_slack_secret-fOlJIt"
CSTATE_ARN="arn:aws:secretsmanager:ap-northeast-1:718959508629:secret:teamagent/dev/slack_oauth_state_secret-yGYkUF"

echo "== mcp exec role: CID + STATE の2本 =="
aws iam put-role-policy --role-name teamagent-dev-ecs-exec-mcp --policy-name slack-oauth-secrets \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",\"Resource\":[\"$CID_ARN\",\"$CSTATE_ARN\"]}]}"
echo "  OK"

echo "== connect-web exec role: CID + SECRET + STATE の3本 =="
aws iam put-role-policy --role-name teamagent-dev-ecs-exec-connect-web --policy-name slack-oauth-secrets \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"secretsmanager:GetSecretValue\",\"Resource\":[\"$CID_ARN\",\"$CSEC_ARN\",\"$CSTATE_ARN\"]}]}"
echo "  OK"
echo "✅ 完了（冪等・以後不要）。"
