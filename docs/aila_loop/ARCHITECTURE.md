# ARCHITECTURE.md — AiLa（v3.2 / live／loop読み取り用）

最終更新: 2026-06-16
Branch: `dev`（main より 40+ commit 先行・`bundled_deploy_2026-06-16` で本番反映）
Region: `ap-northeast-1` ／ AWS Account: `718959508629`
本ファイルの位置づけ: PDCA loop が **毎営業日朝に Plan フェーズで参照する 1 枚絵**。Maker subagent からの編集は禁止（RULES.md §1.7／PDCA_LOOP.md §9 allowed_files）。改訂は人間専用 routine（`/vision-review`）でのみ可。

関連: `docs/v3.2/system_reference.md`（フル仕様）／`docs/v3.2/slo_v1.md`（SLO 契約）／`docs/openclaw/deploy_runbook.md`（デプロイ）／`~/.claude/plans/mossy-snacking-locket.md` §T／§Q／§V。

---

## 0. 30秒で全体像

```
                           ap-northeast-1 / acct 718959508629
┌─────────────────┐
│   Slack         │
│   vectorinc     │   Socket Mode (xoxb/xapp)
│   #aila 専用ch  │ ─────────────┐
│   @AiLa mention │              │
└─────────────────┘              ▼
                ┌──────────────────────────────────────┐
                │  ECS Fargate cluster: teamagent-dev  │
                │                                      │
                │  ┌────────────────────────────────┐  │       ┌──────────────────────────┐
                │  │  Service: openclaw             │  │       │  Bedrock (ap-northeast-1)│
                │  │  (Node 24 / TS)                │  │ ──▶   │  - Claude Sonnet 4.6     │
                │  │  taskdef teamagent-dev-        │  │       │  - Claude Haiku 4.5      │
                │  │   openclaw:10                  │  │       │  - Cohere Rerank v3.5    │
                │  │  - Slack Socket Mode 1 接続    │  │       └──────────────────────────┘
                │  │  - LLM ルーター (Haiku)        │  │
                │  │  - clawhub.disabled = true     │  │       ┌──────────────────────────┐
                │  │  - tools 限定 (toolFilter)     │  │       │  Vertex AI (ntv-ai / GCP)│
                │  └──────────────┬─────────────────┘  │ ──▶   │  - Gemini 2.5 Flash      │
                │                 │ streamable-http     │       │   (動画マルチモーダル)   │
                │                 │ bearer auth         │       └──────────────────────────┘
                │                 │ SG: openclaw → mcp  │
                │                 ▼                     │
                │  ┌────────────────────────────────┐  │       ┌──────────────────────────┐
                │  │  Service: teamagent-mcp        │  │       │  RDS PostgreSQL 16.14    │
                │  │  (Python 3.11 / FastMCP)       │  │ ──▶   │  + pgvector 0.8.2        │
                │  │  taskdef teamagent-dev-mcp:5   │  │       │  + RLS (acl_groups       │
                │  │  - search / clientkarte /      │  │       │   intersect)             │
                │  │    proposal_draft / review     │  │       │  - documents 794 件      │
                │  │  - operation_log (Wave1-②)    │  │       │  - chunks (e5 1024d)     │
                │  │  - video_analysis (gated)      │  │       │  - usage_events (旧経路) │
                │  │  - tiktok_search (gated)       │  │       │  - connector_state (W2-⑤)│
                │  │  - STRUCTLOG_FORMAT=json       │  │       └──────────────────────────┘
                │  │  - port 8787 (SG ingress=      │  │                  ▲
                │  │     openclaw SG のみ)          │  │                  │ ingest only
                │  └────────────────────────────────┘  │                  │
                └──────────────────────────────────────┘                  │
                                                                          │
                ┌──────────────────────────────────────┐                  │
                │  worker EC2 (t4g.medium / arm64)     │ ──── ingest ─────┘
                │  i-0feaa3c…                          │       週次 systemd timer
                │  - teamagent-ingest.service          │       Slack history + Drive
                │  - Wave1-③ ingest 配線              │       → chunks → pgvector
                │  - Wave2-④ office 本文抽出          │
                │  - #ops Slack alert on failure       │
                │  - 旧 slack_bot は引退（接続しない） │
                └──────────────────────────────────────┘

                ┌──────────────────────────────────────┐
                │  AiLa MCP (別repo ~/Documents/AI-IA-UAE)
                │  朝メール秘書 — Phase2 で OpenClaw 結線（§Q-Q3 Wave4）
                │  現状: fake inbox / heuristic (Milestone1 完了)
                └──────────────────────────────────────┘

  Secrets Manager: teamagent/dev/{mcp/bearer, openclaw/slack-bot-token,
                                  openclaw/slack-app-token, openclaw/gateway-token,
                                  database-url, sentry_dsn, ops-slack-webhook, ...}
  S3:              teamagent-dev-raw-files / teamagent-tfstate-* (DynamoDB lock = teamagent-tflock)
  ECR (immutable): teamagent-mcp, teamagent-openclaw
```

**読み方**: 左→右が「ユーザー → Bedrock/Vertex」、上→下が「Fargate → RDS/EC2」。本番経路は **Slack → OpenClaw(Fargate) → teamagent-mcp(Fargate) → Bedrock + pgvector**。worker EC2 は **ingest 専用**で常駐 Bot ではない（go-live で旧 slack_bot は引退・Socket Mode 二重接続禁止）。

---

## 1. コンポーネント表

| # | コンポーネント | 役割 | 実装 | デプロイ先 | 接続元 | 接続先 | 秘密の取得経路 |
|---|---|---|---|---|---|---|---|
| 1 | **Slack Workspace** (vectorinc, App `A0B51FGQ8JK`) | ユーザー入口・出口 | — | Slack Cloud | 営業16名 | OpenClaw (Socket Mode) | Secrets Manager → openclaw env |
| 2 | **OpenClaw 外殻** (Node 24/TS) | Slack 張付け／LLM ルーター(Haiku)／tool 呼出制御／clawhub 無効化／会話メモリ(ephemeral SQLite) | TypeScript | ECS Fargate `teamagent-dev-openclaw:10` (arm64) | Slack | teamagent-mcp (HTTP), Bedrock (Converse) | IAM Role (IMDSv2) + Secrets Manager |
| 3 | **teamagent-mcp** (MCP 金庫) | 4 ツール (search / clientkarte / proposal_draft / proposal_review) + operation_log (W1-②)。Pydantic v2 I/O・RLS gate・bearer 認証・port 8787 | Python 3.11 / FastMCP / streamable-http | ECS Fargate `teamagent-dev-mcp:5` (arm64) | OpenClaw (SG ingress=openclaw SG のみ) | RDS (pgvector)、Bedrock、Cohere Rerank | IAM Role + Secrets Manager |
| 4 | **aiia-mcp** (朝メール秘書) | AiLa Phase1 ロジック (mcp/providers/pipeline/delivery/CLI)。fake inbox + heuristic (38 tests / cov94%) | Python / Agent SDK | 別 repo `~/Documents/AI-IA-UAE` (dev `7367507`/p1f)。本番結線は Wave4 で OpenClaw に追加 | (未結線) | Gmail (将来)、Bedrock、社内カレンダー | (Phase2 で Secrets Manager) |
| 5 | **video-mcp** (VSEO / video / tiktok) | over-fetch + yt-dlp + ffmpeg + Gemini 時刻付き分析 → HTML レポート → S3 署名 URL。default OFF (`USE_VIDEO_TOOLS=1` で有効化) | Python + Node | teamagent-mcp に同梱・gated | OpenClaw | Vertex AI (Gemini)、S3 | Vertex SA materialize (`load_secrets.sh`) |
| 6 | **pgvector / RDS** | ナレッジ正本。documents 794 件 (gdrive 589 / slack 205) + chunks (e5 1024d, HNSW cosine) + usage_events + connector_state (W2-⑤) + RLS (本人/会社ドメイン intersect) | PostgreSQL 16.14 + pgvector 0.8.2 | RDS db.t4g.micro | teamagent-mcp、worker EC2 (ingest) | — | Secrets Manager `teamagent/dev/database-url` |
| 7 | **Bedrock** | テキスト LLM = Sonnet 4.6 (回答合成・T=0.1) / Haiku 4.5 (ルーター・operation_log) / Cohere Rerank v3.5 (top-30 → top-5) | — | AWS マネージド | teamagent-mcp、OpenClaw | — | IAM Role (推論プロファイル ARN 限定) |
| 8 | **Vertex AI (Gemini 2.5 Flash)** | 動画マルチモーダル分析。請求は GCP `ntv-ai` | — | GCP マネージド | teamagent-mcp (video tools) | — | Vertex SA JSON (Secrets Manager → `load_secrets.sh`) |
| 9 | **worker EC2** (t4g.medium / arm64) | **ingest 専用常駐** (Slack history + Drive + Sheets → e5 → pgvector)。週次 systemd timer (W1-③) + #ops 失敗通知。office 本文抽出 (W2-④)。SSM のみ／IMDSv2／インバウンド 0／swap 4GB | `scripts/deploy_to_ec2.sh --go` で tarball + venv + Playwright chromium(arm64) | EC2 `i-0feaa3c…` | (ingest のみ) | RDS、Slack API、Drive/Sheets API | `scripts/load_secrets.sh` |
| 10 | **AiLa 別 repo** (`~/Documents/AI-IA-UAE`) | 朝メール秘書の本体。`PLAN_FULL.html` 設計図。Phase1 (Milestone1=2026-06-08) 完了 | Python / Agent SDK | ローカル開発のみ。本番は Wave4 で OpenClaw 経由結線 (`connect.vectorinc.co.jp` 依存) | (未結線) | Gmail (OAuth, TeamAgent から流用)、Bedrock、Google カレンダー | TeamAgent の OAuth 基盤 |
| 11 | **Secrets Manager** | xoxb/xapp/bearer/gateway-token/DB url/sentry_dsn/ops-slack-webhook/Vertex SA を集約。平文露出禁止 | — | AWS マネージド | 全コンポーネント | — | — |
| 12 | **CloudWatch Logs Insights** | 本番 SLI 一次ソース。`/teamagent/dev/teamagent-mcp` (JSON Lines・40afded で実装、`bundled_deploy_2026-06-16` で live)、`/teamagent/dev` (openclaw stream prefix) | — | AWS マネージド | — | — | — |

---

## 2. データフロー（実機・@AiLa 3.3 秒）

```
[1] 営業 (Slack)    @AiLa ◯◯社の提案で響いた訴求は？
        │
[2] OpenClaw       Socket Mode 受信 → request_id 採番 → LLM(Haiku) で
   (Fargate)         ルーティング判定 (search Skill 呼出)
        │  HTTP (streamable-http) + Bearer + X-Request-Id + X-User-Id + X-Channel-Id
        ▼
[3] teamagent-mcp  RLS GUC を user_id / company_domain で SET
   (Fargate)         ├─ LocalE5Embedder (multilingual-e5-large, 1024d)
                     ├─ PgVectorClient.search_similar() (HNSW cosine, top-30, クライアント名ブースト)
                     ├─ Cohere Rerank v3.5 (top-30 → top-5、東京)
                     ├─ min_relevance ゲート (弱根拠は「記載なし」=反ハルシネーション)
                     └─ Bedrock Converse (Sonnet 4.6, T=0.1, system prompt cachePoint)
        │  SearchOutput{answer, hits[], total_cost_usd}
        ▼
[4] OpenClaw       Slack thread に投稿 (Idempotency-Key 付き)
        │
[5] CloudWatch     structlog JSON ライン:
                     {event:"bedrock_converse", request_id, skill:"search",
                      model_id, input_tokens, output_tokens, cache_read_input_tokens,
                      cost_usd, latency_ms}
                     → metric filter (McpCostUSD / McpToolError / McpIdentitySpoofRejected)
                     → CloudWatch アラーム → SNS → メール (→ AWS Chatbot で Slack 化)
```

ingest 経路（左下）: worker EC2 で `teamagent-ingest.service` (systemd timer, 週次) が Slack/Drive/Sheets → chunk → e5 → RDS。失敗時は `OPS_SLACK_WEBHOOK` (Secrets Manager) で `#ops` に通知（W1-③）。

---

## 3. Observability — PDCA で毎日見る指標

### 3.1 即見るダッシュボード

**`scripts/pilot_health.py --hours 24`** が SLI を CloudWatch Logs から集計 → `slo_v1.md` と照合 → GO/NO-GO を返す。**パイロット中は毎朝1回・PDCA loop の Plan フェーズで状態確認**（cron 自動化は Wave3 候補）。

| シグナル | 出所 | 目標 | アラーム |
|---|---|---|---|
| **検索 p95 latency** (`event=bedrock_converse`.latency_ms) | CW `/teamagent/dev/teamagent-mcp` Q5 | **≤ 15s**（中量 Skill） | `p95-latency-high`（15分窓） |
| **エラー率** (`event=*_failed` / `level=error`) | Q1+Q2 | ≤ 1% / 24h | `error-spike`（5分窓 ≥3）／`McpToolError` |
| **1 検索コスト p50** (`cost_usd`) | Q3/Q4 | **≤ $0.02**（cache 前提） | `McpCostUSD`（日次 > $5）／AWS Budgets |
| **cache_hit 率** (`cache_read_input_tokens / input_tokens`) | Q10 | **≥ 80%**（連続トラ前提） | 低トラ時 0% は許容（TTL ~5min） |
| **Skill 別／ユーザー別利用回数** | Q6/Q7/Q9 | ヘビーユーザー按分・content 偏り検知 | — |
| **MCP 可用性** (RunningTaskCount/desired) | ECS metric | ≥ 99.0% / 月 | （未配線・Wave3 候補） |
| **ingest 鮮度** (last_success) | `journalctl -u teamagent-ingest` | ≤ 8 日 | #ops Slack alert (W1-③) |
| **RLS 越権テスト** (DB-gated pytest 28件) | `tests/test_db_*_rls_*.py` | live RDS で **月次手動 PASS**（PDCA loop からは実行禁止／RULES.md §1.3） | 1件 fail で P0 |

### 3.2 RLS 越権の継続検査（セキュリティ核心）

- DB-gated pytest 28 件を **SSM トンネル経由で live RDS に対し月次手動実行**（2026-06-15 全 PASS）。
- PDCA loop からは **絶対に実行しない**（PDCA_LOOP.md §9 / `RUN_DB_TESTS=0` 強制）。Skill が DB-gated test を起動しようとしたら Checker が即 block。
- 観点: GUC 未設定→0 件 / 本人→自分の行のみ / 他人 read 0・update 0 行 / 空 GUC→0 件 / acl_groups intersect で会社ドメイン可視・対象外不可視。

### 3.3 コスト

| 指標 | 目標 | 計測 |
|---|---|---|
| Bedrock 月次 (Sonnet+Haiku+Rerank) | $200〜400 | AWS Budgets `TeamAgent-Bedrock-Monthly` ($50→$250 更新要) + Q3 |
| Server 月次 (Fargate+EC2+RDS+S3+Secrets) | < $267 | AWS Budgets `TeamAgent-Server-Monthly` |
| Gemini Vertex | VSEO 1 回 ≈ $0.01〜0.03 | GCP Billing（会社アカウント化推奨） |
| **PDCA loop 自身のコスト** | **≤ $10/日 (≈ 1500円)** / **≤ $200/月** | Skill 起動時に `aws ce get-cost-and-usage` 直接照会・超過なら即 abort（RULES.md §1.9） |

### 3.4 観測性の既知ギャップ

1. **`usage_events` テーブルは本番経路を記録しない**: 旧 `slack_bot.handle_app_mention` 専用。本番 SLI は **CloudWatch Logs だけが一次ソース**。Wave3 で `pilot_health.py` 日次 cron 化 or MCP 側 usage 記録。
2. **prompt caching が低トラフィックで不発**: cachePoint TTL ~5分。go-live n=3 で `cache_read=0`。連続トラ下で再評価。
3. **app 経路 (旧 EC2 slack_bot) の `/teamagent/dev` ロググループは 0 bytes**＝CloudWatch agent 無し。本番経路ではないため優先度低。
4. **subagent コスト**は `usage_events` に乗らない。PDCA loop は `aws ce` で別途監視（§3.3 参照）。

---

## 4. 変更時に壊れやすい箇所（loop が警戒する 6 領域）

### 4.1 Terraform — **plain `terraform apply` 禁止**

| 罠 | ガード |
|---|---|
| `worker.tf` ドリフト | **`-target` 指定の plan/apply のみ**（runbook §3 順序: role→SG→cluster/log/cloudmap→taskdef→service） |
| live taskdef `teamagent-dev-mcp:5` は **手動 register** | `fargate.tf` の env 変更は手動 register 側にも反映必須。`STRUCTLOG_FORMAT=json` 取り込み漏れ事例あり |
| ECR **immutable tag** | 既存タグ再利用不可。新タグ採番 |
| OpenClaw タスクロール `secretsmanager:GetSecretValue` **Deny** | IAM Policy Simulator で拒否確認（金庫越境の防壁） |

### 4.2 CodeBuild → zip → S3

| 罠 | ガード |
|---|---|
| `git archive` 直前に dev pull 忘れ | `git checkout dev && git pull` を runbook §3① で明示 |
| `WITH_SCRAPE_TOOLS=true` 失念 → tiktok/video tools 欠 | env override 固定 |

### 4.3 ECS rolling update

| 罠 | ガード |
|---|---|
| MCP と OpenClaw の **デプロイ順序逆転** | runbook §3: MCP を先に register/update → 安定化 → OpenClaw を update |
| Slack **Socket Mode 二重接続** | `ec2_cutover_runbook.md` 厳守。go-live で旧 slack_bot 引退済 |
| 会話メモリ (`~/.openclaw/memory` SQLite) は **ephemeral** | P1 許容。P2 で EFS / stateless 判断 |
| Bedrock モデル ID は **推論プロファイル形式必須** (`jp.anthropic.claude-haiku-4-5` 等)。**この文字列の変更を含む diff は Checker が必ず block 初期値**（RULES.md §1.4） | `aws bedrock list-inference-profiles`／両 config 更新 |
| `STRUCTLOG_FORMAT=json` 落としで **アラーム全沈黙** | 手動 register env に明示。`bundled_deploy_2026-06-16` で永続化 |

### 4.4 RDS マイグレーション

| 罠 | ガード |
|---|---|
| マイグレは **SSM トンネル経由 psycopg のみ** | `docs/v3.2/ops/local_dev_with_tunnel.md`／`scripts/db_proxy.sh` |
| 0011 RLS マイグレは `<COMPANY_DOMAIN>` 置換必須 | `sed 's/<COMPANY_DOMAIN>/vectorinc.co.jp/g'` を runbook §5 で明示 |
| **migration の前後順序 / rollback SQL 欠落** | Checker が `risk_flags: touches_db_schema` 時に rollback SQL 有無を必須検査（PDCA_LOOP.md §6） |

### 4.5 CI（GitHub Actions）

| 罠 | ガード |
|---|---|
| 依存は **`pip install --no-deps` + `ci.yml` 手動列挙**。pyproject 欠落 (aiohttp/httpx) で main 長期赤化の前科 | 新 import 追加時は `.github/workflows/ci.yml` にも追記。`ruff format --check` 必須。Checker が `pyproject.toml` 変更時に ci.yml の対応行有無を必須検査 |

### 4.6 トークン rotation（Wave2-⑦ 残）

- Day1 にチャット露出した `xoxb-` / `xapp-` / Vertex SA は **Sprint 14 までに rotation 完了** が本運用ゲート条件（`slo_v1.md` §7）。
- `docs/v3.2/ops/secrets_rotation_policy.md` に従い Reinstall App → 新トークン → Secrets Manager 更新 → ECS `update-service --force-new-deployment`（**人間ゲート**）。

---

## 5. PDCA loop が次に触る箇所（Wave 別）

| Wave | 触るコンポーネント | 注意点 |
|---|---|---|
| **Wave2** office 本文抽出 / 増分同期 / SLO 文書 / token rotation | worker EC2 (ingest)、`docs/v3.2/slo_v1.md`、Secrets Manager | ingest 専用なので本番 MCP は無停止 |
| **Wave3** proposal_deck マージ + 本番配線 / PDF 変換 | teamagent-mcp (gated `USE_PROPOSAL_DECK_TOOLS=1`)、S3 publish | env を **付けない限り無害**。露出は人間承認 |
| **Wave4** AiLa OpenClaw 結線 (§Q-Q3) / 朝配信 / 10名展開 | aiia-mcp (別 repo) → OpenClaw に MCP 接続追加、`connect.vectorinc.co.jp` | TeamAgent OAuth 基盤を流用。Phase2 で Gmail 実接続 + Bedrock。**Wave4 サイクルは Plan 後に status=stopped-by-human で必ず人間承認待ち**（PDCA_LOOP.md §6） |
| **後送り** 負荷試験 / DR 訓練 / 大型リファクタ | 全体 | 本運用 (2026/12〜) の条件 |

---

## 6. 参照リンク

- 全文脈計画書: `~/.claude/plans/mossy-snacking-locket.md` §T／§Q／§V
- 103 項目の最適順序: `~/.claude/plans/abstract-zooming-raccoon.md`（Wave1 完了・Wave2-4 残）
- 元仕様 vs 現状 317 要件突合: `docs/v3.2/spec_vs_current_full_matrix_2026-06-15.md`
- 未着手 76 項目の優先順位: `docs/v3.2/not_done_priorities_2026-06-15.md`
- 12 カテゴリ判定マトリクス: `docs/v3.2/architecture_plan_vs_current_v2.html`
- フル仕様: `docs/v3.2/system_reference.md` / `docs/v3.2/architecture_and_flows.md`
- SLO 契約: `docs/v3.2/slo_v1.md`
- デプロイ: `docs/openclaw/deploy_runbook.md` / `docs/v3.2/bundled_deploy_2026-06-16.md`
- 観測性: `docs/v3.2/ops/observability_and_security.md` / `docs/v3.2/ops/cloudwatch_queries.md`
- パイロットゲート: `docs/v3.2/pilot_gate_status_2026-06-15.md`
- セキュリティ監査: `docs/security/security_audit_2026-06-12.md`
- AiLa (朝メール秘書): `~/Documents/AI-IA-UAE`（`PLAN_FULL.html`）

---

## 7. 本ファイル改訂ポリシー

- 改訂は Shogo（ユーザー本人）の明示承認が必要。
- Maker subagent は本ファイルを編集できない（RULES.md §1.7／PDCA_LOOP.md §9 allowed_files）。
- 改訂時は version を `vX.Y` で増分し、変更履歴を末尾に追記。
- SHA256 ハッシュは PDCA_LOOP.md §9 の expected hash 表に登録される（改訂時は同時更新が必要）。

### 変更履歴

| 日付 | バージョン | 内容 |
|---|---|---|
| 2026-06-16 | v3.2.1 | 初版。go-live (2026-06-12) / P1 ゲート機械検証 (2026-06-15) / MCP JSON ログ化 (`bundled_deploy_2026-06-16`) 反映。PDCA loop が毎日見る §3.1 と「壊れやすい箇所」§4 を核に配置。red-team の指摘（Bedrock model 変更 / migration rollback / ci.yml 連動 / Wave4 人間ゲート / subagent コスト監視）を反映。|
