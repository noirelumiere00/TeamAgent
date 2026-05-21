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

### 🔴 P0: AWS Bedrock 接続（必須）
- [ ] AWS コンソールで Bedrock モデル有効化
  - Claude Sonnet 4.6 (`anthropic.claude-sonnet-4-6-20251022-v1:0`)
  - Claude Haiku 4.5 (`anthropic.claude-haiku-4-5-20251022-v1:0`)
  - Titan Embed v2（または既存の multilingual-e5-large 継続）
- [ ] IAM ユーザに bedrock:InvokeModel 権限付与
- [ ] `pip install boto3 anthropic`
- [ ] hello world 呼び出し確認（python から claude-sonnet-4.6 で「こんにちは」）

### 🔴 P0: Terraform apply（AWS インフラ provisioning）
- [ ] `cd infra/terraform`
- [ ] `cp terraform.tfvars.example terraform.tfvars` → 編集
- [ ] `terraform init && terraform plan && terraform apply`
- [ ] RDS PG 16 + pgvector + Secrets Manager + S3 + IAM Role が立つ
- [ ] RDS 接続確認

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

## 7. 設計の参照優先順

1. **`docs/v3.1/teamagent_search_skill_design_v1.md`** ← 検索 Skill 実装はこれを読む（最重要）
2. `docs/v3.1/teamagent_mva_spec_v1.1.html` ← MVA 全体仕様
3. `docs/v3.1/teamagent_overview_v3.1.html` ← プロジェクト概要
4. `docs/v3.1/teamagent_implementation_plan_v3.1.html` ← 14 Sprint スケジュール

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
