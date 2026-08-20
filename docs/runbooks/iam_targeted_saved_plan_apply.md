# Runbook: IAM の targeted saved-plan apply（PR2-A0.2 型）

trusted automation role 自身の IAM を変更する唯一の正規経路。guard 経由は使えない
（permissions boundary の `DenyIamSelfEscalation` が `iam:PutRolePolicy` を無条件 Deny
しているため、role が自分の権限を書き換えることはできない — 意図された自己昇格防止）。
先例: `docs/runbooks/forced_rollback_drill.md` の基盤（KMS/IAM）手順。

## 原則

- 実行主体は **IAM administrator（AIIAdev）の MFA 済み一時セッション**。root は使わない
  （root は AssumeRole 不可・identity policy / boundary が効かない）。
- **exact saved plan 以外の apply は禁止**。review 後の再 plan は禁止
  （review した plan と apply される plan の同一性が壊れるため）。
- `-target` は変更対象の 1 リソースに固定する。無関係な import / removed ブロックは
  targeted plan では処理されないが、**plan 出力を行単位でレビューして混入ゼロを確認**する。

## 手順

```bash
# 1) 起点 principal の確認（root なら中止）
aws sts get-caller-identity --query '[Account,Arn]' --output text
#    => 718959508629  arn:aws:iam::718959508629:user/AIIAdev

# 2) 保存 plan の作成（--out は repository の外）
terraform -chdir=infra/terraform plan \
  -var-file=<0600 の terraform.tfvars> \
  -target=aws_iam_role_policy.runtime_evidence_automation \
  -out=/secure/path/a02.tfplan

# 3) human review（対象 1 リソースのみ・forget/import/他リソース混入ゼロを行単位で確認）
terraform -chdir=infra/terraform show -json /secure/path/a02.tfplan | less

# 4) plan SHA の記録
shasum -a 256 /secure/path/a02.tfplan

# 5) apply 直前の再確認（principal / account / state serial が review 時と同一）
aws sts get-caller-identity --query Arn --output text

# 6) 保存 plan だけを apply
terraform -chdir=infra/terraform apply /secure/path/a02.tfplan
```

## apply 後の検証（read-only）

```bash
# 権限が意図どおりか policy simulator で確認（実オブジェクトに触れない）
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::718959508629:role/teamagent-dev-terraform-runtime-automation \
  --action-names s3:GetObject s3:GetObjectRetention s3:GetObjectTagging \
  --resource-arns "arn:aws:s3:::teamagent-dev-image-release-evidence/codebuild-buildspecs/teamagent-dev-mcp-source-publisher/example.yml"
#    => allowed x3

aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::718959508629:role/teamagent-dev-terraform-runtime-automation \
  --action-names s3:PutObject s3:PutObjectRetention s3:DeleteObject \
  --resource-arns "arn:aws:s3:::teamagent-dev-image-release-evidence/codebuild-buildspecs/teamagent-dev-mcp-source-publisher/example.yml"
#    => explicitDeny / implicitDeny x3（書き込みが塞がれたままであること）
```

## rollback

statement を除去する revert commit を merge し、同じ手順で targeted saved plan を apply する。
