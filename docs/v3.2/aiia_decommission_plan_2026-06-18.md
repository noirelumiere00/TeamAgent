# AI-IA-UAE（朝メール別プロジェクト）退役プラン — 2026-06-18

> ユーザー判断: 「AI-IA は消す（OC→TA で同機能を将来実装するイメージ）。AWS リソース＋別repo も全部消す」
> 関連: `docs/v3.2/data_governance_inventory_2026-06-18.md`

---

## ⚠️ 最重要: 削除対象と「残す対象」の見分け

`aiia` プレフィックスの中に**別プロダクト2つが混在**している。命名が紛らわしいので絶対に取り違えない。

### 🔴 削除対象（AI-IA-UAE: 朝メール別プロジェクト）

| リソース | 種別 | 識別 |
|---|---|---|
| ECS service `teamagent-dev-aiia-mcp` | Fargate | aiia_mcp.tf:240+ |
| ECR repo `teamagent-aiia-mcp` | Container registry | aiia_mcp.tf:9 |
| CodeBuild `teamagent-dev-aiia-image-builder` | CI | aiia_mcp.tf:51 |
| CloudWatch log `/teamagent/dev/aiia-mcp` | Logs | aiia_mcp.tf:74 |
| IAM `teamagent-dev-aiia-mcp-task` | Role | aiia_mcp.tf:170 |
| IAM `teamagent-dev-ecs-exec-aiia` | Role | aiia_mcp.tf:108 |
| IAM `teamagent-dev-codebuild-aiia` | Role | aiia_mcp.tf:22 |
| Security Group `aiia_mcp` | SG | aiia_mcp.tf:200 |
| Service Discovery `aiia_mcp` | SD | aiia_mcp.tf:217 |
| Secret `teamagent/dev/aiia/mcp-bearer` | Secret | data source aiia_mcp.tf:77 |
| Secret `teamagent/dev/aiia/google-client-id` | Secret | data source |
| Secret `teamagent/dev/aiia/google-client-secret` | Secret | data source |
| Secret `teamagent/dev/aiia/oauth-state-secret` | Secret | data source |
| DynamoDB `aiia-notified-events` | Table | （aiia_mcp.tf 外 or 別管理） |
| DynamoDB `aiia-oauth-tokens` | Table | 同上 |
| DynamoDB `aiia-reminder-state` | Table | 同上 |
| DynamoDB `aiia-slack-tokens` | Table | 同上 |
| S3 prefix `teamagent-dev-raw-files/aiia/` | Storage | （手動削除） |
| terraform `infra/terraform/aiia_mcp.tf` | IaC | ファイルごと削除 |
| terraform `fargate.tf` の aiia 参照 1行 | IaC | 参照のみ削除 |
| terraform `vpc_endpoints.tf` の aiia 参照 3行 | IaC | 参照のみ削除 |
| 別repo `~/Documents/AI-IA-UAE` | ローカル | `rm -rf` |
| memory `project_aiia_morning_email.md` | メモ | 削除 |
| memory `MEMORY.md` の AI-IA-UAE 行 | インデックス | 1行削除 |

### 🟢 絶対に残す対象（AiLa 本体の connect 経路）

| リソース | 種別 | 理由 |
|---|---|---|
| **Lambda `aiia-connect`** | Lambda | **AiLa の connect-web バックエンド**（`connect.newstv.co.jp` のバックエンド）。**消すと OAuth 連携が死ぬ** |
| **IAM role `aiia-connect-lambda`** | Role | 上記 Lambda 用 |
| **Secret `aiia/prod/connect-env`** | Secret | **説明欄に「AiLa connect_web env」と明記**。AiLa の本番 connect 用 |
| **ALB `teamagent-connectweb-alb`** | ALB | AiLa connect-web の入口 |
| memory `reference_aila_connect_url_endpoint.md` | メモ | AiLa connect 用なので残す |

**判別ルール**:
- `aiia-mcp` 系（teamagent-dev-aiia-mcp / teamagent-aiia-mcp 等） → 削除
- `aiia-connect` / `aiia/prod/connect-env` → **残す**

---

## 削除手順（フェーズ別・依存順）

### Phase 1: コード変更（私が代行可・PR にする）

1. `infra/terraform/aiia_mcp.tf` をファイルごと削除
2. `infra/terraform/fargate.tf` の aiia 参照 1行を削除（merged log group の参照など）
3. `infra/terraform/vpc_endpoints.tf` の aiia 参照 3行を削除（VPC endpoint SG 許可など）
4. PR 作成（base=main? dev? → terraform は main 宛が筋）

検証:
- `terraform plan` で消えるリソース一覧が「§削除対象」と一致すること
- 残す対象（aiia-connect 系）は plan に出てこないこと
- `terraform validate` 緑

### Phase 2: 本番停止（人間ゲート・無停止）

5. **Fargate aiia-mcp を desiredCount=0 にして停止**
```bash
aws ecs update-service --cluster teamagent-dev --service teamagent-dev-aiia-mcp --desired-count 0 --region ap-northeast-1
```
6. 数分待ってタスクが全て STOPPED になることを確認
```bash
aws ecs describe-services --cluster teamagent-dev --services teamagent-dev-aiia-mcp --query "services[0].[runningCount,desiredCount]" --region ap-northeast-1
```

### Phase 3: terraform apply（人間ゲート・削除実行）

7. PR をマージ後、本番デプロイのタイミングで:
```bash
cd infra/terraform
terraform plan -out=aiia-decom.tfplan        # 消えるリソース一覧を再確認
terraform apply aiia-decom.tfplan            # 削除実行
```
これで以下が消える: ECS service / task def / SG / Service Discovery / ECR / CodeBuild / CloudWatch log group / IAM 3個

### Phase 4: terraform 範囲外の手動削除（人間ゲート）

8. **DynamoDB 4テーブル**（state にない or 残ってる場合）:
```bash
for t in aiia-notified-events aiia-oauth-tokens aiia-reminder-state aiia-slack-tokens; do
  aws dynamodb delete-table --table-name $t --region ap-northeast-1
done
```

9. **Secrets Manager 4個削除**（即時削除＝7日 recovery 期間）:
```bash
for s in teamagent/dev/aiia/mcp-bearer teamagent/dev/aiia/google-client-id \
         teamagent/dev/aiia/google-client-secret teamagent/dev/aiia/oauth-state-secret; do
  aws secretsmanager delete-secret --secret-id "$s" --recovery-window-in-days 7 --region ap-northeast-1
done
```
※ **`aiia/prod/connect-env` は削除しない**（AiLa 本体用）

10. **S3 `aiia/` prefix 削除**:
```bash
aws s3 rm s3://teamagent-dev-raw-files/aiia/ --recursive --region ap-northeast-1
```

11. **ECR リポジトリ削除**（terraform で消えるはずだが、image 残ってると失敗するので force）:
```bash
aws ecr delete-repository --repository-name teamagent-aiia-mcp --force --region ap-northeast-1
```

12. **CloudWatch log group が terraform で消えなかった場合**:
```bash
aws logs delete-log-group --log-group-name /teamagent/dev/aiia-mcp --region ap-northeast-1
```

### Phase 5: ローカル削除（私が代行可）

13. 別 repo の最終確認（コミット漏れ・残作業ないか）後、ディレクトリ削除:
```bash
ls -la ~/Documents/AI-IA-UAE              # 最終確認
git -C ~/Documents/AI-IA-UAE status        # 未コミット差分が無いこと
rm -rf ~/Documents/AI-IA-UAE
```

14. memory ファイル削除:
```bash
rm /Users/s-komata/.claude/projects/-Users-s-komata/memory/project_aiia_morning_email.md
```

15. `MEMORY.md` の AI-IA-UAE 行（1行目）を削除（Edit ツール）

---

## 検証（削除完了の確認）

```bash
export AWS_REGION=ap-northeast-1
# 全部「(空 or なし)」になるはず（aiia-connect 系を除く）
aws ecs list-services --cluster teamagent-dev --query "serviceArns[?contains(@, 'aiia-mcp')]" --output text
aws ecr describe-repositories --query "repositories[?repositoryName=='teamagent-aiia-mcp']" --output text
aws dynamodb list-tables --query "TableNames[?starts_with(@, 'aiia-')]" --output text
aws secretsmanager list-secrets --query "SecretList[?contains(Name, 'teamagent/dev/aiia/')].Name" --output text
aws s3 ls s3://teamagent-dev-raw-files/aiia/

# これは「あるはず」（残す対象）
aws lambda get-function --function-name aiia-connect --query "Configuration.FunctionName" --output text
aws secretsmanager describe-secret --secret-id aiia/prod/connect-env --query "Name" --output text
```

---

## ロールバック判断

万一「やっぱり AI-IA 必要」となった場合:
- **Phase 1 のみ実施段階** → PR を revert すれば元通り（worktree 上の話）
- **Phase 2 まで** → desiredCount を戻すだけ
- **Phase 3 以降** → terraform state 失われたら復旧困難。**Phase 3 前に snapshot/backup 推奨**（DynamoDB の export to S3 + 別repo を git archive）

別repo `~/Documents/AI-IA-UAE` を `rm -rf` する前に、念のため `git push` してリモート（GitHub？）に同期。最終commit は 6/8（`1de303f`）。

---

## コスト削減見込み

| 項目 | 月額 削減 |
|---|---|
| Fargate aiia-mcp（0.5vCPU/1GB） | ~$22 |
| DynamoDB 4 テーブル（オンデマンド・ほぼ空） | ~$1 |
| ECR ストレージ（205MB） | < $0.1 |
| CodeBuild（ビルド時間） | < $1 |
| CloudWatch Logs（30日保持） | < $1 |
| Secrets Manager 4個（$0.4/月/個） | ~$1.6 |
| **合計** | **約 $25/月** |

加えて **1サービス減・複雑度低下・命名混乱の解消**。

---

## やる順序の推奨

1. **今は私が Phase 1 だけやる**（terraform 削除PRを作る・main宛）
2. PR レビュー → マージ
3. **あなたが Phase 2-4 を本番反映のタイミングで実施**（コマンドはコピペ可・スクリプト化も可）
4. 全部消えたあと **Phase 5（ローカル）は私が代行**

---

## 開いた論点

1. **PR base は main で正しいか？** terraform 変更は main 宛が筋（過去 PR#127 も main 宛）。確認。
2. **Phase 3 前に DynamoDB の export to S3 でバックアップは要るか？** 中身がほぼ空なので不要と判断、念のため確認。
3. **別 repo を消す前に GitHub にミラー push しておくか？** （消したあと復旧不能になるので最終バックアップ）

---

*この文書は調査結果と削除プランです。Phase 1 着手前に他セッションへの共有を推奨。*
