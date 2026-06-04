#!/usr/bin/env bash
# =============================================================================
# connect_web を「安定した公開HTTPS URL」で出すための AWS インフラ構築。
# 設計: API Gateway(HTTP API, $default stage) → VPC Link → 内部ALB → worker:8788
#
# 作るもの（account 718959508629 / ap-northeast-1）:
#   - SG x2          : teamagent-connectweb-alb-sg / -vpclink-sg（新規）
#   - SG ルール x2    : alb-sg<-80(vpclink-sg) / worker-sg<-8788(alb-sg のみ＝非公開)
#   - Target Group   : HTTP:8788, health=GET /healthz, 200
#   - 内部ALB        : 3AZ, scheme=internal（インターネット非公開）＋ HTTP:80 listener
#   - VPC Link       : 3サブネット, vpclink-sg
#   - HTTP API       : $default stage(auto-deploy) → HTTP_PROXY(VPC_LINK)→ALB listener
#   - routes         : GET /oauth2/callback, GET /healthz, $default
#
# 出力: 安定URL https://<API_ID>.execute-api.ap-northeast-1.amazonaws.com
#       Google 登録用 redirect_uri = <URL>/oauth2/callback
# コスト: ≈ $18/月（ALB 時間課金が主。API GW/VPC Link はほぼ$0）
# 削除:  scripts/aws/teardown_apigw_connect.sh （/tmp/apigw_ids.env を読む）
#
# 前提: connect_web が worker(i-0feaa3c103ab6ef91) の 0.0.0.0:8788 で稼働中であること。
# 安全: worker は 8788 を「ALB SG からのみ」許可。インターネットには一切公開しない。
# =============================================================================
set -uo pipefail
export AWS_PAGER=""
REGION=ap-northeast-1
VPC_ID=vpc-06c091b5d5f771227
WORKER_ID=i-0feaa3c103ab6ef91
WORKER_SG=sg-05ca9c6a30f8dfe51
APP_PORT=8788
SUBNET_D=subnet-07e0d4e58b3b83b8a   # ap-northeast-1d (worker)
SUBNET_C=subnet-0d87f3016e96101a5   # ap-northeast-1c
SUBNET_A=subnet-0c5982c60d38557ce   # ap-northeast-1a
IDS=/tmp/apigw_ids.env

echo "### 1) security groups"
ALB_SG=$(aws ec2 create-security-group --region $REGION --group-name teamagent-connectweb-alb-sg \
  --description "Internal ALB SG for connect_web" --vpc-id $VPC_ID --query GroupId --output text) || exit 11
echo "ALB_SG=$ALB_SG"
VPCLINK_SG=$(aws ec2 create-security-group --region $REGION --group-name teamagent-connectweb-vpclink-sg \
  --description "API GW VPC Link SG for connect_web" --vpc-id $VPC_ID --query GroupId --output text) || exit 12
echo "VPCLINK_SG=$VPCLINK_SG"
aws ec2 authorize-security-group-ingress --region $REGION --group-id $ALB_SG \
  --ip-permissions IpProtocol=tcp,FromPort=80,ToPort=80,UserIdGroupPairs="[{GroupId=$VPCLINK_SG,Description=from-vpclink}]" >/dev/null || exit 13
aws ec2 authorize-security-group-ingress --region $REGION --group-id $WORKER_SG \
  --ip-permissions IpProtocol=tcp,FromPort=$APP_PORT,ToPort=$APP_PORT,UserIdGroupPairs="[{GroupId=$ALB_SG,Description=from-internal-alb}]" >/dev/null || exit 14
echo "SG rules added (worker:8788 <- ALB SG only; no internet)"

echo "### 2) target group + register worker:8788"
TG_ARN=$(aws elbv2 create-target-group --region $REGION --name teamagent-connectweb-tg \
  --protocol HTTP --port $APP_PORT --vpc-id $VPC_ID --target-type instance \
  --health-check-protocol HTTP --health-check-port traffic-port --health-check-path /healthz \
  --matcher HttpCode=200 --health-check-interval-seconds 30 \
  --healthy-threshold-count 2 --unhealthy-threshold-count 2 \
  --query 'TargetGroups[0].TargetGroupArn' --output text) || exit 21
echo "TG_ARN=$TG_ARN"
aws elbv2 register-targets --region $REGION --target-group-arn $TG_ARN --targets Id=$WORKER_ID,Port=$APP_PORT || exit 22

echo "### 3) internal ALB (3AZ) + HTTP:80 listener"
ALB_ARN=$(aws elbv2 create-load-balancer --region $REGION --name teamagent-connectweb-alb \
  --type application --scheme internal --ip-address-type ipv4 \
  --subnets $SUBNET_D $SUBNET_C $SUBNET_A --security-groups $ALB_SG \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text) || exit 31
echo "ALB_ARN=$ALB_ARN"
aws elbv2 wait load-balancer-available --region $REGION --load-balancer-arns $ALB_ARN || exit 32
LISTENER_ARN=$(aws elbv2 create-listener --region $REGION --load-balancer-arn $ALB_ARN \
  --protocol HTTP --port 80 --default-actions Type=forward,TargetGroupArn=$TG_ARN \
  --query 'Listeners[0].ListenerArn' --output text) || exit 33
echo "LISTENER_ARN=$LISTENER_ARN"

echo "### 4) VPC Link (poll AVAILABLE ~2-3min)"
VPCLINK_ID=$(aws apigatewayv2 create-vpc-link --region $REGION --name teamagent-connectweb-vpclink \
  --subnet-ids $SUBNET_D $SUBNET_C $SUBNET_A --security-group-ids $VPCLINK_SG \
  --query VpcLinkId --output text) || exit 41
echo "VPCLINK_ID=$VPCLINK_ID"
for i in $(seq 1 24); do
  ST=$(aws apigatewayv2 get-vpc-link --region $REGION --vpc-link-id $VPCLINK_ID --query VpcLinkStatus --output text 2>/dev/null)
  echo "  vpclink: $ST"; [ "$ST" = "AVAILABLE" ] && break; sleep 15
done

echo "### 5) HTTP API + integration(HTTP_PROXY/VPC_LINK) + routes + \$default stage"
API_ID=$(aws apigatewayv2 create-api --region $REGION --name teamagent-connectweb-api \
  --protocol-type HTTP --query ApiId --output text) || exit 51
echo "API_ID=$API_ID"
INTEG_ID=$(aws apigatewayv2 create-integration --region $REGION --api-id $API_ID \
  --integration-type HTTP_PROXY --integration-method ANY --integration-uri $LISTENER_ARN \
  --connection-type VPC_LINK --connection-id $VPCLINK_ID --payload-format-version 1.0 \
  --query IntegrationId --output text) || exit 52
echo "INTEG_ID=$INTEG_ID"
aws apigatewayv2 create-route --region $REGION --api-id $API_ID --route-key 'GET /oauth2/callback' --target "integrations/$INTEG_ID" >/dev/null || exit 53
aws apigatewayv2 create-route --region $REGION --api-id $API_ID --route-key 'GET /healthz'         --target "integrations/$INTEG_ID" >/dev/null || exit 54
aws apigatewayv2 create-route --region $REGION --api-id $API_ID --route-key '$default'             --target "integrations/$INTEG_ID" >/dev/null || exit 55
aws apigatewayv2 create-stage --region $REGION --api-id $API_ID --stage-name '$default' --auto-deploy >/dev/null || exit 56
echo "routes + stage created"

INVOKE_URL=$(aws apigatewayv2 get-api --region $REGION --api-id $API_ID --query ApiEndpoint --output text)
cat > "$IDS" <<EOF
ALB_SG=$ALB_SG
VPCLINK_SG=$VPCLINK_SG
TG_ARN=$TG_ARN
ALB_ARN=$ALB_ARN
LISTENER_ARN=$LISTENER_ARN
VPCLINK_ID=$VPCLINK_ID
API_ID=$API_ID
INTEG_ID=$INTEG_ID
INVOKE_URL=$INVOKE_URL
EOF

echo "### 6) end-to-end verify (retry for target-health window ~30-90s)"
OK="no"
for i in $(seq 1 16); do
  CODE=$(curl -s -o /dev/null -m 8 -w "%{http_code}" "${INVOKE_URL}/healthz" 2>/dev/null || echo "000")
  echo "  public healthz attempt $i -> $CODE"; [ "$CODE" = "200" ] && { OK="yes"; break; }; sleep 15
done
echo "============ RESULT ============"
echo "INVOKE_URL    = $INVOKE_URL"
echo "REDIRECT_URI  = ${INVOKE_URL}/oauth2/callback   # ← Google Console に登録"
echo "HEALTHZ_OK    = $OK"
echo "TARGET_HEALTH = $(aws elbv2 describe-target-health --region $REGION --target-group-arn $TG_ARN --query 'TargetHealthDescriptions[].TargetHealth.State' --output text 2>/dev/null)"
echo "IDS saved to  : $IDS"
echo "================================"
