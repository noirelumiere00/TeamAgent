# go-live 伴走チェックリスト（P1パイロット開店まで）

**使い方（3行）**：①各ステップの「実行」をあなたがコピペで実行 → ②出力を Claude セッションに貼る → ③Claude が「期待」と突合して GO/NO-GO を判定し、次へ進む。
詳細手順・背景は [deploy_runbook.md](deploy_runbook.md)（§番号で対応）。**`terraform apply` は必ず plan 確認→targeted**。
`<<要入力>>` = あなたしか知らない値。値は **絶対にチャット/Gitに貼らない**（出力を貼るときはトークンをマスク）。

> 事前に一度: `bash scripts/preflight_golive.sh` （read-only・FAIL が無くなってから開始）

---

## Phase 1｜準備（runbook §0-§2）

| # | ステップ | 実行 | 期待 | NGなら |
|---|---|---|---|---|
| 1 | ☐ ゲート①承認 | OpenClaw(Node)本番持込の組織承認を取得 | 承認OK | 承認が出るまで以降に進まない |
| 2 | ☐ モデルID確認 | `aws bedrock list-inference-profiles --region ap-northeast-1 \| grep -i haiku-4-5` | **`jp.anthropic.claude-haiku-4-5-20251001-v1:0`**（2026-06-11 実測で config/variables に**反映済み**＝出ることの再確認だけ） | 出ない/IDが変わった→Claudeへ（config 2箇所＋variables_fargate.tf を追従） |
| 3 | ☐ Secret 5本作成 | runbook §1 のコマンド5行（`database-url` の `<<要入力: DBパスワード>>` を埋めて） | 5本とも ARN が返る | `ResourceExistsException`→既存使用でOK。**§1を terraform plan より先に**（data source が前提） |
| 4 | ☐ ECR repo作成 | `cd infra/terraform && terraform apply -target=aws_ecr_repository.mcp -target=aws_ecr_repository.openclaw` | plan に **add 2件のみ**（repo: `teamagent-mcp` / `teamagent-openclaw`）→ apply 完了 | add 以外（change/destroy）が出たら**中断して plan を Claude に貼る** |
| 5 | ☐ イメージ build/push | runbook §2（`docker login` → `buildx --platform linux/arm64` ×2 → digest 控え） | 2 digest（`sha256:...`）が取得できる | buildx 無し→preflight 参照。push 拒否→ECR ログイン再実行 |

## Phase 2｜インフラ apply（runbook §3）

| # | ステップ | 実行 | 期待 | NGなら |
|---|---|---|---|---|
| 6 | ☐ tfvars 作成 | `infra/terraform/terraform.tfvars`（git管理外）に: `mcp_image`/`openclaw_image`（=URL@digest）・`shared_company_domains="vectorinc.co.jp"`・`enable_vpc_endpoints=true`・`alarm_email_endpoints=["<<要入力: 通知先>>"]`（`openclaw_model_id` は実測値が default 済＝省略可） | — | 値の形式が不明なら runbook §3 の例 |
| 7 | ☐ 全体 plan | `terraform plan` | エラー0。**IAM Deny（openclaw_task の rds/secrets Deny）と SG（mcp 8787 ingress=openclaw SGのみ）** が plan に見える | エラー/不審差分→**plan 全文を Claude へ** |
| 8 | ☐ 段階 apply | runbook §3 の順（roles→SG→cluster/logs/CloudMap→**mcp service**→openclaw service→dashboard） | 各 apply がエラー0 | 失敗した target 名＋エラーを Claude へ。**やり直しは同 target の再 apply（冪等）** |
| 9 | ☐ 隔離の実証 | IAM Policy Simulator: openclaw-task ロールで `secretsmanager:GetSecretValue` | **Denied** | Allowed なら**即中断**（fargate.tf の Deny 構造を確認＝設計違反） |

## Phase 3｜Slack・DB（runbook §4-§5）

| # | ステップ | 実行 | 期待 | NGなら |
|---|---|---|---|---|
| 10 | ☐ 新Slackアプリ | runbook §4（新規アプリ・Socket Mode・専用ch・scopes: `app_mentions:read` `chat:write` `channels:history` `connections:write`） | `xoxb-`/`xapp-` 取得 | 既存Botのアプリと**混同しない**（Socket Mode 二重接続不可） |
| 11 | ☐ token 投入＋反映 | `put-secret-value` ×2 → `aws ecs update-service --force-new-deployment`（service名は `terraform output -raw ecs_service_openclaw`） | デプロイが RUNNING に | task が落ちる→#13 の起動ログへ |
| 12 | ☐ DB migration | SSMトンネル→ `psql -f 0010_rls_email_case_insensitive.sql` → `0011`（`<COMPANY_DOMAIN>`→`vectorinc.co.jp` を sed） | エラー0（冪等＝再実行可） | psql エラー全文を Claude へ。**DELETE は無い**（UPDATE/POLICY のみ） |
| 13 | ☐ RLS 実走検証 | runbook §5: 2ユーザ相当で「会社doc可視/会社外0/admin詐称無効」 | 3点とも期待どおり | 1つでも破れたら**パイロット開始禁止**→Claude へ |

## Phase 4｜起動確認→稼働（runbook §5.5-§8）

| # | ステップ | 実行 | 期待 | NGなら |
|---|---|---|---|---|
| 14 | ☐ OpenClaw 起動ログ | CloudWatch `/teamagent/dev`（prefix `openclaw`）を確認（runbook §5.5） | **Slack connected / gateway listening / MCP接続 / モデル解決** の4点 | 出ないログ名と周辺行を Claude へ（§O の config 修正が効いているかの実機確認点） |
| 15 | ☐ smoke | `TEAMAGENT_MCP_BEARER=<<bearer>> TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp uv run python scripts/smoke_mcp.py --base-url http://127.0.0.1:8787 --full`（SSMで8787転送） | healthz=200 / 401 / **tools=4ナレッジのみ（scrape系・mail系が出ない）** / search可 | FAIL 行を Claude へ |
| 16 | ☐ 敵対ハーネス | `scripts/ingest_test_data.py --commit` → `scripts/attack_mcp.py --query P0HARNESS --outsider-needle ...` → `--cleanup`（adversarial_harness_runbook.md） | **全 vector PASS**（詐称無効・outsider漏れ0） | FAIL=パイロット開始禁止→Claude へ |
| 17 | ☐ 2人同時の混線テスト | 専用chで2人が同時に質問 | 各人のスレッドに各人の回答（混線なし＝dmScope per-channel-peer） | 混線したら**即 desired-count 0**（#19）→Claude へ |
| 18 | ☐ P1 パイロット開始 | 専用ch・2-3名・読取のみ・**1週間**。ダッシュボード `teamagent-dev-openclaw-pilot` を毎日確認 | 実測ゲート: 同時4で p95≤15s／エラー<1%／RLS越権0／コスト許容／無事故 | 異常時は #19 ロールバック（現行Botは無停止） |
| 19 | ☐ （常備）ロールバック | `aws ecs update-service --cluster <<ecs_cluster_name>> --service <<ecs_service_openclaw>> --desired-count 0` | ~1分で OpenClaw 停止・既存chの現行Botは継続 | — |

**#18 を1週間通過 → P1 完成 🎉**（→P2 判断・スクレイプ拡張は runbook §9）

---

### 既知制限・注意（承知のうえで開始）
- **会話メモリはタスク再起動で消える**（ephemeral・P1許容・P2でEFS判断）＝再デプロイ後は文脈リセット。
- `terraform validate` は社内proxyで失敗し得る → **plan が実質の検証**。
- secret 値・トークンは**チャットにもGitにも貼らない**（出力共有時はマスク）。
