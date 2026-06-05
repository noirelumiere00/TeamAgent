# TeamAgent システム全体リファレンス（v3.2）

> ベクトル社・営業16名向け **Slack マルチスキル AI エージェント**の決定版リファレンス。
> Skill / 技術スタック / アーキテクチャ / インフラ / 運用 / 品質 / セキュリティ / コスト / 設計判断を一望する。
> 図解・データフローは [`architecture_and_flows.md`](architecture_and_flows.md) を併読。

最終更新: 2026-06-05 ／ Python 3.11+ ／ main: 全機能マージ済 ／ テスト 831 passed

---

## 1. Skill カタログ（9 個・`@register` で自動登録）

| # | Skill | 一言 | 主要技術 | トリガ例 |
|---|---|---|---|---|
| 1 | **search** | 営業16名が過去の提案書・議事録・メールを**自然文で横断検索** | pgvector(dense) + クライアント名ブースト + **Cohere Rerank** + Contextual Retrieval + min_relevance | `@TeamAgent ◯◯社の提案で響いた訴求は？` |
| 2 | **clientkarte** | 指定クライアントの**提案履歴・温度感・次アクションを時系列**で束ねる | pgvector timeline + Claude | `@TeamAgent ◯◯社のカルテ` |
| 3 | **proposal_draft** | **提案書ドラフト**を自動生成（検索基盤を再利用） | 検索 + Claude | `@TeamAgent ◯◯向けの提案たたき` |
| 4 | **proposal_review** | 提案書の**レビュー・改善提案** | Claude | `@TeamAgent この提案レビューして` |
| 5 | **video_algorithm (VSEO)** | 検索KWの**TikTok上位動画**を時刻付き構造分析→横断で勝ち筋→**HTMLレポート** | Scraper + yt-dlp + Gemini + ffmpeg + S3 | `@TeamAgent VSEO分析 新宿 ランチ` |
| 6 | **video_analysis** | YouTube/Shorts/TikTok/IG の**単体動画**を内容分析 | Gemini | `@TeamAgent この動画分析して <URL>` |
| 7 | **video_approval** | 編集者**納品動画をオリエンと照合**し一次FB（必須要素/NG/テロップ/尺）。Phase2 自動監視 | Gemini + Sheets/Drive | `@TeamAgent 動画チェック E01-01` |
| 8 | **tiktok_search** | TikTok をKW/タグで検索し**上位動画データ＋Gemini横断** | Scraper + Gemini | `@TeamAgent TikTokで◯◯検索` |
| 9 | **operation_log** | Slackスレッドの営業会話を**CRM活動ログ**(フェーズ/アクション/次ステップ/BANT)に構造化 | Claude | `@TeamAgent この会話をログ化` |

起動: **Slackメンション**→ **LLMルーター(Haiku)** が意図解釈→該当Skillへ振り分け（auto-route）。

---

## 2. 技術スタック / フレームワーク

### 言語 / 中核
- **Python 3.11+**、**Pydantic v2**（全I/Oスキーマ）、pydantic-settings、**structlog**（構造化ログ）、tenacity（リトライ）、rich、pyyaml
- **Node.js**（TikTokスクレイパ `tools/tiktok_scraper`：Puppeteer/Playwright）、**ffmpeg**（動画圧縮/フレーム抽出）

### LLM / AI
- **claude-agent-sdk** / **anthropic**（Claude）
- **boto3 → AWS Bedrock**: Claude Sonnet 4.6 / Haiku 4.5（テキスト）・**Cohere Rerank v3.5**
- **google-genai → GCP Vertex AI**: **Gemini 2.5 Flash**（動画マルチモーダル）
- **Embedding**: LocalE5Embedder（**multilingual-e5-large**, 1024次元・sentence-transformers）

### データ / RAG
- **psycopg[binary]** + **pgvector**（dense ベクトル検索＝現行の検索本体）、sqlalchemy、alembic（migration）
  - ※ pg_bigm(語彙BM25)+RRF ハイブリッドは設計のみで**現状未実装**（検索は dense + Cohere Rerank が本番経路）

### Slack
- **slack-bolt** / **slack-sdk** / **aiohttp**（Socket Mode・非同期）

### 動画 / スクレイピング
- **yt-dlp** + curl-cffi（TikTok/IG DL・ブラウザ偽装）、**playwright**（chromium・スクレイパ）、Puppeteer(Node)

### Google 連携
- google-api-python-client / google-auth(-oauthlib/-httplib2)（Drive / Sheets / Gmail・個人OAuth）

### ドキュメント / 配信
- pypdf, python-docx, python-pptx, openpyxl, markdown, beautifulsoup4, weasyprint（HTML→PDF）

### Web / 観測 / その他
- fastapi + uvicorn（将来のHTTP口）、httpx、**sentry-sdk**（エラー監視）、apscheduler

### 開発 / CI
- **ruff**（lint+format）、**mypy**（strict）、**pytest** + pytest-asyncio（831 tests）、**bandit**（SAST）、**gitleaks**（秘密スキャン）
- IaC: **Terraform**

---

## 3. アーキテクチャ（3 層厳密分離）

```
Runtime 層   slack_bot.py（Socket Mode / SkillDispatcher / LLMルーター）
             video_approval_poller.py（シート定期監視・Phase2・既定OFF）
   │ 呼ぶ
Skill 層     <skill>/{schema.py, skill.py}  ＋ prompts/<skill>/<ver>/*.md（@register 自動登録）
   │ 使う
Adapter 層   外部I/Oを隠蔽（全て差し替え可能＝テストはモック注入）
```

**Adapter（16）**: bedrock_client / embeddings_client / pgvector_client（検索系）｜ gemini_client / tiktok_scraper / video_download / video_proxy / rakko_scraper（動画系）｜ gsheets_client / gdrive_client / gmail_client / google_auth / drive_video（Google）｜ slack_client / slack_channel_ingest_client / report_publish（配信）

**設計原則**: プロンプトはコードでなくファイル（`load_prompt`）／I/Oは全てPydanticスキーマ／Adapterは callable でモック可／温度0.1（ハルシネーション抑制）。

---

## 4. インフラ

### AWS（acct 718959508629 / ap-northeast-1）
| リソース | 内容 |
|---|---|
| RDS PostgreSQL 16（db.t4g.micro） | **pgvector 0.8.2**（dense 検索）。ナレッジ正本。RLS で行レベル権限分離 |
| Bedrock | **Claude Sonnet 4.6 / Haiku 4.5 / Cohere Rerank v3.5**（テキスト=AWS課金） |
| EC2 worker（t4g.medium・arm64） | 常駐Bot + VSEO。`i-0feaa3c...`（**現在停止中**）。SSMのみ/IMDSv2/swap4GB |
| EC2 bastion（t4g.nano） | SSM踏み台（開発時のRDSトンネル） |
| S3 | `teamagent-dev-raw-files`（レポート/デプロイ）・`teamagent-tfstate-*`（IaC state） |
| Secrets Manager | `teamagent/dev/*`（DBパス/Slackトークン/Google OAuth/Vertex SA） |
| DynamoDB | `teamagent-tflock`（terraform lock） |

### GCP（project ntv-ai）
- **Vertex AI**: Gemini 2.5 Flash（動画分析=GCP課金 ⚠️ 請求先は会社アカウント化推奨）
- **Drive / Sheets / Gmail API**（個人OAuth・drive.readonly 等）

### Slack
- App `A0B51FGQ8JK`（TeamAgent Ver.2）/ **Socket Mode** / Workspace vectorinc

---

## 5. 検索（RAG）パイプライン
取込: Slack履歴 + Drive資料 → チャンク化 → **e5 埋め込み** → RDS(documents/chunks)。
検索: **ベクトル検索(pgvector cosine, top-30)** ＋ クライアント名ブースト → **Cohere Rerank v3.5（東京）** → top-5 → **min_relevance**（弱根拠は「記載なし」=反ハルシネーション）→ **Postgres RLS** で本人の閲覧可能行に限定 → Claude で回答（v2d 教示プロンプト）。
> ※ 語彙検索(pg_bigm BM25)+RRF ハイブリッドは設計のみ・**未実装**。現行本番は dense + Cohere Rerank。固有名詞リコールは将来ハイブリッドで強化予定。
本番フラグ: `USE_NEW_SCHEMA/USE_CONTEXTUAL/USE_COHERE_RERANK/USE_LLM_ROUTER/USE_AGGREGATION_MODE = true`、`PROMPT_VERSION=v2d`。

---

## 6. VSEO（video_algorithm）— 看板機能
KW → スクレイパ（over-fetch 目標+4本）→ 各動画[yt-dlp DL → ffmpeg圧縮 → Gemini時刻付き分析 → 寛容パース]（3並列）→ DL/分析失敗は後続候補でバックフィル → stdlib横断統計 → Gemini戦略シンセシス → **HTMLレポート**（Premiere風タイムライン/実動画再生）→ **S3署名付きURL** → Slack。
**失敗ゼロ化**: over-fetchバックフィル（DL失敗）＋ 寛容パース（enumズレを既定値救済）＋ yt-dlpリトライ。負荷テストで 10本完璧/20並行/失敗嵐/ドリフト嵐すべて検証済。

---

## 7. デプロイ / 運用
- **IaC**: `infra/terraform/worker.tf`（ドリフトのため **targeted apply 必須**）
- **デプロイ**: `scripts/deploy_to_ec2.sh --go`（tarball+env.base→S3→SSM展開: venv/pip + sentence-transformers + Playwright chromium(arm64) + swap + systemd）
- **秘密**: `scripts/load_secrets.sh` が Secrets Manager から起動時展開（Vertex SA も materialize）
- **2モード**: 開発=Mac（SSMトンネルでRDS）／本番=EC2（VPC直結）。差分は `infra/deploy/ec2.overrides.env`
- **二重起動禁止**: Slack Socket Mode は Mac/EC2 を同時接続させない（手順: `docs/v3.2/ec2_cutover_runbook.md`）

---

## 8. 品質 / CI / テスト
- CI（`.github/workflows/ci.yml`）: ruff(lint+format) → mypy(strict) → **pytest 831** → bandit → gitleaks
- ⚠️ 依存は `pip install --no-deps` ＋ **手動列挙**（新importは ci.yml に追加。pyproject の欠落に注意＝aiohttp/httpx の罠あり）
- テストは Adapter をモック注入し**外部I/O・課金ゼロ**で実行

---

## 9. セキュリティ / 設計原則
- **秘密の実値はコード/S3/チャットに出さない** → Secrets Manager のみ
- **シート書込は「削除ゼロ」**（単一セル更新・既存列の右端に追記のみ）
- **個人OAuth強制**（組織SAはDrive/Sheets不可）／Gemini(Vertex)はSA
- **反ハルシネーション**（min_relevance / 相関≠因果 / 確信度天井）
- **IMDSv2必須・SSMのみ・インバウンドゼロ**（EC2）

---

## 10. コスト
| 項目 | 概算 |
|---|---|
| EC2 worker t4g.medium | ≈$29/mo（停止中はEBSのみ≈$2.4） |
| RDS db.t4g.micro | 常時 |
| Bedrock（テキスト） | 従量・AWS課金 |
| Gemini（動画） | 従量・**GCP課金**（VSEO 1回 ≈$0.01〜0.03） |
| AWS Budgets | Bedrock $50/mo・Server $267/mo で通知 |

---

## 11. 主要な設計判断（ADR 要約）
1. **テキスト=Bedrock(Claude) / 動画=Gemini(Vertex)**。Nova A/BでテロップOCR 24 vs 0〜5 と判明し動画はGemini維持（`video_backend_eval_gemini_vs_nova.md`）
2. **検索=pgvector(dense) + Cohere Rerank**（gold set top-1 を 20%→52%→**64%** に改善し採用。pg_bigm+RRF ハイブリッドは将来）
3. **VSEO 失敗ゼロ化**（over-fetchバックフィル + 寛容パース）
4. **EC2 は arm64(t4g) + Playwright chromium**（Chrome-for-Testing に arm64 Linux 版が無いため）
5. **マルチSkill基盤に Claude Agent SDK on Bedrock を採用**（適応型オーケストレーション）

---

## 関連ドキュメント
- [architecture_and_flows.md](architecture_and_flows.md) — Mermaid 図解・データフロー
- [video_backend_eval_gemini_vs_nova.md](video_backend_eval_gemini_vs_nova.md) — 動画backend A/B
- [load_test_results.md](load_test_results.md) — 負荷/堅牢性テスト
- [ec2_cutover_runbook.md](ec2_cutover_runbook.md) — EC2切替手順
- [aws_compute_migration_ec2_vs_ecs.md](aws_compute_migration_ec2_vs_ecs.md) — Compute 移設評価
