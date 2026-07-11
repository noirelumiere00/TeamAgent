# deploy_log — 本番デプロイ履歴（image ↔ commit/branch ↔ 概要）

このファイルが**「何が本番で動いているか」の唯一の正**。CLAUDE.md §4(B3)/§10 の運用ルール：
**デプロイするたびに 1 行追記**（image tag / digest 先頭 / 出所branch・commit / 対象service / 概要 / 実行者）。

> 初版は 2026-06-25 に **実機（ECS task-definition + ECR タグ）を直読みして再構成**したスナップショット
> （それ以前の履歴記録は存在しなかった）。値は「読み取った時点」のもの。

---

## 2026-07-06 🔬 TikTok分析ロジック審査→8勧告実装＋ルーティング最適化＋chromium回帰修正
- **分析ロジック審査(Fable多視点パネル→敵対検証)** で確定した8勧告＋レビュー4指摘を実装。offline 86＋pytest 120＋ruff/mypy 全green:
  R1 エンゲージ率100分の1表示バグ / R2 save_rate選抜の最低再生ガード(10000→1000→display bf・under_guard) / R3 #PR is_promoフラグ→breakout/top_saverate除外 /
  R4 提案書ハードコード結論文→MSG.p_semis化 / R5 WAF blocked検出(error_code:WAF_BLOCK到達可能) / R6 result.json来歴＋status.json恒久running解消 /
  R7 統計的誠実性(リフトゲート/n≥8/免責描画/(n=)表記) / R8 hybrid選抜軸。対象repo=`~/tiktok-data-service`・`~/Documents/TeamAgent`・`~/.claude/skills/tiktok-vseo-proposal`。
- **OCルーティング最適化(Fable Workflow・実測100%)**: tiktok_search/tiktok_acquire/tiktok_acquire_status の description を書き分け(相互排他注記)。「取得して/保存率上位の動画も」→tiktok_acquire に確実収束。SOUL.md にも tiktok_acquire 追記。
- **🔴 chromium 150 SIGTRAP 回帰を修正**: apt の chromium が 149→150 に更新され arm64 Fargate で起動直後 SIGTRAP(全フラグ回避不能・実機実証)。
  → **apt chromium を捨て Playwright 管理の版固定 chromium(arm64・chromium-1187)に切替**、`/usr/bin/chromium` へ symlink(CHROMIUM_PATH 不変)。`Dockerfile.acquire`＋`Dockerfile.teamagent-mcp` 両方。実 Fargate で起動実証。
- **デプロイ(desired=0 のためステージ・次回スケールアップで有効)**: tiktok `teamagent-dev-tiktok-acquire:3`(image f0f6e725・terraform apply)・mcp `:39`(image fcb15731・register+update・env41本保持)。
- **インフラ地雷修正(副産物)**: S3 lifecycle を lambda_iam.tf の `raw_files` に統合(1バケット1config・別resourceだと Glacierルールと交互上書きする事故の再発防止・`tiktok_acquire.tf` 側は state rm)。`fargate.tf` mcp service に `ignore_changes=[task_definition,desired_count]`(コスト停止と apply の衝突防止)。
- **✅ TikTok anti-bot 対策＝Apifyキーワード検索フォールバックを実装・実証(同日追記)**: tk_smoke4 の0件は反復テストによる WAF クールダウンで、時間経過で解消(tk_smoke5 は via=primary で10本取得)。恒久策として **chromium一次＋0件時 Apify(clockworks~tiktok-scraper・searchQueries)フォールバック** を実装(`vendor/apifyFallback.ts` searchTikTokViaApify・純粋写像 `src/apify_map.ts`・`acquire.ts` 配線・トリガは blocked でなく「0件」に広げる=サジェストAPI劣化も拾う)。offline 86+Apify写像テストgreen。
  - デプロイ: tiktok `teamagent-dev-tiktok-acquire:4`(image `6f3c3b83`・Apify fallback＋Playwright chromium)＋`tiktok_apify_secret_arn`(Secrets Manager `teamagent/dev/tiktok/apify-token`)を tfvars 配線→apply(taskdef に `APIFY_API_TOKEN` secret 注入・IAM は `teamagent/dev/tiktok/*` で既存カバー)。
  - **実証**: Apify 直叩き one-off で5件取得(mayo___media 918.7K再生等・実在人気投稿)。トークン初回は末尾改行等で `user-or-token-not-found`→`printf '%s'`＋`put-secret-value file://` で差し替え(再デプロイ不要=値は起動時読取)。
  - ⚠️ このセッションでトークンがチャットに露出→**ローテーション推奨**。proxy(`tiktok_proxy_secret_arn`)は未使用(Apify が anti-bot 吸収するため当面不要)。
  - **フォールバックアクターを clockworks→`automation-lab/tiktok-search-scraper`(検索特化)に切替**(tiktok `:6`・image `aa2fbb0b`)。教訓バグ2件を修正: ①automation-lab は pay-per-event で run が **READY 滞留**→`ensureApifyRunComplete` が RUNNING しか待たず0件→**非終端(READY)を一律ポーリング**(maxWait 240s)。②`Dockerfile.acquire` の Docker Hub 直 pull が **429** 多発→base を **ECR Public ミラー**(`public.ecr.aws/docker/library/node:22-bookworm-slim`)へ(mcp Dockerfile は既に ECR Public 済)。end-to-end 実証=デプロイ済 `:6` 経由で5件取得。
  - **知見(Apify vs Web検索)**: Apify は結果を **TikTok検索面の並び順のまま**返す(再生順でない=順位は本物)。chromium と結果セットが違うのは TikTok検索が **IP/セッション/時刻でパーソナライズ**されるため(別窓の正当なスナップショット)。**Web検索順位の忠実度は chromium 一次が本質・Apify は予備として十分**。

---

## 2026-06-26 🚀 tiktok_acquire 本番デプロイ（AI駆動TikTok取得サービス・@AiLa から呼べるように）
- **新スタック**: `enable_tiktok_acquire=true` で apply。SQS+DLQ / DynamoDB / 使い捨てECS Fargate(arm64) /
  Lambda dispatcher / IAM(権限分離) / SG / S3 lifecycle の18資源。トポロジ＝OC→MCP(SQS送信のみ)→SQS→
  Lambda(RunTask/PassRole保有)→使い捨てFargate(chromium+yt-dlp+ffmpeg)→S3/DynamoDB。
- **使い捨てFargate image**: `teamagent-dev-tiktok-acquire@sha256:1f43dca73a744d6057859277e2c4dbe6b18a2670df4e2c9d2e7275b1cc9dc08e`
  （tag `tk-1782454178`・`~/tiktok-data-service` Dockerfile.acquire を CodeBuild buildspec-override で arm64 ビルド）。
- **mcp**: `:33`(908da44d) → **`:34`** = dev版 **`teamagent-mcp@sha256:df13605951fe91bdc597392a441954e58032d1eec8faabe1fba87272bf28c4df`**
  （tag search-1782448366・tiktok_acquireコード同梱・現prod全USE_import を superset検証済）＋env `USE_TIKTOK_ACQUIRE=1`/
  `TIKTOK_TASK_QUEUE`/`TIKTOK_JOBS_TABLE`/`TIKTOK_S3_BUCKET`。CLI register+update（terraform不使用＝§4 B11）。services-stable 確認。
- **openclaw**: dev から再ビルド（toolFilter に `tiktok_acquire`/`tiktok_acquire_status` 追加・dev最新 SOUL/config 込み）→
  `apply_openclaw.sh` で `:22`→**`:23`** = `teamagent-openclaw@sha256:59139793dce02d175a5e25e45eed569853388fe332c3661a943cf86aeb3e58b6`
  （tag `dev-20260626-153430`）。rollout COMPLETED・health HEALTHY・`[slack] socket mode connected` 確認。
- **隠れ地雷を2件修正**: ①`tiktok_acquire.tf` の policy document **data** 3本が未gateで full `terraform plan` を破壊していた → `count=local.tk_enabled` 追加。
  ②**vpce SG(`sg-0284281974da41b4b`) ingress に tiktok_tasks SG 未追加**で 使い捨てFargate が ECR auth pull を i/o timeout(`TaskFailedToStart`) → `vpc_endpoints.tf` の ingress に追加（実障害を検知し修正）。
- **実証(SQS直叩き tk_smoke2)**: DynamoDB `done` / counts{kw:1,posts:10,videos:3,uploaded:17} / S3 に
  `posts.normalized.json`(10)＋`thumbs/`10枚＋`videos/`(p0001/0003/0007.mp4＋manifest.json) / Fargateログで scrape→DL→upload 確認。
- **@AiLa実機テストで発覚した2バグを同日修正**:
  ①`tiktok_search`/`video_algorithm` が `FileNotFoundError`＝df136059 を**薄殻(WITH_SCRAPE_TOOLS=false)**でビルドして載せ既存スクレイパ(コンテナ内Node)を壊していた →
  `build_mcp_image.sh` に `WITH_SCRAPE_TOOLS=true` 追加し**厚殻再ビルド** `teamagent-mcp@sha256:0c13dd9f…`(tag `mcpfat-1782457046`・node20/chromium149/ffmpeg/scraper入り)→ mcp `:34`→**`:35`**(env保持)。
  ②`tiktok_acquire` submit が `ClientError`＝mcpロール tiktok policy が `dynamodb:GetItem` のみで `submit()` の初期 `put_item`(tiktok_task_store.py:62)が AccessDenied → `tiktok_acquire.tf` の `tiktok_mcp_policy` に `dynamodb:PutItem` 追加(targeted apply・即時有効)。
- secret は直結（proxy/apify 無し・WAFは PACE_* で抑制・後付け可）。法務ゲートO1=承認済。

---

## 2026-06-26 ⚠️事故と恒久対策（openclaw 全断 → 復旧 → terraform 地雷封じ）
- **事故**: `infra/terraform/apply_openclaw.sh`（旧 = `terraform apply -auto-approve -target=openclaw`）を
  `-var openclaw_image=` 無しで実行 → `var.openclaw_image=""` で `count=0`＝openclaw service が destroy、
  image="" で task def 再作成失敗 → **AiLa(Slack bot) 全断**（mcp/connect-web は無傷）。根因=§4 B1/B11。
- **復旧**: CLI `aws ecs create-service` で正常な `teamagent-dev-openclaw:22`
  （image `teamagent-openclaw@sha256:24b9eb66…`・SLACK_DM_ALLOWLIST=`*`＝事故直前と同一）からサービス再作成。terraform 不使用。
- **恒久対策**: ①`terraform.tfvars` に `mcp_image`/`enable_*`/`slack_dm_allowlist` 明記 ②`fargate.tf`/`connect_web.tf` の
  mcp・connect-web task def/service に `lifecycle ignore_changes` ③**openclaw は CLI 管理**（tfvars に
  `openclaw_image` を書かない）④`apply_openclaw.sh` を CLI 化・`apply_resilience.sh` から ECS target 除去＋既定plan化 ⑤CLAUDE.md §4 B11 追記。
  検証=`terraform plan` で destroy=0 / replace=0。
- 本番 openclaw image（復旧後・据置）: `718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-openclaw@sha256:24b9eb660a0f794cbc5e7c13fb468ea2a04ce00550abc372af54496312d78607`（td family `:22` 由来）。
- 参考（今回 dev から焼いたが**未使用で保管**の mcp image）: ECR tag `search-1782448366` / `teamagent-mcp@sha256:df136059…`。

---

## ⚠️ いま本番で起きている重要な不整合（初版で判明）
1. **Python サービス群が 3 つの別 image ビルドで動いている**（下表）。`teamagent-mcp` image を connect-web/ingest/canary も共用するが、**サービスごとに別バージョン**になっている。
2. **mcp は dev でなく feature 枝から焼かれている**（タグが `search-*` / `mailrich-*` / `nlk-*`＝feature 名）。`dev-*` タグの mcp は存在しない＝**dev は本番の正ではない**。
3. **dev に無いコードが本番に居る**（例：`knowledge_deliver` は dev 未マージ＝PR #145 系だが、本番 mcp `nlk-*` はそれを含む）。
4. openclaw だけ dev 基点（`dev-20260622-resilience`）だが **06-22 と古い**（dev 最新や未マージ PR は未反映）。

→ 是正方針：**次回から mcp も dev から焼く**（`dev-*` タグに統一）。本番依存の feature 枝（#145 等）を dev へマージして「dev＝本番」に寄せる。

## 出所タグの読み方
- `dev-YYYYMMDD-<label>` … dev 基点ビルド（正しい形）。
- `search-*` / `mailrich-*` / `nlk-*` … **feature 枝ビルド**（暫定。dev へ寄せるべき）。
  - `search-*`=feat/search-filters-phase1、`nlk-*`=nl-knowledge（#145系・自然言語ナレッジ）、`mailrich-*`=mail 下書き濃度向上。

---

## 現状スナップショット（2026-06-25 ~17:4x JST・実機直読み）

| service | taskdef rev | image | digest(先頭) | 出所(推定) | 概要 |
|---|---|---|---|---|---|
| **mcp**（ツール実体） | 32 | `teamagent-mcp:nlk-20260625-173320` | `5c46b5fe` | nl-knowledge（#145系） | 当日17:33 再デプロイ。knowledge_deliver/検索フィルタ込み |
| **openclaw**（頭脳） | 21 | `teamagent-openclaw:dev-20260622-resilience` | `50e8b536` | dev(06-22) | 再発防止/dmopen/SOUL。dev最新・未マージPR未反映 |
| **connect-web** | 16 | `teamagent-mcp:search-1782295456` | `05be4a78` | feat/search-filters | OAuth公開。mcp image を旧版で共用 |
| **ingest** | 27 | `teamagent-mcp:search-1782295456` | `05be4a78` | feat/search-filters | 週次/一括取込。旧版共用 |
| **morning-digest** | 28 | `teamagent-mcp:mailrich-1782372476` | `f7ab51ed` | mail-draft-richer | 朝ダイジェスト。mail濃度向上ビルド |
| **canary** | 7 | `teamagent-mcp:search-1782295456` | `05be4a78` | feat/search-filters | 合成カナリア。旧版共用 |

### 本番 mcp で ON のツール flag（rev32・参考）
`USE_TIKTOK_TOOLS` `USE_VIDEO_TOOLS` `USE_KNOWLEDGE_DELIVER` `USE_KNOWLEDGE_FILTERS`
`USE_MAIL_LINK_TOOL` `USE_MAIL_REPLY_TOOL` `USE_MAIL_SUMMARY_TOOL` `USE_FOLLOWUP_TOOL`
`USE_MORNING_DIGEST_TOOL` `USE_OAUTH_CONNECT_TOOL` `USE_COHERE_RERANK` `USE_NEW_SCHEMA`
（= search / clientkarte / proposal_draft / proposal_review に加え上記が露出。`video_approval`/`tiktok_acquire` は未登録＝未稼働）

---

## デプロイ履歴（新しい順・1デプロイ1行で追記）

| 日時(JST) | service | image tag | digest先頭 | 出所branch/commit | 概要 | 実行者 |
|---|---|---|---|---|---|---|
| 2026-06-25 17:33 | mcp | nlk-20260625-173320 | 5c46b5fe | nl-knowledge(#145系) | 自然言語ナレッジ/knowledge_deliver | 不明(実機再構成) |
| 2026-06-25 16:31 | morning-digest | mailrich-1782372476 | f7ab51ed | mail-draft-richer | メール下書き濃度向上 | 不明(実機再構成) |
| 2026-06-24 19:07 | mcp(→現 connect-web/ingest/canary) | search-1782295456 | 05be4a78 | feat/search-filters-phase1 | 検索フィルタ/ナレッジ | 不明(実機再構成) |
| 2026-06-23 08:08 | openclaw | dev-20260622-resilience | 50e8b536 | dev(resilience build) | 再発防止/dmopen/SOUL | 不明(実機再構成) |

> 以降のデプロイは**この表の先頭に1行追記**すること（CLAUDE.md §10 E1 の4段ゲートを通したら必ず記録）。
