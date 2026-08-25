# TeamAgent

**Slack を入口に OpenClaw + TeamAgent MCP Gateway + AWS Bedrock Claude で動く社内 AI Agent 基盤。**

ベクトル社の営業 16 名パイロット向け。Slack で `@Aico`（旧称 NewsTV AI / AiLa）に話しかけると、営業ナレッジ検索・クライアントカルテ・提案書生成・メール要約/下書き・カレンダー・TikTok/X 分析などを、per-user 認可と行レベルセキュリティ（RLS）の内側で実行する。

> 📌 本 README は **現在実装されているもの**を説明する（2026-08 時点・dev 基準）。旧 v3.0 設計（「Claude Agent SDK が常駐オーケストレーター」「Slack Bolt が Frontend」「OpenClaw 不採用」）は**現実装と異なる**歴史文書であり、[docs/archive/](docs/archive/) と [docs/v3.0/](docs/v3.0/) に保管されている。将来構想（Hermes）は末尾の Future Roadmap にのみ記載する。

## Current Architecture

```
Slack (Socket Mode)
  ↓
OpenClaw 2026.7.1（外殻: Slack shell・Haiku 4.5 外側ループ・tool 選択）
  ↓  streamable-http :8787 + Bearer + one-use HMAC caller claim
TeamAgent MCP Gateway（信頼境界: identity 解決・RLS・fail-closed・監査）
  ↓
Skill Registry（ToolSpec 最大 40 本・OpenClaw へは toolFilter で 35 本公開）
  ↓
Company Data / APIs（RDS+pgvector / S3 / Google Workspace / Slack / TikTok / X）
  ↓
AWS Bedrock Claude（Haiku 4.5 = ルーティング / Sonnet 4.6 = 重い合成）+ Vertex Gemini（動画）
```

- 開発基準ブランチは **`dev`**（main は大きく遅延している。PR は dev 宛て）。
- `runtime/slack_bot.py`（Slack Bolt 直経路）は go-live で引退した旧経路。現本番の Slack 面は OpenClaw。

## コンポーネント

### OpenClaw（外殻・信頼境界の外）

- upstream `ghcr.io/openclaw/openclaw` を version+digest 固定し、config/SOUL/plugin を焼き込んだ専用イメージ（[infra/docker/Dockerfile.openclaw](infra/docker/Dockerfile.openclaw)）
- 役割: Slack Socket Mode 接続・スレッド返信/ackReaction・セッション分離（dmScope: per-channel-peer）・会話履歴 20 件制限・軽量 tool 選択（Haiku 4.5）
- **できないこと（構造で封鎖）**: exec/shell/fs/browser（tools profile minimal + deny）、RDS/Secrets/Google token への到達（IAM 明示 Deny + SG。渡る secret は Slack tokens / MCP bearer / gateway token / caller claim 署名鍵の 5 つだけ）
- 設定の正: [infra/openclaw/openclaw.config.json5](infra/openclaw/openclaw.config.json5)・ペルソナ: [SOUL.md](infra/openclaw/SOUL.md)・公開 tool 台帳: [effective-tool-scope.json](infra/openclaw/effective-tool-scope.json)

### TeamAgent MCP Gateway（信頼境界・Security Authority）

[src/teamagent/mcp_gateway/server.py](src/teamagent/mcp_gateway/server.py)。OpenClaw からの全 tool call はここを通る:

1. **one-use HMAC caller claim 検証**（[caller_claim.py](src/teamagent/mcp_gateway/caller_claim.py)）: OpenClaw 内の reviewed plugin（[infra/openclaw/caller-identity-plugin](infra/openclaw/caller-identity-plugin)）が Slack event 由来の user/team/channel と tool/全引数ハッシュを署名 claim に束縛。TTL 60 秒・DynamoDB conditional write でクラスタ全体 one-use（replay 不可）
2. **server-side identity 解決**（[identity.py](src/teamagent/identity.py) + `SlackClient.resolve_identity`）: Slack `users.info` で email を解決。guest/外部WS/bot/deleted は fail-closed。**OpenClaw/LLM 申告の email・groups・role は全破棄**
3. **RLS メタ導出**: `build_rls_metadata()` が唯一の変換点。`user_role` は常に `"member"`（admin 昇格の口が構造的に存在しない）
4. tool 実行 → usage_events 記録・進捗表示・payload offload・search リンク注入

### Skill Registry / ToolSpec

- [src/teamagent/skills/](src/teamagent/skills/)（search / clientkarte / proposal_* / mail_* / calendar_* / knowledge_* / tiktok_* / x_research / video_* / slack_summary / attachment_assist / web_research ほか 42 クラス）
- [orchestrator/factory.py](src/teamagent/orchestrator/factory.py) の `build_production_tools()` が `USE_*` フラグで組み立て。skill は Pydantic 入出力 + `run()`、外部 I/O は [adapters/](src/teamagent/adapters/) に隔離（3 層分離）

### Security Boundary / Identity

```
Slack user_id（署名claim内・改ざん不可）
 → SlackClient.resolve_identity（server-side・users.info）
 → ResolvedIdentity{email, groups, is_member}
 → build_rls_metadata（role=member固定・fail-closed）
 → PostgreSQL RLS GUC（app.user_email / app.user_groups / app.user_role）
 → documents/oauth_tokens の行権限・per-user OAuth
```

- RLS: `documents` は owner/acl_emails/acl_groups ベース、GUC 未設定なら何も見えない fail-safe。`FORCE ROW LEVEL SECURITY` + NOBYPASSRLS
- per-user OAuth: Google refresh token は KMS 暗号化（EncryptionContext=user_email で本人束縛）+ RLS 本人行のみ + 同意アカウント id_token 照合 + 配布リンク使い捨て化
- Gmail は下書き作成のみ（送信/削除は adapter denylist で物理封鎖）、Calendar は本人予定登録のみ（招待送信なし）

### RAG / データ層

- RDS PostgreSQL + pgvector。embedding は Local E5（multilingual-e5-large・1024 次元）既定、Bedrock Cohere 切替可（列ペア fail-loud 検証）
- ingest: Slack thread（channel メンバー = ACL）/ Google Drive（permissions 実取得 = ACL）/ Sheets 行単位。Bedrock 分類・Contextual Retrieval・差分取り込み（INGEST_DIFFERENTIAL）
- Web UI: [src/teamagent/connect_web/](src/teamagent/connect_web/)（/search・/app = Aico Vault・Google OAuth callback 受け）

### Agent Runtime（L2・現在 dark）

- [orchestrator/sdk_runner.py](src/teamagent/orchestrator/sdk_runner.py) の `run_sdk_agent` = **anthropic Python client（`AsyncAnthropicBedrock`）による自前 bounded tool loop**
  - Claude Agent SDK は 2026-07-17（`6589e79`）に置換済み（Bun/JS runtime 排除のため）。core イメージでは禁止依存として能動ブロック。ファイル名・関数名に SDK 時代の命名が残るが実体は Bedrock client
  - max_turns / cost cap（$0.5）/ tool timeout / 同一入力反復拒否 / 引用 chunk_id 忠実性照合つき
- MCP tool `run_agent` として `USE_AGENT_ORCHESTRATOR=1` の時だけ露出（**既定 OFF・OpenClaw の toolFilter.include にも意図的に入れていない = dark**）

### Feature Flags

- コード側: `factory.py` の `USE_*`（39 個・既定 ON は USE_CLIENT_BOOST のみ）
- インフラ側: [infra/terraform/fargate.tf](infra/terraform/fargate.tf) の MCP taskdef environment ブロックが単一の注入点（[variables_fargate.tf](infra/terraform/variables_fargate.tf) の `use_*`/`enable_*`）
- OpenClaw への公開は別ゲート: config include + effective-tool-scope.json + 契約テスト + OC イメージ再ビルドの **4 点セット**

## Repository structure

```
src/teamagent/
  mcp_gateway/   # 信頼境界（server / caller_claim / progress / offload）
  identity.py    # 身元→RLSメタの単一変換点
  skills/        # L1 Skill（42クラス）
  orchestrator/  # ToolSpec factory / 自前 agent loop / eval
  adapters/      # 外部I/O（bedrock / pgvector / google / slack / apify …）
  ingest/        # 取り込みパイプライン
  connect_web/   # /search・/app Web UI
  runtime/       # 旧slack_bot（引退済）・usage recorder・request gate
infra/
  openclaw/      # OpenClaw config・SOUL・caller-identity-plugin・scope台帳
  docker/        # 3イメージ（mcp / openclaw / media-worker）+ runtime contract
  terraform/     # ECS/RDS/ECR/署名ゲート/EventBridge/監視
  codebuild/     # 署名リリース鎖（builder/attestor/promoter）
  migrations/    # SQL migrations（RLS 含む）
docs/            # 索引は docs/README.md（v3.2 が現行実装ドキュメント・v3.0 以前は旧設計）
tests/           # unit / integration / scripts(契約テスト)・342ファイル
```

## Development

- 基準ブランチ: `dev`。PR は dev 宛て・CI 全緑 + セルフレビュー必須
- ローカル: Python 3.11+ / uv。テストは CI と同じ extras で（最低 `--extra dev --extra mcp`、media 系は `--extra media` と `npm ci --prefix tools/tiktok_scraper`）
- docker-compose でローカル OpenClaw+MCP+pgvector 一式（[infra/openclaw/docker-compose.yml](infra/openclaw/docker-compose.yml)・[infra/docker/docker-compose.yml](infra/docker/docker-compose.yml)）

## Deployment（署名リリース鎖）

1. source publisher が reviewed commit を KMS 署名付きで evidence bucket へ
2. builder は quarantine ECR にのみ build（release へ書けない）
3. attestor が SBOM + Trivy（Critical=0/High=0/Secrets=0）+ KMS 署名 receipt
4. promoter（release ECR に書ける唯一のロール）が digest 保存で quarantine→verified-candidates→release 昇格
5. terraform plan-time ゲート（image_release_gate.tf）を全 taskdef が depends_on。イメージ参照は必ず `@sha256:` digest

適用は [infra/deploy/terraform_runtime_guard.sh](infra/deploy/terraform_runtime_guard.sh) 経由（素の apply 禁止）。初回 bootstrap は [docs/runbooks/provenance_iam_bootstrap.md](docs/runbooks/provenance_iam_bootstrap.md)。デプロイ後は run-task 検証 → **実 Slack 1 往復まで完了としない**。

## Observability / 制約

- structlog 構造化ログ（request_id 貫通・`mcp_tool_usage` に gateway_ms/latency_ms/tool_cost_usd）
- usage_events テーブル（1 リクエスト 1 行・PII は非空 query のみ例外）
- CloudWatch: コストアラーム・なりすまし検知（identity_spoof_rejected）・Bedrock invocation logging・canary（1h 間隔）
- コスト 4 層ガード: AWS Budgets / 1 実行 cost cap / 外部 SaaS $ 台帳（DynamoDB）/ 動画本数クォータ（Postgres）
- ⚠️ 既知の制約: MCP 経路には現在 admission control（同時実行の総量規制）が無い。対応は [ADR §22](docs/architecture/hermes_migration_design.md) の PR-R として計画済み

## Documentation

- **索引: [docs/README.md](docs/README.md)**
- 現行実装: [docs/v3.2/](docs/v3.2/)（architecture_and_flows / system_reference ほか。※一部に OpenClaw 化以前の記述が残る）・[docs/openclaw/](docs/openclaw/)（deploy runbook・adversarial harness）・[docs/security/](docs/security/)
- 将来設計: [docs/architecture/hermes_migration_design.md](docs/architecture/hermes_migration_design.md)（Hermes 段階導入 ADR）+ [同 implementation plan](docs/architecture/hermes_implementation_plan.md)
- 旧設計: [docs/v3.0/](docs/v3.0/)・[docs/v3.1/](docs/v3.1/)・[docs/archive/](docs/archive/) は歴史文書（現実装の説明としては読まない）

## Future Roadmap — Hermes（計画であり、現在は未使用）

Personal Agent / Memory / Specialist 化のため、Hermes Agent（NousResearch・**OpenClaw からの移行を公式サポートする agent runtime**）を段階導入する**計画**がある。**現時点で Hermes は本番未使用・コードも未追加**。

位置づけ: **TeamAgent を Hermes で作り直すのではなく、自前で持っている Agent Runtime 部分（上記 L2 bounded loop）に Hermes という選択肢を追加する**。Security / MCP / Skills / RAG はすべて既存のまま。Security Authority は永続的に TeamAgent MCP Gateway 側にあり、Hermes には RDS / OAuth token / Secrets / 既存 MCP bearer を渡さない。

- PR2-A0: Supply-Chain Adopt（content-addressed buildspec を hash-keyed append-only 世代モデルへ。Hermes は登場しない）
- PR2-A1: Hermes Supply-Chain Onboarding（upstream image を digest 固定した薄い derived image を署名リリース鎖へ通す）
- PR2-B: Hermes Dark Runtime（PR2-A1 の release digest を ECS へ。常駐タスク 0・受け入れは RunTask のみ）
- PR3: `run_hermes_agent`（`USE_HERMES_ORCHESTRATOR` 既定 OFF・OpenClaw 非公開）+ delegated session claim（per-call MAC・引数束縛・専用 callback boundary）
- PR-R: 容量制御（**実ユーザー routing 前の必須 Gate**）
- PR4-8: Proposal Specialist → Personal Profile → Memory（approval 付き）→ Multi source（policy version による段階解禁）→ AI General Router
- OpenClaw の Keep / Thin / Replace 判断は最終 Phase まで保留（Big Bang 移行はしない）

詳細・Security 不変条件・敵対審査の結果は [ADR](docs/architecture/hermes_migration_design.md) を参照。

## License / Contact

社内利用専用 / Proprietary — Shogo Komata（TeamAgent 推進担当 / FDE）
