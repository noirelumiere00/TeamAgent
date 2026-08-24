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

## 実行 session の制限（必須）

trusted automation role を assume する際に **session policy** で許可を絞る。
session policy は権限の交差にしか働かないため、ここに無い操作は role が許可していても拒否される。

**canonical policy は repo にコミットされた 1 ファイルだけ**:
`infra/deploy/state_rebind_session_policy.json`
（内容は `state_rebind.py` の least-privilege 契約と契約テストが exact に固定している。
手元コピーの改変や別 JSON の代用は禁止。）

```bash
# 1. 契約検証 + sha256 採取（evidence）
python3 infra/deploy/state_rebind.py validate-session-policy

# 2. assume-role（session 名は trust policy が固定値で要求）
aws sts assume-role \
  --role-arn arn:aws:iam::718959508629:role/teamagent-dev-terraform-runtime-automation \
  --role-session-name teamagent-terraform-worker \
  --policy file://infra/deploy/state_rebind_session_policy.json \
  --duration-seconds 3600
```

使用した session policy の JSON と SHA256 を evidence として out ディレクトリへ保存すること
（session policy の適用有無はサーバ側から検証できないため、evidence で補う）。

### 許可 read の由来（2026-08-21 実測。推測での追加は禁止）

| 区分 | action | 根拠 |
|---|---|---|
| v1 実績 | s3/dynamodb backend 6種 + ecs/events/lambda/sts/kms の検証読み 7種 | 初版 policy 下で rm・backend・consumer 検証が実際に使用 |
| 403 実測 | ec2:DescribeImages / ec2:DescribeVpcs / iam:GetUser / kms:ListAliases / secretsmanager:DescribeSecret | 読み取り列挙が不足した初版 policy 下で `terraform import` が AccessDenied で失敗した実ログ（rm 済み・import 未完の中間 state を作った事故） |
| 静的導出 | ec2:DescribeSubnets / ec2:DescribeRouteTables | `data.aws_vpc.default.id` に依存する第二波 data source。第一波の 403 で評価に到達しなかっただけで、config 上 import 時評価が確定している |

`terraform import` は対象 resource だけでなく **root module の全 data source を評価する**。
新しい data source を config に足したら、この policy と `state_rebind.py` の契約表の
両方を更新しない限り rebind は 403 で止まる（fail-closed）。

### 禁止事項（2026-08-21 ユーザー裁定）

- **`Allow *` + Deny 型（復旧時の暫定 v2）を本番 operation で再利用しないこと。**
  base role の「Deny に書き忘れた write」がそのまま残る形であり、成功実績として
  evidence に残っているだけの過去の形。契約テストが `Allow *` の混入を拒否する。
- 次の本番使用前に、可能なら read-only の dry-run で policy の十分性を再確認する
  （403 実測リストは「第一波で観測できた分」であり、config が変われば必要 read も変わる）。

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
