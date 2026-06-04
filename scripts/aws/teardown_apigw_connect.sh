#!/usr/bin/env bash
# =============================================================================
# deploy_apigw_connect.sh で作った AWS リソースを逆順で削除する。
# /tmp/apigw_ids.env（deploy が出力）を読む。worker 本体と worker SG は残す
# （worker SG に足した 8788<-ALB の ingress ルールのみ revoke）。
# =============================================================================
set -uo pipefail
export AWS_PAGER=""
REGION=ap-northeast-1
WORKER_SG=sg-05ca9c6a30f8dfe51
IDS=${1:-/tmp/apigw_ids.env}
[ -f "$IDS" ] || { echo "IDS file not found: $IDS"; exit 1; }
# shellcheck disable=SC1090
source "$IDS"
echo "tearing down: API_ID=$API_ID ALB_ARN=$ALB_ARN VPCLINK_ID=$VPCLINK_ID"

for RK in 'GET /oauth2/callback' 'GET /healthz' '$default'; do
  RID=$(aws apigatewayv2 get-routes --region $REGION --api-id "$API_ID" --query "Items[?RouteKey=='$RK'].RouteId" --output text 2>/dev/null)
  [ -n "${RID:-}" ] && aws apigatewayv2 delete-route --region $REGION --api-id "$API_ID" --route-id "$RID"
done
aws apigatewayv2 delete-stage --region $REGION --api-id "$API_ID" --stage-name '$default' 2>/dev/null
aws apigatewayv2 delete-integration --region $REGION --api-id "$API_ID" --integration-id "$INTEG_ID" 2>/dev/null
aws apigatewayv2 delete-api --region $REGION --api-id "$API_ID" 2>/dev/null
aws apigatewayv2 delete-vpc-link --region $REGION --vpc-link-id "$VPCLINK_ID" 2>/dev/null

aws elbv2 delete-listener --region $REGION --listener-arn "$LISTENER_ARN" 2>/dev/null
aws elbv2 delete-load-balancer --region $REGION --load-balancer-arn "$ALB_ARN" 2>/dev/null
aws elbv2 wait load-balancers-deleted --region $REGION --load-balancer-arns "$ALB_ARN" 2>/dev/null
aws elbv2 deregister-targets --region $REGION --target-group-arn "$TG_ARN" --targets Id=i-0feaa3c103ab6ef91,Port=8788 2>/dev/null
aws elbv2 delete-target-group --region $REGION --target-group-arn "$TG_ARN" 2>/dev/null

aws ec2 revoke-security-group-ingress --region $REGION --group-id $WORKER_SG \
  --ip-permissions IpProtocol=tcp,FromPort=8788,ToPort=8788,UserIdGroupPairs="[{GroupId=$ALB_SG}]" 2>/dev/null
aws ec2 revoke-security-group-ingress --region $REGION --group-id "$ALB_SG" \
  --ip-permissions IpProtocol=tcp,FromPort=80,ToPort=80,UserIdGroupPairs="[{GroupId=$VPCLINK_SG}]" 2>/dev/null
# VPC Link の完全削除待ち（SG 依存解消）後に SG 削除。失敗したら数分後に再実行。
for i in $(seq 1 20); do
  ST=$(aws apigatewayv2 get-vpc-link --region $REGION --vpc-link-id "$VPCLINK_ID" --query VpcLinkStatus --output text 2>/dev/null || echo "GONE")
  [ "$ST" = "GONE" ] && break; echo "  vpclink delete: $ST"; sleep 15
done
aws ec2 delete-security-group --region $REGION --group-id "$VPCLINK_SG" 2>/dev/null
aws ec2 delete-security-group --region $REGION --group-id "$ALB_SG" 2>/dev/null
echo "teardown done"
