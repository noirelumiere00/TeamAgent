# TeamAgent v3.2 設計書（メイン文書 / ドラフト）

> **Status**: ドラフト（Sprint 2 W1 着手用）
> **発行予定日**: 2026-06-07（Sprint 2 末ゲート①でのレビュー後に v3.2 確定版へ昇格）
> **作成日**: 2026-05-22
> **前提となる訂正ノート**: [`docs/v3.1/teamagent_design_corrections_2026-05-22.md`](../v3.1/teamagent_design_corrections_2026-05-22.md)（v0.3 まで反映）
> **置き換え対象**: [`docs/v3.1/teamagent_overview_v3.1.html`](../v3.1/teamagent_overview_v3.1.html)
> **OpenClaw 採用方針**: **B案（HTTP 橋渡し）でドラフト** — 最終確定は Sprint 2 末ゲート①（2026-06-07）

---

## 0. 本書の位置づけ

本書は **TeamAgent v3.2 のメイン設計書**である。v3.1 では「OpenClaw を AI Agent Runtime として正式採用し、Skill 内部ロジックは Claude Agent SDK で実装する」という二層構成を前提に書かれていたが、Sprint 1 Day 2（2026-05-22）の Web 調査と PoC 実装の結果、複数の前提が事実と異なることが判明した。詳細は訂正ノート v0.3 に記録されている。

v3.2 はこの訂正を **取り込んだ後の正しい設計**を提示するものであり、次の 3 つの方針転換を反映する。

1. **OpenClaw は Claude Agent SDK 互換ではない** — OSS の公式 Issue #10149（CLOSED / NOT_PLANNED, 2026-04-24）で `@anthropic-ai/claude-agent-sdk` 採用は却下されている。v3.1 の「OpenClaw Runtime + Claude Agent SDK Skill の二層構成」は破綻している。
2. **OpenClaw は TypeScript / Node.js 製である** — TeamAgent の既存実装は Python（pydantic v2 + boto3 + psycopg + slack-sdk + sentence-transformers）であるため、両者を直結すると言語境界の問題が必ず発生する。
3. **AWS 公式の OpenClaw + Bedrock CloudFormation テンプレートが ap-northeast-1 で利用可能** — `aws-samples/sample-OpenClaw-on-AWS-with-Bedrock` が EC2 + IAM Role + Bedrock 接続を 1 クリック構築できる（API キー不要設計）。これにより B 案（HTTP 橋渡し）の PoC 工数が大幅に下がる。

加えて、Anthropic が 2026/5/13 に発表した **Agent SDK Credits**（6/15 適用開始）は、第三者 Agent からの Claude サブスクリプション利用を再許諾するものだが、TeamAgent は **Bedrock 経由** で Claude を呼ぶ設計であるため影響を受けない。Anthropic 側のサブスクリプション政策変動から AWS Bedrock 経由で遮断されているという v3.1 からの方針はそのまま継続する。

---

## 1. 概要

### 1.1 TeamAgent v3.2 とは

**TeamAgent v3.2** は、ショート動画 PR 会社の**営業 16 名**向け Slack ベース AI Agent プラットフォームである。営業担当者が日常的に使う Slack 上から、過去の提案書・議事録・メール等の社内資料を自然文クエリで検索し、引用付きの要約回答を得られることを Day 1 のコア体験とする。将来的にはメタデータ抽出パイプライン、Contextual Retrieval、動画分析（Skill ④）等を同一プラットフォーム上に拡張する。

| 項目 | v3.2 仕様 |
|---|---|
| 想定ユーザー | 営業 16 名（社内 Slack ワークスペース） |
| Slack 連携方式 | Socket Mode（17 OAuth scopes 取得済み） |
| LLM | AWS Bedrock 経由 Claude Sonnet 4.6（メイン） / Claude Haiku 4.5（軽量タスク） |
| データ層 | Amazon RDS for PostgreSQL 16.14 + pgvector 0.8.2（東京リージョン稼働中） |
| Embedding | `multilingual-e5-large`（1024 次元、ローカル sentence-transformers） |
| Skill 実装言語 | Python 3.12（pydantic v2 + boto3 + psycopg） |
| Skill オーケストレータ | **OpenClaw v2026.5.20（Node.js 24 / TypeScript）** — B 案採用時 |
| ホスティング | EC2 t3.medium（4 GB, ap-northeast-1） + IAM Role 経由 Bedrock |
| Secrets | AWS Secrets Manager（Slack bot/app token、DB 認証情報） |

### 1.2 v3.1 からの主な変更点

| # | 変更点 | 影響箇所 |
|---|---|---|
| 1 | OpenClaw の位置づけを **「Agent Runtime + 内部 Agent SDK」** から **「Slack/チャネル層のオーケストレータ + Bedrock provider」** に変更 | アーキ全体図 / Skill 配置 |
| 2 | Skill 実装方針を **「SKILL.md + Agent SDK 内蔵」** から **「OpenClaw SKILL.md → HTTP → 既存 Python Skill（FastAPI）」** に変更（B 案） | `src/teamagent/skills/` 周辺 |
| 3 | デプロイ方針を **「OpenClaw を任意のホストで動かす」** から **「AWS 公式 CloudFormation テンプレート（`sample-OpenClaw-on-AWS-with-Bedrock`）で EC2 + IAM Role + Bedrock を一括構築」** に変更 | `infra/` |
| 4 | Bedrock 認証を **「個人 access key」** から **「EC2 IAM Role（IMDSv2）経由」** に変更（API キー不要） | セキュリティ全般 |
| 5 | **ClawHub（OpenClaw コミュニティ Skill レジストリ）を `openclaw.json` で完全無効化**（ClawHavoc サプライチェーンインシデント対策） | セキュリティポリシー |
| 6 | Anthropic 「Agent SDK Credits」（2026/5/13 発表、6/15 適用）の影響を明示的に **「無関係」と確定**（Bedrock 経由のため） | リスク分析 |
| 7 | 営業ターゲットを v3.1 記述の「20 名」表記から **実態に合わせて 16 名**に統一 | 規模試算全般 |
| 8 | OpenClaw ライセンス記述を Apache-2.0 → **MIT** に訂正（fork 可能性に影響なし） | ライセンス節 |

### 1.3 OpenClaw 採用方針（暫定）

v3.2 ドラフトは **B 案（HTTP 橋渡し）** を前提に書き下ろす。ただし採用そのものは **Sprint 2 末ゲート①（2026-06-07）** で最終確定する。ゲート①で B 案が採用されなかった場合、本書の §2〜§4 を D 案（OpenClaw 不採用、現状の `boto3 + slack-bolt + 自前 Skill Registry` を継続）に差し替えるリビジョンを発行する。それ以外の節（§3 技術スタックの Bedrock / pgvector / Skill 設計、§5 セキュリティの一部、§6 コスト試算）は B/D いずれでも有効である。

---

## 2. アーキテクチャ

### 2.1 全体構成（B 案 / HTTP 橋渡し）

```
                                  ap-northeast-1 (東京リージョン)
┌───────────────┐                ┌──────────────────────────────────────────────────────────┐
│   Slack       │                │                       AWS VPC                            │
│   Workspace   │                │                                                          │
│               │  Socket Mode   │  ┌──────────────────────┐    HTTP (localhost)            │
│ @TeamAgent_v3 │ ─────────────▶ │  │  OpenClaw Gateway    │  ─────────┐                    │
│  (mention)    │ ◀───────────── │  │  (Node 24 / TS)      │           │                    │
│               │                │  │  port :18789         │           ▼                    │
└───────────────┘                │  │  - openclaw.json     │   ┌──────────────────────┐    │
                                 │  │  - SKILL.md (search) │   │  FastAPI             │    │
                                 │  │  - Slack connector   │   │  teamagent-skills    │    │
                                 │  └──────────┬───────────┘   │  (Python 3.12)       │    │
                                 │             │               │  port :8000          │    │
                                 │             │ IAM Role      │  POST /skills/       │    │
                                 │             │ (IMDSv2)      │       {name}/invoke  │    │
                                 │             ▼               └──────────┬───────────┘    │
                                 │  ┌──────────────────────┐              │                │
                                 │  │  Amazon Bedrock      │              │                │
                                 │  │  Converse API        │ ◀────────────┤                │
                                 │  │  - Claude Sonnet 4.6 │              │                │
                                 │  │  - Claude Haiku 4.5  │              │                │
                                 │  └──────────────────────┘              │                │
                                 │                                        ▼                │
                                 │                              ┌────────────────────┐     │
                                 │                              │  RDS PostgreSQL 16 │     │
                                 │                              │  + pgvector 0.8.2  │     │
                                 │                              │  proposal_chunks   │     │
                                 │                              └────────────────────┘     │
                                 │                                                          │
                                 │                              ┌────────────────────┐     │
                                 │                              │  Secrets Manager   │     │
                                 │                              │  - Slack tokens    │     │
                                 │                              │  - DB credentials  │     │
                                 │                              └────────────────────┘     │
                                 └──────────────────────────────────────────────────────────┘
```

EC2 1 台に **OpenClaw Gateway（Node）** と **FastAPI Skill サーバ（Python）** を共置する。両者は localhost 経由で HTTP 通信し、外部ネットワークには出ない。Bedrock / RDS / Secrets Manager への呼び出しは EC2 にアタッチされた **IAM Role**（IMDSv2 経由で取得した一時クレデンシャル）で行う。

### 2.2 各層の責務

| 層 | コンポーネント | 責務 | 実装言語 |
|---|---|---|---|
| Channel | Slack Workspace | ユーザーからの mention / DM 受信、応答表示 | — |
| Gateway / Orchestrator | OpenClaw Gateway | Slack Socket Mode の張り付け、LLM の Tool 呼び出し制御、Bedrock provider 経由の Claude 呼び出し、SKILL.md 解釈 | TypeScript / Node.js |
| Skill API | FastAPI `teamagent-skills` | 既存 Python Skill（`src/teamagent/skills/`）を HTTP 公開、`POST /skills/{name}/invoke` を提供 | Python 3.12 |
| Business Logic | `teamagent.skills.search.SearchSkill` ほか | クエリ理解、embedding、pgvector 検索、Bedrock 要約、引用組み立て | Python 3.12 |
| Adapter | `teamagent.adapters.*` | Bedrock / pgvector / Slack / Embedder の薄いラッパー | Python 3.12 |
| Data | RDS pg16 + pgvector | チャンク本文 + JSONB メタデータ + HNSW index | — |
| LLM | Amazon Bedrock | Converse API（Claude Sonnet 4.6 / Haiku 4.5、prompt caching 対応） | — |
| Secret | AWS Secrets Manager | Slack bot/app token、DB 認証情報のローテーション管理 | — |

### 2.3 データフローとリクエスト ID 伝播

1. ユーザーが Slack で `@TeamAgent_v3 飲食業の提案実績を教えて` と mention する。
2. OpenClaw Gateway（Socket Mode）がイベントを受信する。Gateway は `X-Request-Id` ヘッダに UUID v4 を発行する。
3. Gateway は LLM（Bedrock Claude Sonnet 4.6）に**「`teamagent-search` SKILL を呼ぶか自然文で返すか」**を判断させる。検索が必要と判断された場合、SKILL.md の指示に従い `curl http://127.0.0.1:8000/skills/search/invoke` を実行する。
4. FastAPI は受信ヘッダから request_id を取り出し、`SkillContext(request_id=...)` を組み立て `SearchSkill.run()` を呼ぶ。
5. `SearchSkill` は以下を順に実行する（`src/teamagent/skills/search/skill.py` の現行実装をそのまま使う）。
   1. `LocalE5Embedder` でクエリを 1024 次元ベクトルへ変換
   2. `PgVectorClient.search_similar()` で類似 top_k チャンクを取得
   3. `BedrockClient.converse()` に system prompt + チャンク + クエリを渡して要約を得る
   4. 引用 source（`file_name (p.{page_num})` または `metadata.source`）を整形
6. FastAPI が `SearchOutput`（`answer`, `hits[]`, `total_cost_usd`）を JSON で返却する。
7. OpenClaw Gateway は応答テキストを Slack thread に投稿する。

全層で構造化ログに `request_id` を入れて流す（CLAUDE.md 6-bis 準拠）。Gateway 層のログは OpenClaw 標準の JSON ロガー、FastAPI 層は structlog で出力し、CloudWatch Logs で `request_id` を横断検索できるようにする。

---

## 3. 技術スタック

### 3.1 OpenClaw 関連

| 項目 | 値 | 出典 |
|---|---|---|
| OpenClaw 本体バージョン | **v2026.5.20**（2026-05-21 リリース） | `gh release list openclaw/openclaw` |
| 実装言語 | TypeScript（114 MB） + Python（0.08 MB、補助のみ） | `gh api repos/openclaw/openclaw` |
| ライセンス | **MIT** | `gh api`（v3.1 記載の Apache-2.0 は誤り） |
| Stars / Forks | 373,824 / 77,665（2026-05-22 時点） | `gh api` |
| 推奨 Node | **Node 24**（最低 Node 22.19+ LTS） | `docs.openclaw.ai/install` |
| デフォルトポート | 18789 | 同上 |
| 設定ファイル | `~/.openclaw/openclaw.json`（JSON5） | 同上 |
| Slack channel | `mode: "socket"`（TeamAgent 既存方式と一致） | `docs/channels/slack.md` |
| Bedrock provider | 公式サポート `amazon-bedrock` | `docs/providers/bedrock.md` |

### 3.2 AWS インフラ（CloudFormation テンプレート）

ベースは [`aws-samples/sample-OpenClaw-on-AWS-with-Bedrock`](https://github.com/aws-samples/sample-OpenClaw-on-AWS-with-Bedrock) の `clawdbot-bedrock.yaml`。対応リージョンは `us-east-1` / `us-west-2` / `eu-west-1` / **`ap-northeast-1`**。TeamAgent はすでに ap-northeast-1 で RDS を稼働させているため、同一 VPC 内に EC2 を配置する。

テンプレートがプロビジョンするもの：

- VPC + 公開サブネット（既存 VPC を import するよう変更）
- EC2 インスタンス（**t3.medium / 4 GB**）+ UserData で OpenClaw bootstrap
- **IAM Role**（`bedrock:InvokeModel` / `InvokeModelWithResponseStream` / `ListFoundationModels` / `ListInferenceProfiles` + Secrets Manager 読み取り）
- セキュリティグループ（Inbound: 不要、Outbound: Bedrock / RDS のみ）
- オプションで VPC エンドポイント（Bedrock / Secrets Manager / S3）

**API キー不要の認証フロー**：

```
EC2  ─(IAM Role)─▶  IMDSv2  ─(temporary credentials, TTL あり)─▶  Bedrock / RDS / Secrets Manager
                       ▲
                       └─ AWS_PROFILE=default で AWS SDK が IMDS を参照
```

⚠️ **`AWS_PROFILE` の罠**：OpenClaw v2026.4.5+ では gateway systemd service が shell env を継承しないため、`~/.openclaw/.env` に `AWS_PROFILE=default` を明示する必要がある。`sample-OpenClaw-on-AWS-with-Bedrock` の UserData は 2026/4 以降の新規デプロイで自動書き込みするが、本書では Sprint 2 W1 の PoC 手順に明記する。

### 3.3 LLM / Embedding

| 用途 | モデル | 推論プロファイル ID | 料金 (USD / 1M tokens) |
|---|---|---|---|
| メイン（回答生成） | Claude Sonnet 4.6 | `us.anthropic.claude-sonnet-4-6` | input 3.0 / output 15.0 |
| 軽量（Router、Contextual Retrieval 等） | Claude Haiku 4.5 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | input 1.0 / output 5.0 |
| Embedding | multilingual-e5-large（1024 次元） | ローカル sentence-transformers | $0 / 推論コストのみ |

モデル ID は **必ず推論プロファイル形式（`us.` プレフィックス付き）** を使う。`anthropic.claude-sonnet-4-6-20251022-v1:0` や `anthropic.claude-sonnet-4-6`（on-demand 非対応）は NG（Day 0 で確認済み）。料金表は `src/teamagent/adapters/bedrock_client.py` の `_PRICE_TABLE` に既に組み込み済み。

### 3.4 既存 Python 資産（B/D いずれでも継続利用）

- `pyproject.toml`：claude-agent-sdk, anthropic, boto3, psycopg, pgvector, sentence-transformers, pdfplumber, structlog, pydantic v2
- **FastAPI を追加**（B 案採用時）：`fastapi[standard]` + `uvicorn[standard]`
- `src/teamagent/`：
  - `adapters/bedrock_client.py`（Converse + usage/cost/latency ロギング）
  - `adapters/pgvector_client.py`（psycopg + ベクトル検索ヘルパー）
  - `adapters/slack_client.py`（slack_sdk AsyncWebClient ラッパー、D 案時のみ稼働）
  - `adapters/embeddings_client.py`（`LocalE5Embedder`）
  - `skills/base.py`（`BaseSkill` / `Registry` / `SkillContext`）
  - `skills/search/`（`schema.py` + `skill.py`）
  - `runtime/local.py`（CLI エントリポイント、開発用）
  - `runtime/slack_bot.py`（Bolt Socket Mode、D 案時のみ稼働 / B 案では引退）
  - `prompts/search/v1/system.md`（コード外 prompt、Git 管理）

### 3.5 Slack 設定（既得・継続利用）

- Slack App ID: `A0B51FGQ8JK`（`TeamAgent_Dev_Ver.2`）
- Socket Mode 有効化済み
- Event Subscriptions: `app_mention` + `message.im`
- OAuth スコープ **17 個**（取得済み）：`app_mentions:read` / `chat:write` / `chat:write.public` / `channels:history` / `channels:read` / `groups:history` / `groups:read` / `im:history` / `im:write` / `commands` / `reactions:read` / `users:read` / `users.profile:read` / `files:read` / `files:write` / `dnd:read` / `usergroups:read`
- これは OpenClaw 公式 `docs/channels/slack.md` が要求するスコープと完全一致するため、B 案へ移行しても Slack App の再申請は不要。

---

## 4. Skill 設計

### 4.1 設計原則（CLAUDE.md 6-bis を堅持）

OpenClaw 採否に関わらず以下のルールは v3.2 でも変更しない。

1. **3 層分離**：`skills/`（ビジネスロジック）/ `adapters/`（外部依存ラッパー）/ `runtime/`（エントリポイント）
2. **Pydantic v2 で I/O 固定**：Skill の入出力は必ず `BaseModel`、dict をそのまま返さない
3. **`mypy --strict` 必須**：CI で型エラーは fail
4. **構造化ログ + request_id 伝播**：全層で `{"request_id": "...", "skill": "...", "token_usage": {...}, "latency_ms": ..., "cost_usd": ...}`
5. **Prompt はファイル化**：`src/teamagent/prompts/<skill>/v1/system.md`
6. **Bedrock 呼び出しごとに usage / cost を必ずログ**
7. **テスト**：Skill 単位で pytest（adapter は moto / fake でモック）、最低 happy path + 1 エッジケース
8. **Slack 投稿 / DB 書き込みは Idempotency-Key 付き**

### 4.2 既存 `src/teamagent/skills/search/` の流用方針

現在 `SearchSkill` は以下のシグネチャを持つ（`skill.py:30-103` の実装）。これを **そのまま** FastAPI 経由で呼び出す。

```python
@register
class SearchSkill(BaseSkill[SearchInput, SearchOutput]):
    name: ClassVar[str] = "search"
    description: ClassVar[str] = "営業16名が過去の提案書・議事録・メールを自然文クエリで検索する"

    def __init__(
        self,
        bedrock: BedrockClient | None = None,
        pgvector: PgVectorClient | None = None,
        embedder: Embedder | None = None,
        target_table: str = "proposals_chunks",
        ...
    ) -> None: ...

    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput: ...
```

`run()` のフローは **embedding → pgvector 検索 → Bedrock 要約 → 引用整形** という現行ロジックを 100% 流用する。

### 4.3 SearchSkill フロー（B 案）

```
┌────────────────────────┐
│ OpenClaw Gateway       │
│ (TS)                   │
└──────────┬─────────────┘
           │ HTTP POST
           │ /skills/search/invoke
           │ headers: X-Request-Id, X-User-Id, X-Channel-Id
           │ body: { query, top_k, filter_industry? }
           ▼
┌────────────────────────────────────────────────┐
│ FastAPI: services/teamagent_skills_api/        │
│                                                │
│  @app.post("/skills/{name}/invoke")            │
│  def invoke(name, body, request: Request):     │
│      request_id = request.headers["X-Request-Id"]
│      ctx = SkillContext(request_id=request_id) │
│      skill = registry.get(name)                │
│      input_obj = skill.input_schema(**body)    │
│      output = skill.run(input_obj, ctx)        │
│      return output.model_dump()                │
└──────────┬─────────────────────────────────────┘
           │
           ▼
┌────────────────────────────────────────────────┐
│ SearchSkill.run()                              │
│  1. self._embed(query)                         │
│       → LocalE5Embedder (multilingual-e5-large)│
│       → 1024 次元 list[float]                  │
│  2. self._retrieve(embedding, input)           │
│       → PgVectorClient.search_similar()        │
│       → HNSW cosine, top_k=5（default）        │
│       → JSONB filter（industry, budget, ...）  │
│  3. self._summarize(query, hits, request_id)   │
│       → load_prompt("search", "v1", "system")  │
│       → BedrockClient.converse(                │
│            model=us.anthropic.claude-sonnet-4-6│
│            temperature=0.1                     │
│            cache_control=ephemeral             │
│         )                                      │
│  4. SearchHitOut[] へ整形（source 引用付き）   │
└────────────────────────────────────────────────┘
```

### 4.4 OpenClaw SKILL.md → FastAPI 呼び出しパターン

OpenClaw 側に登録する Skill は **薄いラッパー** に徹し、ロジックは Python 側に置く。`~/.openclaw/skills/teamagent-search/SKILL.md` は以下のような構造になる（参考イメージ）。

```yaml
---
name: teamagent-search
description: |
  TeamAgent の過去資料検索 Skill。営業 16 名が提案書・議事録・メールを
  自然文クエリで検索し、引用付きの要約回答を得る。
inputs:
  query: { type: string, required: true }
  top_k: { type: integer, default: 5 }
  filter_industry: { type: string, required: false }
---

# 使い方

ユーザーから過去資料に関する質問を受けたら、以下を実行する：

1. クエリ文字列を整形（不要な敬語、署名、引用記号を除去）
2. `curl -s -X POST http://127.0.0.1:8000/skills/search/invoke \
     -H "Content-Type: application/json" \
     -H "X-Request-Id: ${REQUEST_ID}" \
     -d '{"query": "...", "top_k": 5}'`
3. レスポンスの `answer` をユーザーに返し、`hits[].source` を引用として併記する
4. cost_usd が $0.05 を超えた場合は警告ログを出す
```

ロジックを Python 側に集約することで、SKILL.md は **薄いオーケストレーション層**にとどまり、サプライチェーンリスクの最小化（次節）にも寄与する。

---

## 5. セキュリティ

### 5.1 IAM Role による API キー不要設計

EC2 にアタッチした IAM Role を IMDSv2 経由で AWS SDK が参照することで、Bedrock / RDS / Secrets Manager への API キーをディスクやメモリ常駐の環境変数に置かない設計とする。これは v3.1 の「個人 access key を `aws configure` で配置」よりセキュリティが強化される。

**ポリシー（最小権限）**：

- `bedrock:InvokeModel`（特定の推論プロファイル ARN のみに制限可能）
- `bedrock:InvokeModelWithResponseStream`
- `bedrock:ListFoundationModels` / `bedrock:ListInferenceProfiles`
- `secretsmanager:GetSecretValue`（`teamagent/dev/*` 配下のみ）
- `rds-db:connect`（IAM database authentication 採用時）

メリット：

- API キーがどこにも保存されない（ローカル `.env` / Secrets Manager 経由でも持たない）
- プロンプトインジェクションで OpenClaw が攻撃されても credentials は漏洩しない（IMDSv2 + TTL 短い一時クレデンシャル）
- ローテーション自動

### 5.2 ClawHub 無効化

ClawHub（OpenClaw コミュニティの Skill 共有レジストリ）には **ClawHavoc インシデント**（341 件の悪意ある Skill が混入、9,000+ インストールが侵害、HKCERT / Straiker / CertiK が公式警告）の前科がある。v3.1 の「ホワイトリスト運用 + 週次 CVE 確認」では不十分と判断し、v3.2 では **完全無効化** とする。

`~/.openclaw/openclaw.json`（JSON5）の該当箇所：

```json5
{
  clawhub: {
    disabled: true,         // ClawHub からの一切のインストール / アップデートを禁止
  },
  skills: {
    sources: ["local"],     // ローカル ~/.openclaw/skills/ のみを Skill ソースとする
  },
}
```

社内独自 Skill は **GitHub PR レビュー + コードオーナー承認 + Skill 単位のサンドボックス制限**（ファイルアクセス・ネットワーク・実行時間）を必須化する。SKILL.md の `curl` 先は `127.0.0.1:8000` のみホワイトリストする。

### 5.3 Secrets Manager 経由の Slack token

Slack の `xoxb-` / `xapp-` トークンは **AWS Secrets Manager** にのみ保管し、EC2 起動時に IAM Role 経由で取得して `~/.openclaw/.env`（root:root, 0600）に書き込む。Git / CloudWatch Logs / Slack チャットに平文露出させない。

- `teamagent/dev/slack/bot_token`（xoxb-）
- `teamagent/dev/slack/app_token`（xapp-）

⚠️ Day 1 でチャットに露出した既存トークンは **Sprint 2 W1 開始時にローテーション**（Reinstall App → 新トークン取得 → Secrets Manager 更新）する（CLAUDE.md 2-ter に既に TODO として記載）。

### 5.4 PII マスキング・構造化ログ

CLAUDE.md 6-bis の Don't 規定をそのまま継承する。

- ❌ エラーログに生入力（提案 PDF 全文・顧客名・会話履歴）を入れない
- ✅ `request_id` だけログし、本体は `s3://teamagent-dev-debug-snapshots/<request_id>.json`（KMS 暗号化、TTL 30 日）に保管
- ✅ 構造化ログ（JSON）に `request_id`, `skill`, `event`, `token_usage`, `latency_ms`, `cost_usd` を必ず出す
- ✅ Slack 投稿 / DB 書き込みは Idempotency-Key 付きでリトライ二重投稿を防ぐ

OpenClaw Gateway 側の標準ログにも、検索クエリ本文・LLM 応答本文はマスクして出力する（OpenClaw の logger フックを利用する想定、Sprint 2 W1 PoC で検証）。

---

## 6. コスト試算

### 6.1 月次インフラ（B 案）

| 項目 | 月額（USD） | 備考 |
|---|---|---|
| EC2 t3.medium（東京、On-Demand） | 約 $30 | 24 時間稼働。Reserved Instance / Savings Plans で約 $20 まで圧縮可能 |
| EBS gp3 30 GB | 約 $3 | OS + OpenClaw + FastAPI + ログ |
| RDS db.t4g.micro（既存） | 約 $15 | v3.1 から継続、pg16.14 + pgvector 0.8.2 |
| Secrets Manager（5 シークレット） | 約 $2 | Slack tokens + DB credentials |
| CloudWatch Logs（5 GB/月想定） | 約 $3 | 構造化ログ + Skill 実行ログ |
| **インフラ小計** | **約 $53** | Bedrock 従量を除く |

### 6.2 Bedrock 従量（Day 2 実績ベース）

Day 2（2026-05-22 15:05）の Slack 実機 E2E テスト 3 連投で測定した実績：

| 指標 | 値 |
|---|---|
| 1 クエリあたりコスト | **$0.01 〜 $0.02** |
| 1 クエリあたりレイテンシ | 7 〜 11 秒 |
| Top retrieval similarity | 0.80 〜 0.84 |

これを 16 名 × 30 クエリ/日 × 22 営業日で外挿すると：

| シナリオ | 1 クエリ単価 | 月次 Bedrock コスト |
|---|---|---|
| 低位（$0.01 / クエリ） | $0.01 | 16 × 30 × 22 × $0.01 ≈ **$106** |
| 高位（$0.02 / クエリ） | $0.02 | 16 × 30 × 22 × $0.02 ≈ **$211** |
| ストレッチ（Contextual Retrieval + 動画分析追加） | $0.03 | 16 × 30 × 22 × $0.03 ≈ **$317** |

### 6.3 月次合計（B 案）

| シナリオ | インフラ | Bedrock | **合計** |
|---|---|---|---|
| 低位 | $53 | $106 | **約 $160** |
| 標準 | $53 | $200 | **約 $253** |
| ストレッチ | $53 | $317 | **約 $370** |

→ **おおむね $200 〜 $400 / 月** の範囲に収まる見込み。AWS Budgets には既に `TeamAgent-Bedrock-Monthly $50/月`（要 W1 で再設定、$250 程度へ）と `TeamAgent-Server-Monthly $267/月` が設定済み（CLAUDE.md 2-bis）。

### 6.4 コスト最適化レバー

1. **prompt caching**（CLAUDE.md §6 重要判断）：system prompt + 頻出 context で約 90% コスト削減見込み
2. **Haiku 4.5 への routing**：Query Router 系の軽量タスクを Haiku に寄せる（input $1 / output $5 で Sonnet の 1/3）
3. **Reserved Instance / Savings Plans**：EC2 を 1 年 RI に切り替えで約 $10/月削減
4. **Amazon Nova Lite 等での 90% コスト削減はオプション**として残す（v3.2 標準ではない）

---

## 7. 採否判断（B 案 vs D 案）

ゲート①（2026-06-07）での判断材料として、両案を並べて整理する。

### 7.1 B 案（OpenClaw 採用 / HTTP 橋渡し）

**メリット**：

- **マルチチャネル拡張**：将来 WhatsApp / Telegram / Discord / Microsoft Teams 等を OpenClaw 公式 connector で追加可能
- **AWS 公式テンプレートで構築最短**：`sample-OpenClaw-on-AWS-with-Bedrock` で EC2 + IAM Role + Bedrock を 1 クリック構築（ap-northeast-1 対応）
- **API キー不要設計**：IAM Role + IMDSv2 でセキュリティ強化
- **既存 Python 資産を 85% 流用**：`src/teamagent/` をそのまま FastAPI ラップで再利用、`runtime/slack_bot.py` のみ引退
- **子会社運用実績 120 ユーザー**：子会社エンジニアの知見を取り込める可能性
- **デフォルトモデルが Claude Sonnet 4.6**：TeamAgent と一致

**デメリット**：

- **2 コンテナ運用**：OpenClaw Gateway（Node）+ FastAPI（Python）の同時運用で複雑度が上がる
- **言語境界の保守コスト**：SKILL.md（TS/YAML）と FastAPI（Python）の二箇所で I/O スキーマを揃える必要
- **ClawHub サプライチェーンリスク**：完全無効化しても、誤って有効化されたら 341 件の悪意 Skill にさらされる
- **OpenClaw 本体のバージョン追随**：Node 24 + v2026.5.20 系のセキュリティ CVE を週次で追跡する運用負荷
- **`AWS_PROFILE` の罠**：v2026.4.5+ で gateway systemd の env 継承問題があり、対応漏れで起動失敗のリスク
- **PoC 工数 1.5 Sprint**：D 案より +1.5 Sprint 必要

### 7.2 D 案（OpenClaw 不採用 / 現状維持）

**メリット**：

- **Day 2 で既に End-to-End 疎通成功**：mention → 検索 → 引用付き回答が動作確認済み
- **1 コンテナ運用**：FastAPI なし、`runtime/slack_bot.py` 単体でデプロイ
- **ClawHavoc サプライチェーンリスクを 100% 回避**
- **テスト 24 件 + mypy --strict 通過済み**：このまま本番投入可能
- **PoC 工数 0 Sprint**：そのまま Sprint 2 W1 から機能追加に進める
- **Python 一言語で完結**：保守者の認知負荷が低い

**デメリット**：

- **マルチチャネル拡張は自前実装が必要**：Microsoft Teams 連携など将来追加するチャネルは TeamAgent 内に直書きする必要
- **OpenClaw コミュニティの Skill エコシステムは使えない**（ただし ClawHub リスクを考えると元々使わない方が望ましい）
- **AWS 公式テンプレートの恩恵を受けられない**：EC2 / IAM の構築は Terraform で自前管理（既に動いているので影響は限定的）

### 7.3 判断材料（Sprint 2 末ゲート①）

訂正ノート v0.3 §6 に従い、以下の 5 項目で最終判断する。

1. OpenClaw を **AWS 公式テンプレートで ap-northeast-1 に起動**できるか（Sprint 2 W1 PoC）
2. Slack mention → OpenClaw → Bedrock の Hello World が動くか
3. Python Skill を OpenClaw から **HTTP 経由で呼べるか**（FastAPI PoC）
4. **子会社運用ヒアリング**結果（`docs/v3.1/teamagent_subsidiary_questions_v2.md` 送付済み）
5. **社内セキュリティ部レビュー**（ClawHub 無効化 + IAM Role + Secrets Manager の構成）

→ 5 項目すべて green の場合は B 案採用、いずれかで red の場合は D 案転換。判断結果は本書を v3.2 確定版に昇格する際に「B 案確定」または「D 案転換」として §1.3 と §2 を書き換える。

---

## 8. 次のステップ（Sprint 2 W1 から）

### 8.1 Sprint 2 W1（2026-05-30 〜 2026-06-05）— PoC 段階

| # | タスク | 担当 | 出力 |
|---|---|---|---|
| 1 | Slack Bot/App Token ローテーション | Komata | Secrets Manager 更新、CloudWatch ログ確認 |
| 2 | `aws-samples/sample-OpenClaw-on-AWS-with-Bedrock` を fork → ap-northeast-1 で `clawdbot-bedrock.yaml` を apply | Komata | EC2 + IAM Role + SG が立つ |
| 3 | OpenClaw v2026.5.20 を EC2 で起動、`openclaw.json` に `clawhub.disabled: true` を設定 | Komata | `openclaw gateway --port 18789` が動く |
| 4 | Slack トークンを EC2 IAM Role 経由で Secrets Manager から取得 → `~/.openclaw/.env` に注入 | Komata | `AWS_PROFILE=default` を含む |
| 5 | Slack `@TeamAgent_v3 hello` → OpenClaw → Bedrock Claude → 応答 | Komata | スクショ + ログ |
| 6 | `services/teamagent_skills_api/` を新設、FastAPI で `POST /skills/{name}/invoke` を実装 | Komata | uvicorn 起動、pytest 追加 |
| 7 | `~/.openclaw/skills/teamagent-search/SKILL.md` を作成、FastAPI を curl 呼び出し | Komata | Slack mention → 引用付き回答 |

### 8.2 Sprint 2 W2（2026-06-06 〜 2026-06-12）— ゲート①

| # | タスク | 担当 | 出力 |
|---|---|---|---|
| 1 | 子会社運用ヒアリング結果のレビュー | Komata + 子会社 | レビューメモ |
| 2 | 社内セキュリティ部に ClawHub 無効化 + IAM Role 構成をレビュー依頼 | Komata + 情シス | 承認 or 是正リスト |
| 3 | **ゲート①判断会議**：B 案採用 / D 案転換 / 部分採用のいずれかを確定 | 全員 | 議事録 |
| 4 | 本書を v3.2 確定版に昇格（採否反映） | Komata | `teamagent_overview_v3.2.md`（draft 接尾辞除去） |

### 8.3 Sprint 3 以降（B 案採用の場合）

- **Sprint 3（6/13 〜 6/26）**：FastAPI Skill API の本実装、Pact 契約テスト追加、`runtime/slack_bot.py` を引退
- **Sprint 4 〜**：Contextual Retrieval（既存 chunk に Claude Haiku で前置詞付与 → 再 embedding）、メタデータ抽出パイプライン（Claude Sonnet で業界・予算・担当者を JSONB 抽出）、Query Router（meta / content / conditional / compare）、Skill ④ 動画分析（Gemini 2.5 Flash）
- **Sprint 10 末ゲート②**：本番ロールアウト判断

### 8.4 Sprint 3 以降（D 案転換の場合）

- 本書 §2 / §3 / §4 を D 案構成（boto3 + slack-bolt + 自前 Skill Registry）に差し替えてリビジョン発行
- 既存 `runtime/slack_bot.py` をそのまま機能拡張、`mention → SearchSkill` ディスパッチを軸に Contextual Retrieval / メタデータ抽出 / Query Router を追加
- Sprint 4〜10 のスケジュールは変更なし

---

## 9. 影響範囲・移行計画

### 9.1 既存資産の継続性

| 項目 | B 案影響 | D 案影響 |
|---|---|---|
| `src/teamagent/skills/` | 100% 継続（FastAPI でラップ） | 100% 継続 |
| `src/teamagent/adapters/` | 100% 継続 | 100% 継続 |
| `src/teamagent/runtime/slack_bot.py` | **引退**（OpenClaw が Socket Mode を持つ） | 100% 継続 |
| `src/teamagent/prompts/` | 100% 継続 | 100% 継続 |
| `infra/terraform/` | 一部追加（EC2 + IAM Role） | 変更なし |
| RDS / pgvector / Secrets Manager | 変更なし | 変更なし |
| pytest 24 件 + mypy --strict | 100% 継続（FastAPI 層に Pact 契約テスト追加） | 100% 継続 |
| Slack App（OAuth scopes） | **再申請不要**（OpenClaw 公式と一致） | 100% 継続 |

→ いずれの案でも **既存 Python コードは無駄にならない**。

### 9.2 ロールバック手順

B 案採用後にトラブルが発生した場合：

1. EC2 上の OpenClaw Gateway を停止（`systemctl --user stop openclaw-gateway.service`）
2. ローカル / EC2 で `python -m teamagent.runtime.slack_bot` を起動（D 案へフォールバック）
3. Slack App の Socket Mode 接続先を切り替え（OAuth スコープは共通のため再認可不要）

D 案で稼働継続できる構成を **Sprint 3 末まで残す**ことで、B 案の安定化判断までの保険とする。

---

## 10. 参照ドキュメント

| 種別 | パス / URL |
|---|---|
| 訂正ノート（必読） | [`docs/v3.1/teamagent_design_corrections_2026-05-22.md`](../v3.1/teamagent_design_corrections_2026-05-22.md) |
| 旧 overview（被置換） | [`docs/v3.1/teamagent_overview_v3.1.html`](../v3.1/teamagent_overview_v3.1.html) |
| プロジェクト引継ぎノート | [`CLAUDE.md`](../../CLAUDE.md) |
| 検索 Skill 設計 v1 | [`docs/v3.1/teamagent_search_skill_design_v1.md`](../v3.1/teamagent_search_skill_design_v1.md) |
| MVA 仕様 | [`docs/v3.1/teamagent_mva_spec_v1.1.html`](../v3.1/teamagent_mva_spec_v1.1.html) |
| 実装計画（14 Sprint） | [`docs/v3.1/teamagent_implementation_plan_v3.1.html`](../v3.1/teamagent_implementation_plan_v3.1.html) |
| 子会社向け質問リスト | [`docs/v3.1/teamagent_subsidiary_questions_v2.md`](../v3.1/teamagent_subsidiary_questions_v2.md) |
| OpenClaw 本体 | https://github.com/openclaw/openclaw |
| OpenClaw Bedrock provider | https://raw.githubusercontent.com/openclaw/openclaw/main/docs/providers/bedrock.md |
| OpenClaw Slack channel | https://raw.githubusercontent.com/openclaw/openclaw/main/docs/channels/slack.md |
| AWS 公式 CloudFormation | https://github.com/aws-samples/sample-OpenClaw-on-AWS-with-Bedrock |
| AWS Lightsail Blueprint | https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-quick-start-guide-openclaw.html |
| Issue #10149（SDK 採用却下） | https://github.com/openclaw/openclaw/issues/10149 |
| `AWS_PROFILE` の罠 Issue | https://github.com/openclaw/openclaw/issues/32290 |
| ClawHavoc 解説（HKCERT） | https://www.hkcert.org/blog/openclaw-s-rapid-adoption-exposes-skills-supply-chain-and-fake-installer-risks-in-a-high-privilege-ai-agent-platform |
| Agent SDK Credits 解説 | https://thenewstack.io/anthropic-agent-sdk-credits/ |

---

## 11. 更新履歴

| 日付 | バージョン | 内容 |
|---|---|---|
| 2026-05-22 | v3.2-draft.0 | 初版ドラフト（B 案前提で記述、ゲート①で確定予定） |

---

> 本ドラフトは **Sprint 2 W1 着手の判断材料**として作成された。実コードと PoC 結果に基づき、ゲート①（2026-06-07）で B 案 / D 案 / 部分採用のいずれかを確定し、本書を `teamagent_overview_v3.2.md`（draft 接尾辞除去）に昇格する。
