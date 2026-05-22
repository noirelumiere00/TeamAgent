# TeamAgent — Claude Code 引継ぎノート

このファイルは Claude Code 起動時に自動で読み込まれます。
プロジェクト概要・現状・次のタスクをここに集約しています。

---

## 0. プロジェクト概要

**TeamAgent v3.1** — 社内営業16名向け Slack ベース AI Agent プラットフォーム。

### アーキ核心
- **OpenClaw Runtime** をオーケストレータに（フル採用）
- **AWS Bedrock 経由** で Claude Sonnet 4.6 / Haiku 4.5 を呼ぶ
- **pgvector (PostgreSQL 16)** でデータ層
- **Skill Registry / Plugin** パターンで Skill 数は無限に拡張可能
- **Gemini 2.5 Flash** で動画分析（Skill ④）

### スケジュール
14 Sprint × 2 週 = 7 ヶ月（5 月〜12 月 2026）
Go/No-Go gates ①(Sprint 2末) ②(Sprint 10末)

### コスト枠
- Dev: ¥80K 一時
- Ops: ¥100K〜¥1M/月（規模次第）

---

## 1. リポジトリ構造

```
TeamAgent/
├── CLAUDE.md                  ← このファイル（Claude Code 自動読込）
├── README.md                  ← 一般向け README（v3.1 版）
├── README.v2.md               ← v2.x 時代の README バックアップ
├── docs/
│   ├── v3.1/                  ← 最新ドキュメント（OpenClaw フル採用版）
│   │   ├── teamagent_overview_v3.1.html
│   │   ├── teamagent_implementation_plan_v3.1.html
│   │   ├── teamagent_mva_spec_v1.1.html
│   │   ├── teamagent_subsidiary_questions_v2.md
│   │   └── teamagent_search_skill_design_v1.md   ← 🆕 検索 Skill 設計詳細
│   ├── v3.0/                  ← v3.0 で生きてる現役ドキュメント
│   │   ├── teamagent_requirements_v3.0.html
│   │   ├── teamagent_architecture_v3.0.html
│   │   ├── teamagent_personal_ai_coach_requirements_v3.0.html
│   │   ├── teamagent_kinou4_spec_v1.html
│   │   ├── teamagent_phase0E_spec_v1.html
│   │   └── ...
│   ├── archive/               ← v1.5/v2.0/v2.1/v3.0_superseded
│   └── README.md
├── src/                       ← Python 実装（Skill Registry スケルトン）
│   ├── README.md
│   └── skills/
├── infra/
│   ├── terraform/             ← AWS Terraform（RDS pg16 + IAM + S3）
│   │   ├── main.tf / variables.tf / rds.tf / lambda_iam.tf / outputs.tf
│   │   └── terraform.tfvars.example
│   └── docker/                ← ローカル開発用 docker-compose
│       ├── docker-compose.yml （pgvector + adminer + minio）
│       └── init-pgvector.sql
├── scripts/
│   ├── setup_local.sh
│   ├── demo_pgvector_search.py    ← md ファイル → ベクトル検索
│   └── demo_pdf_vectorize.py      ← 🆕 PDF → ベクトル検索（実資料デモ）
├── tests/
├── data/                      ← gitignore（提案 PDF などの機密データ置き場）
│   └── proposals/
├── pyproject.toml             ← Python 依存（claude-agent-sdk, anthropic, boto3, psycopg, pgvector, sentence-transformers, pdfplumber...）
├── .env.example
├── .gitignore
└── MIGRATION.md               ← v2.2 → v3.1 移行手順（一度完了済）
```

---

## 2. Day 0（2026/5/21）完了状況

✅ **GitHub モノレポ移行**：feat/v3.1-monorepo ブランチ → PR #1 マージ完了
✅ **Docker Desktop + ローカル pgvector**：3 コンテナ稼働中（postgres / adminer / minio）
✅ **Python venv + sentence-transformers**：multilingual-e5-large（1024 次元）動作確認
✅ **smoke_test テーブル**：HNSW index 付き、1 行 INSERT 確認
✅ **md ベクトル検索デモ**：teamagent_subsidiary_questions_v2.md → 20 チャンク
✅ **PDF 実資料ベクトル検索デモ**：data/proposals/ の 3 提案 PDF を検索 → 類似度 0.80〜0.83 の関連 chunk 返却
✅ **営業 8 軸ヒアリング統合**：要件として明文化
✅ **3 Agent 並列調査**：query routing / pgvector ハイブリッド / RAG → docs/v3.1/teamagent_search_skill_design_v1.md に統合
✅ **Claude Code ハンドオフ**：CLAUDE.md + 検索 Skill 設計書 v1 → PR #2 マージ完了

---

## 2-bis. Day 0 夕方追加作業（2026/5/21 19:00〜）

✅ **AWS CLI セットアップ**：`~/.aws/credentials` 設定済み（us-east-1）
✅ **Bedrock 接続 hello world 成功**：
  - 正しいモデル ID は **`us.anthropic.claude-sonnet-4-6`**（推論プロファイル形式、`us.` プレフィックス必須）
  - Claude Haiku 4.5 は `us.anthropic.claude-haiku-4-5-20251001-v1:0`
  - ❌ NG 例：`anthropic.claude-sonnet-4-6-20251022-v1:0`（こちらは存在しない）
  - ❌ NG 例：`anthropic.claude-sonnet-4-6`（on-demand 非対応）
✅ **Bedrock モデルアクセスページ廃止確認**：2026/5 時点で事前有効化は不要、初回呼び出し時に自動有効化
✅ **AWS Budgets 設定**：
  - `TeamAgent-Bedrock-Monthly` $50/月（Bedrock のみ）
  - `TeamAgent-Server-Monthly` $267/月（≒¥40,000、RDS / EC2 / S3 / Lambda など）
  - 通知閾値：50% / 80% / 100%
  - 送信先：`s-komata@vectorinc.co.jp`, `NewsTV_AWS_AIagentAdmin@vectorinc.co.jp`

---

## 2-ter. Day 1（2026/5/22）作業実績

### インフラ（午前〜午後）
✅ **Terraform 1.12.2 インストール**（tfenv 経由、sudo 不要）
✅ **tfstate バックエンド構築**：S3 `teamagent-tfstate-718959508629` + DynamoDB `teamagent-tflock`
✅ **AWS Terraform apply 完了**：東京リージョン 23 リソース稼働
  - RDS PostgreSQL **16.14** (`db.t4g.micro`, 20GB)
  - EC2 踏み台（SSM Session Manager のみ、SSH 22 ポート閉鎖）
  - S3 raw files バケット
  - IAM Role / Secrets Manager
✅ **session-manager-plugin インストール**（pkg 展開で sudo 不要回避、`~/.local/bin/`）
✅ **踏み台に psql 16.12 導入**
✅ **本番 RDS に `CREATE EXTENSION vector` 実行**：pgvector **0.8.2** 導入確認
✅ **踏み台 IAM に Secrets 読み取り権限追加**（最小権限）
✅ **PostgreSQL バージョン整合**：terraform.tfvars.example / variables.tf を 16.14 に統一

### コード基盤（午後〜夕方）
✅ **src/teamagent/ 3層分離パッケージ構築**（CLAUDE.md 6-bis 準拠）：
  - `adapters/bedrock_client.py`（Converse + usage/cost/latency ロギング）
  - `adapters/pgvector_client.py`（psycopg + ベクトル検索ヘルパー）
  - `adapters/slack_client.py`（slack_sdk AsyncWebClient ラッパー）
  - `skills/base.py`（BaseSkill / Registry / SkillContext）
  - `skills/search/` 雛形（schema.py + skill.py）
  - `runtime/local.py`（CLI エントリポイント）
  - `runtime/slack_bot.py`（Bolt Socket Mode）
  - `prompts/search/v1/system.md`（コード外 prompt）
✅ **テスト整備**：16 件 all PASS（adapter モック化、SkillRegistry、SearchSkill happy path）
✅ **mypy --strict 通過**：15 source files、no issues
✅ **demo スクリプトのハードコード除去**：DATABASE_URL 環境変数化

### Slack 連携
✅ **Slack App 作成**：`TeamAgent Ver.2` / `TeamAgent_Dev_Ver.2`（App ID `A0B51FGQ8JK`）
✅ **OAuth スコープ 17 個付与**：app_mentions:read / chat:write / chat:write.public /
   channels:* / groups:* / im:history / im:write / commands / reactions:read /
   users:read / users.profile:read / files:read / files:write / dnd:read / usergroups:read
✅ **Event Subscriptions**：`app_mention` + `message.im` 購読
✅ **Socket Mode 有効化** + Messages Tab 有効化（DM 受信可能）
✅ **Secrets Manager にトークン保管**：
  - `teamagent/dev/slack/bot_token`（xoxb-）
  - `teamagent/dev/slack/app_token`（xapp-）
✅ **実機疎通成功**：
  - チャンネル `#bot_test_server` でメンション → echo 返信 ✅
  - DM → echo 返信 ✅

### 統合状況
✅ **Sprint 1 P0「AWS Bedrock 接続」完了**
✅ **Sprint 1 P0「Terraform apply」完了**
✅ **Sprint 1 P1「Slack コネクタ着手」echo Bot まで完成**

### Day 2 夕方 — End-to-End 疎通成功（2026/5/22 15:05）
✅ **Anthropic Use Case フォーム承認**：us-east-1 で Claude Sonnet 4.6 / Haiku 4.5 利用可能
✅ **LocalE5Embedder 実装**：multilingual-e5-large（1024次元、ローカル sentence-transformers）
✅ **PgVectorClient 柔軟化**：content_col / metadata_col / extra_cols を引数化、本番 RDS でも proposals_chunks でも対応
✅ **SkillDispatcher 実装**：runtime/slack_bot.py の mention → SearchSkill.run() → Bedrock 要約
✅ **Slack 実機 E2E 疎通成功**（3 連投すべて成功）：
  - クエリ「飲食業の提案実績を教えて」「PR代行の業界別実績は？」「競合動画の分析事例は？」
  - 1 クエリあたり **コスト約 $0.01-0.02 / レイテンシ 7-11 秒**
  - top similarity score 0.80〜0.84
✅ **テスト 24 件 all PASS、mypy --strict 16 source files クリア**

### ⚠️ OpenClaw 採用ステータス変更（2026/5/22）
🟡 **「フル採用」→「再評価中」に変更**
詳細は [docs/v3.1/teamagent_design_corrections_2026-05-22.md](docs/v3.1/teamagent_design_corrections_2026-05-22.md) 参照

理由（v3.1 ドキュメントの記述ズレ）：
1. **Agent SDK 互換は誤認** — OpenClaw 公式が `claude-agent-sdk` 採用を [Issue #10149](https://github.com/openclaw/openclaw/issues/10149) で却下
2. **TypeScript/Node.js 製** — TeamAgent (Python) との言語境界課題
3. **ClawHub にサプライチェーン事例** — ClawHavoc インシデント（341 件悪意 Skill、9,000+ 被害）

→ 現在の `boto3 + slack-bolt + 自前 Skill Registry + claude-agent-sdk` 構成は**そのまま継続可能**。
→ OpenClaw 採否の最終判断は **Sprint 2 末ゲート①（2026-06-07）** で確定する。

### Day 2 夜 — Contextual Retrieval + Drive リンク + メタデータ抽出（2026/5/22 17:30）
✅ **Contextual Retrieval 実装（PR #16）**：Haiku 4.5 で各 chunk に前置詞生成 + 再 embedding
  - 98 chunks 処理コスト $0.21、平均 $0.002 / chunk
  - INPEX クエリで top-1 score **+3.69 ポイント改善**
  - SearchSkill に use_contextual オプション追加
✅ **Drive リンク Phase 1（PR #18）**：検索結果に「📎 Drive で開く」ボタンを Block Kit で表示
  - proposals_chunks に drive_url 列追加
  - data/proposal_drive_map.json で file_name → URL 手動マッピング
  - Slack 返信を Block Kit 化、SearchHitOut.drive_url 追加
✅ **メタデータ抽出パイプライン（PR #19）**：Sonnet 4.6 で各 PDF から JSON 抽出
  - industry / client_company / target_audience / service_type / proposed_at / key_keywords
  - 3 PDF を $0.07 / 14.8 秒で処理
  - filter_industry='エネルギー' → INPEX 提案のみ、'不動産' → 森ビル提案のみ が動作確認
✅ **AWS リソース・Memory 整備**：Memory に AWS / リポジトリ / Agent 一次ソース確認ルールを追加
✅ **OpenClaw + Bedrock 検証**：AWS Lightsail Blueprint / aws-samples 公式 CFN（ap-northeast-1 対応）/ AWS_PROFILE 罠を実証データで確認
✅ **v3.2 設計ドラフト 3 ファイル（PR #15）**：overview + migration runbook + implementation plan

### Day 2 完了時点の累計 PR：#1〜#19（19 本マージ）

### ⚠️ TODO（Sprint 2 開始時に対応）
- [ ] **data/proposal_drive_map.json の PLACEHOLDER を実 Drive URL に差し替え**（Sprint 3 の Drive API 連携で自動化されるので、それまでは保留可）
- [x] **本番 RDS への migration**：ローカル → 東京 RDS proposals_chunks_contextual（PR #22 で完了）
- [ ] **Drive 取り込みパイプライン（Sprint 3）**：Google Drive API + webViewLink 自動取得

### 🔐 Slack トークン管理メモ
- xoxb- が会話履歴に露出した経緯あるが、チャットは private で外部漏洩リスクなしと判断
- 定期ローテーション（180 日）は `docs/v3.2/ops/secrets_rotation_policy.md` の手順で実施

---

## 3. ローカル開発環境の使い方

### コンテナ起動
```bash
cd ~/Documents/TeamAgent/infra/docker
docker compose up -d
docker ps                                              # 3 コンテナ確認
```

### Python venv（既に作成済み）
```bash
cd ~/Documents/TeamAgent
source .venv/bin/activate
```

### DB 接続情報
- Host: `localhost:5432`
- User/Password: `teamagent / teamagent`
- Database: `teamagent`
- Adminer GUI: http://localhost:8080
  - System: PostgreSQL / Server: `postgres` / その他: teamagent
- MinIO Console: http://localhost:9001（teamagent / teamagent-local）

### デモ実行
```bash
# md ファイル検索
python scripts/demo_pgvector_search.py

# PDF 検索（data/proposals/ に PDF を置く）
python scripts/demo_pdf_vectorize.py
```

---

## 4. 既知の課題（Day 0 で判明）

### 4-1. 「業界は？」のような meta-query は pure vector search では機能しない
- 例：「業界は？」と聞くと「クリエイティブ」「お見積り」など意味的に近いだけの無関係チャンクが返る
- 根本原因：vector search は「答え」ではなく「似てる文章」を探す仕組み
- **解決策**：Query Router + JSONB メタデータ + Claude RAG
- 詳細：`docs/v3.1/teamagent_search_skill_design_v1.md` の Section 3 以降

### 4-2. ハイブリッド検索が必須
- 営業ヒアリングで判明した 8 軸：
  - 構造化（業界・予算・商材・担当者・部署・自社サービス）→ JSONB フィルタ
  - セマンティック（文脈・マルチコンテキスト・インサイト）→ vector search
- 両者を WHERE + ORDER BY で組み合わせる必要

---

## 5. Sprint 1 タスク（次にやること）

優先度高い順：

### ✅ P0: AWS Bedrock 接続（完了 2026/5/21）
- [x] ~~AWS コンソールで Bedrock モデル有効化~~ → 仕様変更で不要（初回呼び出し時に自動有効化）
- [x] IAM 認証情報設定（aws configure / us-east-1）
- [x] `pip install boto3` 完了
- [x] hello world 成功（`us.anthropic.claude-sonnet-4-6` で「こんにちは！」返答確認）
- [x] AWS Budgets 設定（Bedrock $50/月 + Server $267/月、50/80/100% アラート）

### ✅ P0: Terraform apply（完了 2026/5/22）
- [x] ~~tfvars 作成~~（東京リージョン / db.t4g.micro / pg 16.14）
- [x] terraform init + S3 backend
- [x] terraform apply 完了（23 リソース）
- [x] RDS 接続確認（踏み台 + SSM + psql 16.12）
- [x] **pgvector 0.8.2 を本番 RDS に CREATE EXTENSION**

### ✅ P1: Slack コネクタ着手（完了 2026/5/22 — echo Bot まで）
- [x] Slack App 作成（`TeamAgent_Dev_Ver.2`）
- [x] OAuth スコープ 17 個付与
- [x] Event Subscriptions（app_mention + message.im）
- [x] Socket Mode + Messages Tab 有効化
- [x] Bot/App Token を Secrets Manager に保管
- [x] `src/teamagent/adapters/slack_client.py` 実装
- [x] `src/teamagent/runtime/slack_bot.py` 実装（Socket Mode）
- [x] 実機 echo 疎通成功（チャンネル + DM）
- [ ] **次：mention テキストを SearchSkill にディスパッチ（Sprint 2）**

### 🟡 P1: Contextual Retrieval（既存チャンクに前置詞付与）
- [ ] 既存の demo_pdf_vectorize.py で生成された chunk に Claude Haiku で「この章は...」前置詞を生成
- [ ] 再 embedding して proposal_chunks テーブルへ
- [ ] retrieval error が下がるか測定

### 🟡 P1: メタデータ抽出パイプライン
- [ ] Claude Sonnet で「この PDF の業界・予算・ターゲット・担当者を JSON で抽出」プロンプト
- [ ] 初期 PDF を全件処理 → JSONB メタデータ列に保存
- [ ] 詳細スキーマ：`docs/v3.1/teamagent_search_skill_design_v1.md` Section 3

### 🟢 P2: Query Router
- [ ] meta / content / conditional / compare のルーティング実装（最初はルールベース）
- [ ] meta query → SQL 集計、content → vector search
- [ ] 後で Claude Haiku ベース版に置き換え

### 🟢 P2: 子会社エンジニアへ質問リスト送付
- [ ] `docs/v3.1/teamagent_subsidiary_questions_v2.md` をメールで送付
- [ ] 期限なし、回答が来たら設計に反映

---

## 6. 重要な技術的判断（変えないでほしい）

1. **OpenClaw フル採用**（v3.1 で確定）— 不採用にしない。子会社運用実績 + セキュリティ運用ルールでカバー
2. **AWS Bedrock 経由で Claude を呼ぶ** — Anthropic API 直接ではなく Bedrock 必須
   - 理由：2026/4 の Anthropic サブスク制限事件があったため、Bedrock 経由で政策変動を遮断
3. **pgvector 0.8.0 以上を必ず使う** — 古いとフィルタで結果ゼロのバグ
4. **temperature=0.1 + 引用必須化** — ハルシネーション抑制の鍵
5. **prompt caching を必ず使う** — system prompt + 頻出 context で 90% コスト削減

---

## 6-bis. AI エージェント実装ルール（メンテ性最優先）

新しい Skill / Lambda / バッチを書くときは以下を必ず守る。

### Do（守ること）
1. **3層分離**：`skills/`（ビジネスロジック）/ `adapters/`（Bedrock・pgvector・S3クライアント）/ `runtime/`（Lambda・ECS・local エントリポイント）— Skill から Lambda を見せない
2. **Pydantic v2 で I/O 固定**：Skill の input / output は必ず `pydantic.BaseModel`。dict をそのまま返さない
3. **型ヒント + `mypy --strict`**：CI で型エラーは fail にする
4. **構造化ログ（JSON）+ request_id 伝播**：`{"request_id": "...", "skill": "...", "event": "...", "token_usage": {...}, "latency_ms": ..., "cost_usd": ...}` を全層で同じ request_id で出す
5. **prompt はファイル化 + Git 管理**：`src/prompts/<skill>/v1/system.md` のように versioned。コード内文字列リテラル禁止
6. **Bedrock 呼び出しごとに usage / cost を必ずログ**：`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `model_id`, `latency_ms`, `cost_usd`
7. **テスト**：Skill 単位で pytest（adapter は moto / fake でモック）。最低でも happy path + 1エッジケース
8. **Slack 投稿 / DB 書き込みは Idempotency-Key 付き**：リトライで二重投稿しない

### Don't（やってはいけないこと）
- ❌ **エラーログに生入力（提案PDF全文・顧客名・会話履歴）を入れる** — PII / 機密漏洩。代わりに `request_id` だけログし、本体は `s3://teamagent-dev-debug-snapshots/<request_id>.json`（KMS 暗号化、TTL 30日）に置く
- ❌ **「AI の推論スコア」を曖昧にログ** — Claude に confidence score はない。代わりに retrieval_similarity / token_usage / latency をログ
- ❌ **Skill ファイル内で boto3 を直叩き** — 必ず `adapters/bedrock_client.py` 経由（テストでモック差し替え可能にするため）
- ❌ **prompt をコードに hard-code** — `src/prompts/` 配下の `.md` を読み込む形に統一

### コードレビューの定型チェック
- [ ] Pydantic スキーマで入出力が定義されているか
- [ ] `mypy --strict` が通るか
- [ ] 構造化ログに request_id / token_usage / cost_usd が出るか
- [ ] 機密データが CloudWatch に出ていないか（grep で確認）
- [ ] prompt が `src/prompts/` 配下に分離されているか
- [ ] pytest で happy path が通るか

---

## 7. 設計の参照優先順

1. **`docs/v3.2/teamagent_master_todo_v1.md`** ← Sprint 14 までの全タスク（最重要）
2. **`docs/v3.1/teamagent_design_corrections_2026-05-22.md`** ← v3.1 訂正ノート v0.3
3. **`docs/v3.1/teamagent_search_skill_design_v1.md`** ← 検索 Skill 実装の詳細
4. `docs/v3.2/teamagent_overview_v3.2_draft.md` ← v3.2 設計ドラフト
5. `docs/v3.2/teamagent_migration_runbook_v3.2_draft.md` ← v3.1→v3.2 移行手順
6. `docs/v3.2/teamagent_implementation_plan_v3.2_draft.md` ← Sprint タイムライン

## 7-bis. 運用ドキュメント

- **`docs/v3.2/ops/cloudwatch_queries.md`** — CloudWatch Logs Insights クエリ集（10 個）
- **`docs/v3.2/ops/secrets_rotation_policy.md`** — Secrets Manager ローテーションポリシー

---

## 8. Cowork（別環境）で過去にやった作業

このリポジトリの主要ドキュメント・設計は Anthropic の Cowork で作成済み。
Cowork session ID は `f3ed19ba-6169-4b92-b621-189d74ae07cb`（参考程度）。
Cowork outputs フォルダ：
`/Users/s-komata/Library/Application Support/Claude/local-agent-mode-sessions/...`

このリポジトリに必要なファイルは既に Day 0 でコピー済み。
今後のコーディングは Claude Code で完結可能。

---

## 9. 連絡先・体制

- Project Lead: Shogo Komata (FDE)
- 営業ヒアリング先: 営業16名
- 子会社エンジニア: OpenClaw 120 ユーザー運用実績あり（質問リスト送付予定）

---

## 10. 困ったときに見るドキュメント

| やりたいこと | 見るドキュメント |
|---|---|
| 検索 Skill を実装したい | `docs/v3.1/teamagent_search_skill_design_v1.md` |
| MVA 全体像が知りたい | `docs/v3.1/teamagent_mva_spec_v1.1.html` |
| Sprint スケジュール | `docs/v3.1/teamagent_implementation_plan_v3.1.html` |
| AWS Terraform | `infra/terraform/README.md` |
| ローカル docker-compose | `infra/docker/docker-compose.yml` |
| Skill Registry パターン | `src/skills/README.md` |
| 子会社に聞きたいこと | `docs/v3.1/teamagent_subsidiary_questions_v2.md` |

---

最終更新：2026 年 5 月 21 日（Day 0 完了時点）
