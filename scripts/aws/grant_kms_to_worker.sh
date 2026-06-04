#!/usr/bin/env bash
# =============================================================================
# worker IAM ロール(teamagent-dev-worker)の inline ポリシーに、oauth用KMS鍵限定で
# kms:Encrypt/Decrypt/GenerateDataKey を追加する。
#   - connect_web: refresh token を KMS暗号化して RDS 保存（Encrypt/GenerateDataKey）
#   - bot(Phase2b): 各営業の token を復号して本人のGoogleを使う（Decrypt）
# 鍵は alias/teamagent-oauth-tokens（= key/7e41bedb-...）に限定。冪等（既存なら無変更）。
# =============================================================================
set -euo pipefail
export AWS_PAGER=""
ROLE=teamagent-dev-worker
POLICY=teamagent-dev-worker-app
KEY_ARN=arn:aws:kms:ap-northeast-1:718959508629:key/7e41bedb-6980-4a67-a0bc-e1307d798fb4

aws iam get-role-policy --role-name "$ROLE" --policy-name "$POLICY" --query PolicyDocument --output json > /tmp/worker_policy.json
python3 - "$KEY_ARN" <<'PY'
import json, sys
key = sys.argv[1]
doc = json.load(open('/tmp/worker_policy.json'))
sids = [s.get('Sid') for s in doc.get('Statement', [])]
if 'OAuthKms' in sids:
    print('OAuthKms already present; no change')
    json.dump(doc, open('/tmp/worker_policy_new.json', 'w'))
    sys.exit(0)
doc.setdefault('Statement', []).append({
    'Sid': 'OAuthKms',
    'Effect': 'Allow',
    'Action': ['kms:Encrypt', 'kms:Decrypt', 'kms:GenerateDataKey'],
    'Resource': key,
})
json.dump(doc, open('/tmp/worker_policy_new.json', 'w'))
print('added OAuthKms statement (Encrypt/Decrypt/GenerateDataKey scoped to oauth key)')
PY
aws iam put-role-policy --role-name "$ROLE" --policy-name "$POLICY" --policy-document file:///tmp/worker_policy_new.json
echo "applied. current OAuthKms statement:"
aws iam get-role-policy --role-name "$ROLE" --policy-name "$POLICY" \
  --query 'PolicyDocument.Statement[?Sid==`OAuthKms`]' --output json
