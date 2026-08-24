# Runbook: secret 含有 plan 成果物の取り扱い（PR2-A0.2.2b 以降）

**裁定日**: 2026-08-21 / 2026-08-24（ユーザー裁定）

## なぜ必要か

`aws_secretsmanager_secret_version.db_password`（`infra/terraform/rds.tf`）は
secret の **値** を持つ managed resource で、`terraform plan -refresh=true` は
その値を読む。A0.2.2b で trusted automation role に
`secretsmanager:GetSecretValue`（db_password の exact ARN のみ）を許可した結果、
**plan / `show -json` の成果物が DB パスワードを内包し得る**。

## 規約（例外なし）

| 項目 | 規約 |
|---|---|
| 保存場所 | **repository の外**。guard が `--out` の repo 配下を拒否する |
| 権限 | **0600**（ディレクトリは 0700）。`umask 077` 下で作成する |
| チャット / CI | **生 JSON を出さない**。`show -json` の全文を貼らない・ログへ流さない |
| 検証 | **機械検証**で行う（validator / contract / jq）。人間が全文を目視しない |
| 人間向け | **redacted 要約のみ**（リソース数・action 種別・SHA256 など） |
| 事後 | 作業後に **sensitive 成果物を破棄**する。evidence として残すのは redacted 要約と SHA256 |

## 具体の手順

```bash
OUT=$(mktemp -d)            # repo 外
chmod 700 "$OUT"
umask 077

# plan は保存済みファイルへ。生 JSON は $OUT の外へ出さない
terraform -chdir=infra/terraform plan -var-file=<0600 tfvars> -out="$OUT/a022b.tfplan"
chmod 600 "$OUT/a022b.tfplan"

# 機械検証のみ（全文を表示しない）
terraform -chdir=infra/terraform show -json "$OUT/a022b.tfplan" > "$OUT/plan.json"
chmod 600 "$OUT/plan.json"
python3 <検証スクリプト> --plan "$OUT/plan.json"     # 判定だけを stdout へ

# 人間レビュー用の redacted 要約（SHA256 と件数のみ）
shasum -a 256 "$OUT/a022b.tfplan"
jq -r '[.resource_changes[] | select(.change.actions != ["no-op"]) |
        "\(.change.actions|join(","))\t\(.address)"] | .[]' "$OUT/plan.json"

# 事後
rm -rf "$OUT"
```

`jq` で値そのものを出す操作（`.change.after`, `.prior_state`, `secret_string` 等）を
**人間の目に入る経路へ流さない**。差分の確認は address と action、および
before/after の SHA256 で行う（既存の `terraform_plan_contract.py` は同じ理由で
before/after を SHA256 化している）。

## なぜ kms:Decrypt を足さないか

`teamagent/dev/db_password` の `KmsKeyId` は未設定 = AWS managed key
（`alias/aws/secretsmanager`）。その key policy が
`Principal {"AWS":"*"}` + `kms:ViaService=secretsmanager.ap-northeast-1.amazonaws.com`
+ `kms:CallerAccount` 条件で Decrypt を **直接** 許可しているため、
identity policy 側の `kms:Decrypt` は不要。

対照実験（実測）: `teamagent-dev-ecs-exec-mcp` は同じ AWS managed key で暗号化された
`teamagent/dev/database-url` に対し `GetSecretValue=allowed` / `kms:Decrypt=implicitDeny`
のまま本番稼働している。

**customer managed key へ移行した場合は exact key ARN での追加を別レビューにかける。**

## 禁止

- secret 値を `terraform output` / `console` / チャット / commit message へ出す
- plan 成果物を repo 配下や共有ストレージへ置く
- ワイルドカード secret ARN（`-*` / `-??????`）で許可する
- 「値を見て確認する」タイプのレビュー手順を作る
