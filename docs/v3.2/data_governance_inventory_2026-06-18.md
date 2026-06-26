# TeamAgent / AiLa データ全体図 と ガバナンス・セキュリティ棚卸し — 2026-06-18

> 他セッション・他作業者向けの**読みもの**。コード変更未実施。
> 一次調査: 3 Explore エージェント並列・読み取り専用・branch `dev`（HEAD `6f22a7d`）。
> 関連: `docs/v3.2/skill_inventory_2026-06-18.md` (Skill 棚卸し), `docs/v3.2/workspace_search_slack_integration_plan_2026-06-18.md` (workspace 連携設計)

---

## TL;DR（1分で読む）

- **基盤は堅牢**：KMS/RLS/at-rest 暗号化/正規化された身元解決まで多層実装済。漏れているのは「**運用と DLP の自動化**」と「**ナレッジベースの削除ポリシー**」。
- **データ規模は小〜中**：S3 約 107MB（VSEO レポート等）／RDS 20GB 確保（実使用量は未測定）／DynamoDB は AI-IA 用 4テーブルがほぼ空。
- **要対応の弱点 4 つ**（優先順）:
  1. 🔴 **LLM 送信前の PII マスクが無い**（Bedrock/Gemini に chunks 本文をそのまま送ってる）
  2. 🔴 **ナレッジベース（documents/chunks）の削除ポリシーが無い**（GDPR 削除要求に対応不能）
  3. 🔴 **secret rotation が全部手動**（9種類のシークレット・180日ポリシーだが手動）
  4. 🟡 **退社時のユーザー token cleanup 手順が無い**

---

## 1. 全体図（1枚で把握）

```
┌─ Slack ─┐  ┌─ Gmail/Drive/Cal ─┐  ┌─ Bedrock/Gemini ─┐
│ ユーザー │  │ Google Workspace  │  │ 外部 LLM         │
└────┬────┘  └─────────┬─────────┘  └────────┬─────────┘
     │              ↑│↓                       ↑PII送信 ⚠未マスク
     ↓              │                          │
┌──────────────────────────────────────────────────────┐
│ ECS Fargate (ap-northeast-1 東京)                    │
│  ┌────────────┐ ┌──────────────┐ ┌──────────────┐  │
│  │ OpenClaw   │→│ teamagent-mcp│ │ aiia-mcp     │  │
│  │ (Slack入口)│ │ (Skill実行)  │ │ (朝メール別件)│  │
│  └────────────┘ └──────┬───────┘ └──────────────┘  │
└─────────────────────────│────────────────────────────┘
                          ↓ RLS GUC (app.user_email)
       ┌──────────────────────────────────────┐
       │ RDS PostgreSQL (db.t4g.micro/20GB)   │
       │ • documents/chunks (ACL+RLS)         │
       │ • oauth_tokens (KMS BYTEA + RLS)     │
       │ • usage_events/audit_log (本文なし)  │
       │ • connector_state/ingest_jobs        │
       └──────────────────────────────────────┘
                          ↑
       ┌──────────────────────────────────────┐
       │ EC2 worker (t4g.medium, teamagent-bot)│
       │ • slack_bot.py (per-user メール経路) │
       │ • ingest pipeline (Drive/Slack→pgvector)│
       └──────────────────────────────────────┘
                          ↑
       ┌──────────────────────────────────────┐
       │ AWS Storage                          │
       │ • S3 raw-files (107MB・90日Glacier)  │
       │ • Secrets Manager (12 secrets)       │
       │ • KMS (oauth-tokens 復号鍵)          │
       │ • DynamoDB aiia-* (ほぼ空)           │
       │ • CloudWatch Logs (30日)             │
       └──────────────────────────────────────┘
```

---

## 2. データ所在マップ（実測）

### 2.1 RDS PostgreSQL（プライマリ・ナレッジ + 認証）

| テーブル | 用途 | 機微度 | アクセス制御 |
|---|---|---|---|
| `documents` | 文書メタ（Slack/Drive/Gmail/PDF統合） | 中 | **RLS** + `acl_emails[]` / `acl_groups[]` |
| `chunks` | 本文の embedding 分割（vector[1024]） | 高（本文・PII 含む可能性） | RLS（documents経由） |
| `oauth_tokens` | per-user Google OAuth refresh token | 🔴 最重要 | **KMS BYTEA 暗号化 + RLS 本人行のみ** |
| `usage_events` | 1リクエスト1行（cost/latency） | 低（本文なし） | RLS（admin のみ SELECT） |
| `usage_event_calls` | Bedrock呼び出し明細 | 低 | 同上 |
| `audit_log` | append-only 監査（誰が何を ingest） | 低（本文なし） | RLS（admin のみ） |
| `connector_state` | ingest の cursor/差分位置 | 低 | RLS |
| `ingest_jobs` | PDF ingest の state machine | 低 | RLS |

**RDS自体の設定**:
- ✅ at-rest 暗号化（AWS 管理 KMS）
- ✅ 接続 TLS 強制（`rds.force_ssl=1`）
- ✅ IAM Database Authentication 有効
- ✅ 自動バックアップ 7日 / PITR 可
- ⚠️ 削除保護（`deletion_protection`）の設定要確認

### 2.2 S3 `teamagent-dev-raw-files`（107MB / 26 objects）

| prefix | 中身 | ライフサイクル |
|---|---|---|
| `vseo-reports/` | VSEO 分析レポート HTML/JSON | 90日→Glacier IR |
| `vseo-proposals/` | 提案書 PPTX/PDF | 同上 |
| `codebuild/` | ECR ビルド source.zip | 同上 |
| `deploy/` | EC2 worker app tarball + env.base | 同上 |
| `migrations/` | DB migration ログ | 同上 |
| `aiia/` | AI-IA 一時データ（返信ドラフト等） | 同上 |

**設定**: ✅ AES256 / ✅ Versioning / ✅ Public Block / ✅ 非現行版 180日で削除

### 2.3 Secrets Manager（12 secrets / 全部 KMS）

| 種別 | 数 | 例 | rotation |
|---|---|---|---|
| 認証token | 5 | SLACK_BOT_TOKEN / SLACK_APP_TOKEN / MCP_BEARER / GATEWAY_TOKEN / VERTEX_SA | ⚠️ 全部手動・180日推奨 |
| DB認証 | 2 | DB master password / database-url DSN | 同上 |
| OAuth/state | 3 | GOOGLE_CLIENT_ID/SECRET / OAUTH_STATE_SECRET | 同上 |
| AI-IA別件 | 4 | aiia/mcp-bearer / google-client-* / oauth-state-secret | 同上 |

**今朝のインシデント**: `OAUTH_STATE_SECRET` が Secrets Manager 未投入で `connect.newstv.co.jp` が500（PR#127で根治中）

### 2.4 KMS / DynamoDB

| リソース | 用途 |
|---|---|
| `alias/teamagent-{env}-logs` | CloudTrail + Bedrock invocation log の暗号化 |
| `alias/teamagent-oauth-tokens` | oauth_tokens.refresh_token_enc の Encrypt/Decrypt（EncryptionContext で per-user 束縛）|
| DynamoDB `aiia-*` (4テーブル) | AI-IA（朝メール別プロジェクト）・**ほぼ空・休眠中** |
| DynamoDB `teamagent-tflock` | terraform state lock |

### 2.5 CloudWatch Logs

- `/teamagent/dev/{app,openclaw,teamagent-mcp,aiia-mcp}` — **30日保持**（JSON Lines・structlog）
- メトリクス: `BedrockCostUSD` / `SkillLatencyMs` / `ErrorCount` / `McpIdentitySpoofRejected` 等

---

## 3. アクセス制御の防御層（4段）

```
[1] Slack 境界           identity_resolver で bot/guest/外部/削除済を拒否
       ↓
[2] MCP Gateway          STRICT モード: OC 申告の email/role/groups を破棄
       ↓                  → resolve_identity で Slack user_id から再解決
       ↓
[3] DB Row Level Security  app.user_email GUC で SELECT 結果を自動フィルタ
       ↓                  acl_emails[] / acl_groups[] / owner_email で評価
       ↓
[4] KMS EncryptionContext  oauth_token 復号時に user_email を AAD で要求
                          → DB を読めても他人の token は復号不可
```

**重要な不変条件**（テスト固定）:
- ✅ OC が `user_role="admin"` 申告 → 観測 role は必ず `"member"`
- ✅ slack_user_id 欠落 → fail-closed（OC 申告 email にフォールバックしない）
- ✅ resolver 失敗 → fail-closed（fail-open 不在）
- ✅ 許可ドメイン外 email → fail-closed（DB query は走らない）

**ガバナンス上重要なテスト 5本**（赤化したら情報漏洩発生）:
| # | テスト | 赤化したら何が起きる |
|---|---|---|
| 1 | `test_db_oauth_tokens_rls_blocks_other_users` | 他人の Google OAuth token が見える |
| 2 | `test_db_rls_enforces_acl` | ACL 崩壊・誰でも全 doc を読める |
| 3 | `test_strict_resolves_and_drops_all_oc_fields` | OC 申告 email で詐称越権 |
| 4 | `test_strict_fuzz_never_admin_and_requires_resolution` | admin role 観測で権限昇格 |
| 5 | `test_resolve_identity_rejects_guest_bot_deleted` | 境界外主体が身元取得 |

---

## 4. ガバナンスの「できてる事」（強み）

- ✅ **本文を保存しない設計**: `usage_events`/`audit_log` は `query_chars`（文字数）のみ
- ✅ **トークンを平文で書かない**: `oauth_tokens.refresh_token_enc` は KMS 暗号化、Sentry スクラブで `[REDACTED_SECRET]`
- ✅ **per-user identity 解決の唯一真実源**: `identity.py` の `build_rls_metadata()` だけが GUC を作る
- ✅ **会社共有ドメイン**: `TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp` で社内ナレッジを RLS 通過
- ✅ **CloudTrail multi-region + log file validation**: KMS で改竄検知
- ✅ **Slack 境界の身元検査**: bot/guest/外部ワークスペース/削除済を全部拒否（5パターン）
- ✅ **Sentry breadcrumb 自動スクラブ**: email/電話/各種 token を `[REDACTED]` 化

---

## 5. ガバナンス上の弱点 ＋ 推奨

### 🔴 弱点 1: **LLM 送信前の PII マスクが無い**
- chunks の本文（顧客メール・電話・氏名含む可能性）が、Bedrock/Gemini にそのまま送信される
- Sentry には PII マスクある（`scrub_value`）が、LLM 送信経路には未適用
- **影響**: Bedrock のログ（CloudWatch）には KMS 暗号化されるが、Anthropic 側のログ保存ポリシーに依存
- **推奨**: 各 Skill の invoke 前に `chunks.content` を regex でマスクする層を入れる（メール → `t***@domain.jp`、電話 → `[REDACTED_PHONE]`）

### 🔴 弱点 2: **ナレッジベース（documents/chunks）の削除ポリシーが無い**
- pgvector に流し込んだ Drive/Slack/Gmail データに TTL なし
- 顧客 GDPR 削除要求が来ても、特定→削除の手段がない
- **影響**: コンプライアンス（個人情報保護法・GDPR）
- **推奨**:
  1. `documents.ingested_at < NOW() - INTERVAL '12 months'` で cascade delete（chunks も連鎖）
  2. `owner_email` 退社時の cleanup スクリプト整備
  3. GDPR 削除請求対応の runbook（owner特定→SQL）

### 🔴 弱点 3: **Secret rotation が全部手動**
- 9 種類の secret が手動運用（180日推奨だが自動化されてない）
- 今朝のインシデント（OAUTH_STATE_SECRET 未投入）も「手動運用の漏れ」が一因
- **影響**: 期限切れで突発障害／長期間放置で漏洩リスク
- **推奨**:
  1. Lambda + EventBridge で自動 rotation（Sprint 14 予定とのこと・前倒し可）
  2. EventBridge ルールで「rotation 期限 30 日前」に Slack 通知
  3. まず Slack 3 本（bot/app/connect oauth）の即座 rotation

### 🟡 弱点 4: **退社時の token cleanup 手順が無い**
- 営業が退社しても `oauth_tokens.user_email` のレコードは残る
- **推奨**: HR/IT と連携した「退職者処理 runbook」（DELETE FROM oauth_tokens WHERE user_email=...）

### 🟡 弱点 5: **Slack 送信内容の audit log 化が無い**
- `operation_log` の Slack スレッド出力は Slack API レスポンスのみ
- 「誰が何を AiLa に頼んで何を受け取ったか」の細かい追跡は限定的
- **推奨**: usage_events に `output_chars` を追加（本文は入れない）

---

## 6. 即座にできる軽い改善（提案）

| # | やること | 影響 | 工数 | 担当 |
|---|---|---|---|---|
| 1 | この文書を他セッションに共有 | なし | 即 | あなた |
| 2 | DynamoDB aiia-* テーブルの停止判断（運用してないので止めて月$余り削減） | 小 | 0.5d | 人間ゲート |
| 3 | ECR 古いイメージ整理（teamagent-mcp 20.5GB → 5GB 想定） | 小 | 0.5d | AI代行可 |
| 4 | Slack tokens の rotation を即時実施（Slack 管理画面で） | 中 | 1h | あなた |
| 5 | LLM 送信前マスクの実装（chunks → masked_chunks 層） | 中 | 2-3d | 別 PR |
| 6 | 12ヶ月 cascade delete の cron job | 中 | 1-2d | 別 PR |
| 7 | rotation 自動化 Lambda（Sprint 14 → 前倒し可） | 大 | 1週間 | 別プロジェクト |

---

## 7. 開いた問い（あなたの判断が要る）

1. **AI-IA-UAE は止める？継続する？**（aiia-mcp Fargate + DynamoDB 4 テーブル＋関連 secrets）
2. **顧客 PII の取り扱い方針**（Gmail/Drive ingest に顧客 PII が混入する前提で、マスクする・しない？）
3. **データ保持期間の目標値**（pgvector は6ヶ月？12ヶ月？無期限？）
4. **退社時の cleanup の責任分担**（IT 側？AiLa 運営側？）

---

## 8. 関連ドキュメント

- `docs/v3.2/skill_inventory_2026-06-18.md` — Skill 棚卸し（22個）
- `docs/v3.2/workspace_search_slack_integration_plan_2026-06-18.md` — workspace_search Slack連携設計
- `docs/v3.2/ops/secrets_rotation_policy.md` — Secret 手動 rotation ポリシー
- `infra/migrations/0006_oauth_tokens.sql` — KMS 暗号化 + RLS の核心
- `src/teamagent/identity.py` — 身元解決の唯一真実源
- `src/teamagent/mcp_gateway/server.py` — STRICT モード anti-spoof
- `docs/openclaw/deploy_runbook.md` — OC デプロイ

---

*この文書は読み取り専用調査の結果です。次回セッションは §5 弱点 1〜3 または §6 即座の改善 から実装に進めます。*
