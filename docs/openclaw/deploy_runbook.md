# OpenClaw 前面 P1 パイロット — デプロイ Runbook（§I / M2-M5）

OpenClaw 外殻 ＋ TeamAgent-MCP 境界（会社共有モデル §G）を **ECS Fargate** に出し、専用Slackチャネル・
少数(2-3名)・**読取のみ**で実稼働させるまでの本人手順。production image は
**署名済み release digest＋one-time full saved plan** 以外では変更しない。
全コードは authoring 済（dev/PR#118）。本書は **apply＝本人操作**の手順だけを示す。

> 関連: プラン `~/.claude/plans/mossy-snacking-locket.md` §A/§C/§D/§G/§H/§I。IaC=`infra/terraform/{fargate,ecr,vpc_endpoints,cloudwatch_fargate,outputs_fargate}.tf`、
> イメージ=`infra/docker/Dockerfile.{teamagent-mcp,openclaw}`、smoke=`scripts/smoke_mcp.py`。

## 0. 前提（gated）
- [ ] **ゲート①承認**：OpenClaw(Node コンテナ)を本番 AWS に持ち込む承認。
- [ ] **Bedrock モデル確認**：`aws bedrock list-inference-profiles --region ap-northeast-1` で
      Haiku4.5 の推論プロファイル ID を確定（`variables_fargate.tf:openclaw_model_id` / `openclaw.config.json5` の `★deploy時要確認` を実値へ）。
- [ ] リージョン=`ap-northeast-1`、account=`718959508629`、tfstate=既存 S3 backend（`main.tf:32-38`）。
- [ ] **P1 範囲＝会社ナレッジ4ツール（search/clientkarte/proposal_draft/proposal_review）読取のみ**。スクレイプ/動画ツールは**既定 OFF**＝有効化は P1 安定後に §9（別承認）。

## 1. Secrets 作成（値は本人が投入・コミット禁止）
Secrets Manager に 5 つ作成（名前は `variables_fargate.tf` の default に合わせる）:
```sh
R=ap-northeast-1
aws secretsmanager create-secret --region $R --name teamagent/dev/mcp/bearer            --secret-string "$(openssl rand -hex 32)"
aws secretsmanager create-secret --region $R --name teamagent/dev/database-url           --secret-string "postgresql://USER:PASS@HOST:5432/teamagent?sslmode=require"
aws secretsmanager create-secret --region $R --name teamagent/dev/openclaw/slack-bot-token --secret-string "xoxb-..."   # 手順4で取得
aws secretsmanager create-secret --region $R --name teamagent/dev/openclaw/slack-app-token --secret-string "xapp-..."   # 手順4で取得
aws secretsmanager create-secret --region $R --name teamagent/dev/openclaw/gateway-token   --secret-string "$(openssl rand -hex 32)"
```
- `teamagent/dev/mcp/bearer` と `gateway-token` は新規ランダム。`database-url` は既存 RDS（password は `teamagent/dev/*` の DB secret 参照可）。
- ⚠️ `fargate.tf` は **secret 実在を前提**（`data.aws_secretsmanager_secret`）＝この手順を terraform plan より先に。

## 2. イメージ build・検証・release authorization（2つ）
```sh
# clean な remote dev HEAD から quarantine build→actual-image gate→candidate receipt
bash infra/deploy/build_teamagent_image.sh
bash infra/deploy/build_openclaw_image.sh

# 各 launcher が返した candidate receipt の exact key/VersionId を使い、
# pipeline=mcp と pipeline=openclaw をそれぞれ active（rollback 時は rollback）承認
bash infra/deploy/authorize_image_release.sh --help
```

ECR/provenance 基盤をまだ導入していない場合だけ、
`infra/terraform/README.md` の one-time provenance bootstrap を先に完了する。
ローカル `docker build/push`、mutable tag、candidate/quarantine digest はデプロイ証拠にならない。

## 3. Terraform apply（one-time full saved plan）
`infra/terraform/terraform.tfvars`（git管理外）に:
```hcl
mcp_image              = "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@sha256:<MCP_RELEASE_DIGEST>"
openclaw_image         = "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-openclaw@sha256:<OPENCLAW_RELEASE_DIGEST>"
image_release_evidence = {
  mcp      = { # authorize_image_release.sh が返した exact key/VersionIds }
  openclaw = { # authorize_image_release.sh が返した exact key/VersionIds }
}
shared_company_domains = "vectorinc.co.jp"        # §G 会社共有ドメイン
openclaw_model_id      = "jp.anthropic.claude-haiku-4-5"   # 手順0で確定した値
enable_vpc_endpoints   = true
alarm_email_endpoints  = ["s-komata@vectorinc.co.jp"]
```
`image_deployment_intent_id` は設定しない。worktree 外の saved plan を作成・レビューし、
その同じ plan を一度だけ apply:
```sh
bash infra/deploy/terraform_runtime_guard.sh plan --help
terraform show /secure/local/path/openclaw-release.tfplan
bash infra/deploy/terraform_runtime_guard.sh apply --plan /secure/local/path/openclaw-release.tfplan --out /secure/local/path/openclaw-release.apply.json
```
- plan は全差分をレビュー（特に IAM Deny / SG / secrets data source 解決）。
- `-target`、direct ECS task-definition registration、失敗後の同 plan 再実行は禁止。
- apply 失敗時は状態を reconcile し、fresh receipt＋new intent＋new plan で roll-forward/rollback。
- 検証: **OpenClaw タスクロールで `secretsmanager:GetSecretValue` が拒否**されることを IAM Policy Simulator で確認。

### 3.1 通常運用logの30日adoption

runtime migrationには、既存のContainer Insights
`/aws/ecs/containerinsights/teamagent-dev/performance` と
`/aws/ecs/containerinsights/teamagent-dev-tiktok/performance`（live初期値はいずれも1日）を
含む7 log groupのin-place import/adoptionが必要です。各groupの既存eventをexact
S3 bucket/key/versionからfresh fileへ取得したretention export receiptを作り、
`terraform_runtime_guard.sh plan`へ渡します。receiptはcanonical path/device/inode/
nlink=1/size/timestamps/hash、AWS metadata、delivery時刻を拘束し、saved plan時とapply直前に
guardが再取得・再hashします。7 groupの1件でも欠落、別version、差替え、時刻逆転なら中断し、
log groupを削除・再作成したりdirect retention変更で迂回しません。

## 4. 新 Slack アプリ（OpenClaw 専用・Socket Mode）
- Slack で **新規アプリ**を作成（既存 Bot とは別＝Socket Mode 二重接続回避）。**専用チャネル**を1つ用意。
- スコープ: `app_mentions:read`/`chat:write`/`channels:history`(+DM 要件) など。**Socket Mode 有効**で `connections:write`。
- 取得した `xoxb-`/`xapp-` を手順1の secret（`slack-bot-token`/`slack-app-token`）に `put-secret-value` で投入。
- service を再デプロイ（`aws ecs update-service --force-new-deployment`）してトークンを反映。

## 5. DB マイグレーション（SSM トンネル・要承認）
SSM トンネルで RDS へ接続し、未適用分を流す（**会社共有モデルの前提**）:
```sh
# 既存踏み台/worker 経由 SSM port forward → psql で
psql "$DATABASE_URL" -f infra/migrations/0010_rls_email_case_insensitive.sql
# 0011 は <COMPANY_DOMAIN> を実値へ置換してから
sed 's/<COMPANY_DOMAIN>/vectorinc.co.jp/g' infra/migrations/0011_backfill_company_acl_groups.sql | psql "$DATABASE_URL"
```
- **RLS 実走検証（M1/P0）**: 2 ユーザ相当で「会社ドメイン doc は見える / 会社外は0 / `user_role=admin` 詐称は無効」を確認（`scripts/smoke_mcp.py --full` か手動 SQL）。

### 5.5 OpenClaw 起動ログ確認（§O・config 妥当性の実機確認）
CloudWatch `/teamagent/dev`（stream prefix `openclaw`）で以下を確認:
- **Slack connected**（channels.slack=Socket Mode が確立。出なければ token/scope/`channels.slack` 設定を疑う）
- **gateway listening**（loopback:18789。ECS healthCheck はこれを叩く）
- **MCP teamagent 接続**（streamable-http 8787。`tools/list` が toolFilter どおりか）
- **モデル解決**（`amazon-bedrock/jp.anthropic.claude-haiku-4-5-v1:0`。unknown model なら §0 の list-inference-profiles 値とズレ）
- `discovery` キー位置の警告が出ていないか（出たら plugins.entries 配下へ移動を検討・起動は止めない）
- ⚠️ **既知制限（P1 許容・記録）**: 会話メモリ（~/.openclaw/memory/SQLite）は **タスク再起動で消える**（volume は ephemeral）。
  スレッド文脈は Slack 側にも残るため P1 は許容。P2 で EFS マウント or stateless 設計を判断。

## 6. 起動確認 & smoke
```sh
# タスクが RUNNING / healthz green を確認
aws ecs describe-services --cluster $(terraform -chdir=infra/terraform output -raw ecs_cluster_name) \
  --services $(terraform -chdir=infra/terraform output -raw ecs_service_mcp) --query 'services[0].deployments'
# MCP へ（SSM トンネルで 8787 を localhost へ転送して）smoke
TEAMAGENT_MCP_BEARER=<bearer値> TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp \
  uv run python scripts/smoke_mcp.py --base-url http://127.0.0.1:8787 --full
# 期待: healthz=200 / bearer無=401 / tools=会社ナレッジ4のみ / (--full) search=会社ドメインdocのみ
```
- Slack 専用チャネルで実ユーザが「検索/カルテ/提案」を投げ、**他人に返答が混ざらない**（`dmScope:per-channel-peer`）ことを 2 人同時で確認。

## 7. ロールバック（~1分・可逆）
OpenClaw は**専用 Slack アプリ/チャネル**・現行 Bot は**既存チャネル**で物理分離。ロールバック＝**OpenClaw を止めるだけ**（現行 Bot は無停止）:
```sh
aws ecs update-service --cluster <cluster> --service <ecs_service_openclaw> --desired-count 0
```
- `USE_OPENCLAW_FRONTEND` の**コード実装は不要**（物理分離のため運用ロールバックで足る）。MCP バックエンドは残してよい（現行 Bot からは使わない／将来の入口）。

## 8. パイロット運用ゲート（→P2 判断）
- 専用ch・2-3名・読取のみで**1週間**。`teamagent-dev-openclaw-pilot` ダッシュボードで監視。
- 合格: **同時4で p95≤15s／エラー<1%／RLS越権0（会社外/admin不可）／コスト許容／無事故**＋ OpenClaw 単一GWの同時実行上限・月運用工数を記録。

## 9. （拡張版・任意）スクレイプ/動画ツール有効化（§L/§M/§N・別承認）
P1 の4ナレッジツールに加え、**TikTok検索・動画分析・VSEOアルゴリズム分析**を OpenClaw に出す“拡張版”。
**既定 OFF**（`enable_scrape_tools=false`／P1 薄殻には一切影響しない）。有効化は P1 安定後・別判断で。
前提＝**§N の SSRF 硬化（`adapters/url_guard.py`）が入っていること**（読取専用・HITL不要）。

```sh
R=ap-northeast-1
# (a) Gemini 認証 secret（variables_fargate.tf:gemini_secret_name と一致。Vertex 利用なら task role + GOOGLE_CLOUD_PROJECT）
aws secretsmanager create-secret --region $R --name teamagent/dev/gemini-api-key --secret-string "AI..."
# (b) WITH_SCRAPE_TOOLS は production contract の固定入力。guarded MCP launcher で
#     quarantine build→attest→candidate→active receipt を作る
bash infra/deploy/build_teamagent_image.sh
bash infra/deploy/authorize_image_release.sh --help
# (c) terraform.tfvars に enable_scrape_tools=true、新 release @sha256、exact receipt
#     VersionIds を設定し、§3 と同じ new full saved-plan flow を一度だけ実行
bash infra/deploy/terraform_runtime_guard.sh plan --help
terraform show /secure/local/path/mcp-scrape-release.tfplan
bash infra/deploy/terraform_runtime_guard.sh apply --plan /secure/local/path/mcp-scrape-release.tfplan --out /secure/local/path/mcp-scrape-release.apply.json
# (d) 任意: 許可ドメインを絞る（未設定なら url_guard の保守的既定 youtube/youtu.be/tiktok/instagram）
#     task env SCRAPE_ALLOWED_DOMAINS="youtube.com,youtu.be,tiktok.com,instagram.com"
```
- (e) OpenClaw `openclaw.json` の `toolFilter.include` に `tiktok_search`/`video_analysis`/`video_algorithm` を追加（既にテンプレ記載・コメント参照）。
- **検証**:
  ```sh
  # 3ツールが露出していること
  TEAMAGENT_MCP_BEARER=<bearer> uv run python scripts/smoke_mcp.py --base-url http://127.0.0.1:8787 --expect-scrape
  # video_analysis が SSRF URL(IMDS/localhost/private/非許可) を拒否すること
  TEAMAGENT_MCP_BEARER=<bearer> uv run python scripts/attack_mcp.py --base-url http://127.0.0.1:8787 --mode ssrf
  ```
- **残存リスク（記録・P2検討）**:
  - **DNS rebinding**：検証時の解決と yt-dlp/Node の実接続が別タイミング＝TOCTOU 余地。allowlist＋内部IP拒否＋社内＋読取専用で実害を限定。完全対処（pinned-connect）は P2。
  - **Gemini の $ ハードキャップ無し**：1リクエスト費用は max_videos 上限（≤10/≤30）×単価で**算術的に有界**。動的キャップ実装は重く、**CloudWatch コストアラート（M4）で監視**する方針。
  - **video_algorithm の同期処理（DL+Gemini+presigned）が OpenClaw 既定 60s timeout に収まるか**を P1 拡張時に監視（超過なら max_videos を下げる）。

## コスト目安（パイロット）
- Fargate 2 task（小）＋ ECR ＋ CloudWatch ＋ VPC endpoints(任意 ~$7/月×6) ＋ Bedrock(Haiku外側+cache)。詳細は §9。
- 拡張版(§9)有効時は Gemini 従量＋動画DL帯域が加算（VSEO分析 1回 ≈ $0.x・max_videos に比例）。
