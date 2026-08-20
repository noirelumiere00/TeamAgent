# Runbook: State Rebind（PR2-A0.4 — Terraform state binding の live 追従）

**正式契約**: `AWS managed application resources mutation = 0 / Terraform remote state mutation only`

## いつ使うか

```
approved release receipt == live（本番は正しい）
Terraform state の binding だけが旧 revision を指す
```

このとき **live を再デプロイして state へ合わせてはならない**（rolling restart は順序が逆）。
state の binding だけを live の exact task-definition revision へ付け替える。

同一アドレスの rebind は removed/import ブロックでは表現できない（removed は config 不在を、
import は config 存在を要求し矛盾する）。よって `state rm → 即 import` を guard 監督下の
唯一の経路として使う。**素の `terraform state rm` / `terraform import` は引き続き禁止。**

## 前提（順序厳守）

1. **PRODUCTION DEPLOYMENT FREEZE** を宣言してから mapping を確定する。freeze 対象:
   - ECS task definition の登録 / service の task-definition 更新
   - 承認済み 8 flag の変更
   - deployment pipeline / admin 手動デプロイ
   - taskdef ARN を参照する Lambda env の変更
   - rebind 対象 consumer の参照先変更
   （generation publisher freeze は継続。無関係な AWS 変更まで止める必要はない）
2. freeze 後に **6 consumer の live ARN を fresh に再解決**し、
   `infra/deploy/state_rebind_targets.json` の `targets` を確定して merge する。
   調査時点の ARN を焼かない（2026-08-20 に調査中 `mcp:86` が増えた実例）。
3. 実行 session は可能なら **application write を絞った一時 session** を使う（下記）。

## 実行 session の制限（推奨）

trusted automation role を assume する際に **session policy** で許可を絞る。
session policy は権限の交差にしか働かないため、ここに無い操作は role が許可していても拒否される:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "StateBackend", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
     "Resource": ["arn:aws:s3:::teamagent-tfstate-718959508629", "arn:aws:s3:::teamagent-tfstate-718959508629/teamagent/terraform.tfstate"]},
    {"Sid": "BackendLock", "Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"],
     "Resource": "arn:aws:dynamodb:ap-northeast-1:718959508629:table/teamagent-tflock"},
    {"Sid": "ReadOnlyVerify", "Effect": "Allow",
     "Action": ["ecs:DescribeTaskDefinition", "ecs:DescribeServices", "events:ListTargetsByRule", "lambda:GetFunctionConfiguration", "sts:GetCallerIdentity", "kms:Decrypt", "kms:DescribeKey"],
     "Resource": "*"},
    {"Sid": "DenyApplicationWrites", "Effect": "Deny",
     "Action": ["ecs:RegisterTaskDefinition", "ecs:DeregisterTaskDefinition", "ecs:UpdateService", "lambda:UpdateFunctionConfiguration", "events:PutTargets", "events:PutRule"],
     "Resource": "*"}
  ]
}
```

```bash
aws sts assume-role \
  --role-arn arn:aws:iam::718959508629:role/teamagent-dev-terraform-runtime-automation \
  --role-session-name teamagent-terraform-worker \
  --policy file:///secure/path/rebind-session-policy.json \
  --duration-seconds 3600
```

使用した session policy の JSON と SHA256 を evidence として out ディレクトリへ保存すること
（session policy の適用有無はサーバ側から検証できないため、evidence で補う）。

## 手順（two-phase・すべて guard 経由）

```bash
# Phase 1: precheck（read-only。state backup / mapping と live の一致 / binding 記録）
bash infra/deploy/terraform_runtime_guard.sh state-rebind-precheck --out /secure/path/rebind-...

# 人間レビュー: rebind-plan.tsv（address / 現 state ARN / rebind 先 ARN）と binding を確認
# → 出力された承認トークン（precheck binding に束縛）を控える

# Phase 2: apply（human gate 後のみ）
bash infra/deploy/terraform_runtime_guard.sh state-rebind-apply \
  --out /secure/path/rebind-... \
  --var-file <0600 の terraform.tfvars> \
  --approve "I-HAVE-REVIEWED-THE-STATE-REBIND:<binding sha256 先頭16桁>"
```

apply は 1 address ずつ atomic-like に進む:

```
consumer 参照の直前再検証（動いていれば STALE MAPPING で停止）
→ state rm → 即 import → state と live DescribeTaskDefinition の機械比較 → 次へ
```

- **一括 rm は構造的に不可能**（テストが per-address 順序を固定している）
- 失敗した場合は**次の resource へ進まず停止**する。deployment lock は TTL まで残り、
  復旧判断まで他の guard 操作を塞ぐ（意図された fail-closed）
- 復旧は human 裁定: `state-backup.json`（out ディレクトリ・0600）からの復元を第一候補とする

## 完了後

- 直後の guarded plan で rebind 対象が **no-op** であることを確認（これが成功の定義）
- evidence 一式（binding / rebind-plan.tsv / per-address の describe と state 比較結果 /
  session policy）を保全し、freeze は activation 全体の完了まで解除しない
