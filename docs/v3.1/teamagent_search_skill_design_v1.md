# TeamAgent 検索 Skill 設計書 v1
## 〜営業 8 軸ヒアリング + 3 Agent 並列調査の統合〜

最終更新：2026 年 5 月 21 日
作成者：Shogo + TeamAgent 推進チーム

---

## 0. このドキュメントの位置づけ

Day 0 (2026/5/21) にローカル pgvector で実 PDF 3本を検索したところ、`「業界は？」` のような **meta-query では pure vector search が機能しない** ことが明らかになった。

そこで 3 つの Agent を並列で投げて、以下の3観点で調査を実施：
- **Agent A**：Query routing / rewriting（meta-query 対策）
- **Agent B**：pgvector + JSONB ハイブリッド検索アーキ
- **Agent C**：営業向け提案 DB RAG 設計パターン

3 Agent ともに結論が一致しており、本書はその統合版。MVA spec v1.1 Chapter 3「検索 Skill」の詳細実装指針として位置づける。

---

## 1. 営業ヒアリング結果（要件定義の起点）

営業から確認した「絞り込み軸」は以下8軸：

| # | 軸 | 性質 | 検索方式 |
|---|---|---|---|
| 1 | 業界 | カテゴリ | 構造化フィルタ |
| 2 | 自社サービス（何を提案したか） | カテゴリ | 構造化フィルタ |
| 3 | 予算感 | 数値レンジ | 構造化フィルタ |
| 4 | CL担当者 | テキスト | 構造化フィルタ |
| 5 | 部署 | カテゴリ | 構造化フィルタ |
| 6 | 商材 | テキスト | 構造化フィルタ |
| 7 | 文脈・マルチコンテキスト | 概念 | **セマンティック検索** |
| 8 | ターゲット・インサイト | 構造 + フリーテキスト | **ハイブリッド** |

→ **1-6 は構造化フィルタ、7-8 はベクトル検索、両方を AND で組み合わせる**のが正解。

---

## 2. 設計の全体像

```
┌─────────────────────────────────────────────────────────────┐
│                     【取り込みフェーズ】                       │
│                                                              │
│  提案 PDF (1ファイル)                                          │
│    ├─ pdfplumber でテキスト抽出                                │
│    ├─ Contextual Retrieval ヘッダ生成                         │
│    │   (Claude Haiku で「この章はxxx」を prepend)              │
│    ├─ チャンク化 (400-800 token, 50-100 token overlap)        │
│    ├─ multilingual-e5-large で 1024 次元 embedding            │
│    └─ Claude Sonnet で 8 軸メタデータ抽出 (JSON)               │
│                                                              │
│              ↓                                                │
│                                                              │
│      PostgreSQL + pgvector                                    │
│      ┌─────────────────────────────────────────────┐         │
│      │ proposal_chunks                              │         │
│      │  - id, proposal_id, page, chunk_idx          │         │
│      │  - content (前置詞付き)                       │         │
│      │  - embedding vector(1024)                   │         │
│      │  - metadata jsonb (8軸)                     │         │
│      │  - industry GENERATED (B-tree)              │         │
│      │  - budget_band GENERATED (B-tree)           │         │
│      │  - cl_department GENERATED (B-tree)         │         │
│      └─────────────────────────────────────────────┘         │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│                     【検索フェーズ】                            │
│                                                              │
│  営業の質問 (Slack 経由)                                       │
│    │                                                          │
│    ├─ ① Query Router (Claude Haiku) で意図分類                 │
│    │    ├─ meta → JSONB SQL 集計 (LLM 不要)                   │
│    │    ├─ content → vector search                            │
│    │    ├─ conditional → JSONB filter + vector search         │
│    │    └─ compare → 並列 2 クエリ + 比較表生成                 │
│    │                                                          │
│    ├─ ② Hybrid Search 実行                                    │
│    │    - pgvector (HNSW, iterative_scan)                    │
│    │    - tsvector (BM25 補完, 日本語 textsearch)              │
│    │    - RRF (Reciprocal Rank Fusion) で統合                 │
│    │                                                          │
│    ├─ ③ Cohere Rerank (Bedrock)                              │
│    │    top 30-50 → top 8-15 に絞る                          │
│    │                                                          │
│    └─ ④ Claude Sonnet で引用付き回答生成                       │
│         - system prompt は prompt caching (1h TTL)            │
│         - 引用: [提案ID:ページ] 形式                            │
│         - 「該当なし」を許可                                    │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. メタデータスキーマ（JSONB 設計）

```json
{
  "schema_version": "1.0",
  "industry": {
    "primary": "食品",
    "secondary": ["飲料", "健康食品"],
    "code": "JSIC-J"
  },
  "budget": {
    "amount_jpy": 5000000,
    "band": "M",
    "range_min_jpy": 3000000,
    "range_max_jpy": 10000000
  },
  "product": ["広告運用", "CRM導入支援"],
  "cl_contact": {
    "name": "山田太郎",
    "department": "マーケティング本部",
    "role": "部長"
  },
  "own_service": ["広告運用代行", "クリエイティブ制作"],
  "target": {
    "demographic": "20-40代女性",
    "geo": "首都圏",
    "persona_free_text": "都市部の働く女性"
  },
  "insight": "競合A社からのリプレース提案",
  "_meta": {
    "extracted_at": "2026-05-21T10:00:00Z",
    "extractor": "claude-sonnet-4.6",
    "reviewed": false,
    "field_confidence": {
      "industry.primary": 0.95,
      "budget.amount_jpy": 0.70
    }
  }
}
```

### 設計原則
- 業界は `{primary, secondary[]}` で複数業界対応
- 予算は数値 + バンド両方持つ（数値はレンジクエリ、バンドは UI 表示）
- ターゲットは構造化 + フリーテキスト両持ち
- `_meta` でレビュー状態・抽出元モデルをトラッキング
- スキーマ変更は破壊変更しない（新キーのみ追加）

---

## 4. テーブル DDL（実装用）

```sql
CREATE TABLE proposal_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    page_num INT NOT NULL,
    chunk_idx INT NOT NULL,
    content TEXT NOT NULL,              -- Contextual Retrieval 前置詞付き
    raw_content TEXT,                   -- 元のテキスト (参照用)
    embedding vector(1024) NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('japanese', content)) STORED,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- 頻出3軸を生成列で B-tree 化 (GIN より 3-5x 速い)
    industry TEXT GENERATED ALWAYS AS (metadata->'industry'->>'primary') STORED,
    budget_band TEXT GENERATED ALWAYS AS (metadata->'budget'->>'band') STORED,
    cl_department TEXT GENERATED ALWAYS AS (metadata->'cl_contact'->>'department') STORED,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

-- HNSW (e5 は cosine 推奨)
CREATE INDEX idx_proposal_embedding ON proposal_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- BM25 用 (日本語 textsearch)
CREATE INDEX idx_proposal_tsv ON proposal_chunks USING GIN (tsv);

-- JSONB 柔軟検索用
CREATE INDEX idx_proposal_metadata ON proposal_chunks
    USING gin (metadata jsonb_path_ops);

-- 頻出3軸の高速 B-tree
CREATE INDEX idx_proposal_industry ON proposal_chunks (industry) WHERE deleted_at IS NULL;
CREATE INDEX idx_proposal_budget ON proposal_chunks (budget_band) WHERE deleted_at IS NULL;
CREATE INDEX idx_proposal_dept ON proposal_chunks (cl_department) WHERE deleted_at IS NULL;
```

---

## 5. クエリパターン例

### 5.1 meta-query：「業界は？」

```sql
-- LLM 不要、SQL 集計のみ
SELECT
  industry,
  COUNT(DISTINCT proposal_id) AS proposal_count
FROM proposal_chunks
WHERE deleted_at IS NULL
GROUP BY industry
ORDER BY proposal_count DESC;
```

### 5.2 conditional content-query：「予算1000万規模の食品提案で KPI どう設計してた？」

```sql
SET hnsw.iterative_scan = 'relaxed_order';

SELECT
  proposal_id, file_name, page_num, content, metadata,
  1 - (embedding <=> %(qvec)s::vector) AS similarity
FROM proposal_chunks
WHERE
  deleted_at IS NULL
  AND industry = '食品'
  AND budget_band IN ('M', 'L')
  AND (metadata->'budget'->>'amount_jpy')::int BETWEEN 8000000 AND 12000000
ORDER BY embedding <=> %(qvec)s::vector
LIMIT 15;
```

### 5.3 Hybrid (RRF)：「マルチコンテキスト戦略の事例」

```sql
WITH vector_hits AS (
  SELECT id, RANK() OVER (ORDER BY embedding <=> %(qvec)s) AS r
  FROM proposal_chunks WHERE deleted_at IS NULL
  ORDER BY embedding <=> %(qvec)s LIMIT 50
),
sparse AS (
  SELECT id, RANK() OVER (ORDER BY ts_rank(tsv, plainto_tsquery('japanese', %(qtext)s)) DESC) AS r
  FROM proposal_chunks
  WHERE tsv @@ plainto_tsquery('japanese', %(qtext)s)
    AND deleted_at IS NULL
  LIMIT 50
)
SELECT pc.*, SUM(1.0/(60+u.r)) AS rrf
FROM (SELECT * FROM vector_hits UNION ALL SELECT * FROM sparse) u
JOIN proposal_chunks pc ON pc.id = u.id
GROUP BY pc.id ORDER BY rrf DESC LIMIT 30;
```

---

## 6. プロンプトテンプレ（RAG 回答生成）

### 6.1 System prompt（prompt caching 対象、1h TTL）

```
あなたは TeamAgent の営業ナレッジアシスタントです。
社内営業16名が過去の提案PDFを横断検索し、新規案件の提案作成を加速するために使用します。

# 厳守ルール
1. 提供された <context> 内の情報のみを根拠に回答してください。
2. すべての具体的記述には必ず [提案ID:ページ] 形式で引用を付けてください。
   例: 食品業界では3層KPI設計が好まれます [202309_食品A社:p12]
3. context に情報がない場合は推測せず「該当情報は検索結果にありません」と返答。
4. 営業が即座に流用できるよう、回答は以下の構造で:
   - 結論 (1〜2行)
   - 詳細 (引用付き箇条書き)
   - 関連提案リスト (ID・業界・年月)
5. 業界・予算規模・年月のメタ情報が context にあれば必ず明示。
6. 推測・脚色禁止。「おそらく」「一般的に」は使わない。

# 回答スタイル
- 営業現場で使うため簡潔・実務的に
- マルチコンテキスト戦略など専門用語は context 内の表現をそのまま使う
- 比較質問では Markdown 表で整理
```

### 6.2 User prompt

```
<context>
[提案ID: 202309_食品A社_提案v2 | 業界: 食品 | 予算: 2,400万円 | 提出: 2023-09]
[ページ: 12]
3層KPI設計を採用し、認知層・関与層・購買層でそれぞれ...
---
[提案ID: 202402_飲料B社_提案v1 | 業界: 食品/飲料 | 予算: 800万円 | 提出: 2024-02]
[ページ: 8]
予算制約下では2層に簡略化し...
---
(以降 top-N 件)
</context>

# 営業からの質問
{user_query}

# クエリ類型
{query_type}  # meta / example / concept / conditional / compare / ambiguous
```

---

## 7. 評価指標

### 7.1 Ragas 自動評価（月次バッチ）

| 指標 | 目標値 |
|---|---|
| Faithfulness（回答が context に基づく） | 0.90 以上 |
| Answer Relevancy | 0.85 以上 |
| Context Precision | 0.80 以上 |
| Context Recall | 0.85 以上 |

### 7.2 実用指標（16人運用）

- Slack 内 thumbs up / down 率
- 引用元 PDF を実際に開いたクリック率
- 回答からの提案コピペ率
- 同質問の再問い合わせ率（低いほど良い）

---

## 8. 実装ロードマップ（Sprint 単位）

### Sprint 1（Week 1-2）— Day 0 + Bedrock 接続
- [x] ローカル pgvector + multilingual-e5-large 動作確認 (2026/5/21 完了)
- [ ] AWS Bedrock モデル有効化 (Claude Sonnet 4.6 + Haiku 4.5 + Titan Embed v2)
- [ ] Terraform apply (RDS pg16 + IAM + S3)
- [ ] pyproject.toml に boto3 / anthropic 追加

### Sprint 2（Week 3-4）— Contextual Retrieval + メタデータ抽出
- [ ] 既存 PDF の Contextual Retrieval ヘッダ生成 (Haiku で一括)
- [ ] Claude Sonnet で 8 軸メタデータ自動抽出パイプライン
- [ ] 初期 100 件は人手レビュー (プロンプト改善ループ)
- [ ] JSONB スキーマ確定 + 生成列インデックス

### Sprint 3（Week 5-6）— Query Router + Hybrid Search
- [ ] Query Router (ルールベース版)
- [ ] Hybrid Search (vector + BM25 RRF)
- [ ] Cohere Rerank 接続 (Bedrock)
- [ ] Go/No-Go gate ①（営業3人 dogfood）

### Sprint 4（Week 7-8）— RAG 回答生成
- [ ] Bedrock Converse + prompt caching
- [ ] 引用付き回答生成 (system prompt 確定)
- [ ] クエリ類型ごとの回答テンプレ実装
- [ ] Slack スラッシュコマンド `/teamagent {query}`

### Sprint 5（Week 9-10）— 評価 + 運用
- [ ] Ragas golden test set (50問) 作成 + CI 化
- [ ] thumbs up/down 収集 + 週次ダッシュボード
- [ ] ハルシネーション検出フィルタ (引用なし主張の警告)

### Sprint 6+（Week 11+）— 拡張
- [ ] 部署別 RLS
- [ ] faithfulness < 0.8 ケースの自動アラート
- [ ] スキーマ拡張 (8軸 → 12軸)

---

## 9. コスト試算（16人運用・月額）

| 項目 | 月額 |
|---|---|
| Bedrock Claude Sonnet 4.6 (検索回答, 1日5問×22日, prompt caching 込み) | ¥15,000-20,000 |
| Bedrock Claude Haiku 4.5 (Query Routing) | ¥1,000-2,000 |
| Bedrock Titan Embed v2 (新規追加 PDF の embedding) | ¥500-1,000 |
| Cohere Rerank | ¥1,500-2,000 |
| RDS (db.t4g.medium) | ¥5,000-8,000 |
| S3 + Logs | ¥1,000 |
| **合計** | **¥24,000-34,000/月** |

→ 「月 ¥100K まで OK」の予算内で十分賄える。

---

## 10. 落とし穴・注意事項

1. **pgvector 0.8.0 以上**を必ず使う（古いとフィルタで結果ゼロのバグ）
2. **e5 モデルは cosine distance**（内積はダメ）
3. **temperature=0.1 + 引用必須化**でハルシネーション抑制
4. **JSONB `?` 演算子は jsonb_ops、`@>` は jsonb_path_ops** が高速 → 用途分離
5. **指数バックオフ + jitter** は Bedrock throttling 対策で必須
6. **prompt caching** の TTL は 1h（5分版もあるが Sonnet 4.5/4.6 で 1h 対応）
7. **「該当なし」を許可**することがハルシネーション抑制の鍵

---

## 11. 参考リンク（3 Agent から）

### Anthropic 公式
- [Contextual Retrieval | Anthropic](https://www.anthropic.com/news/contextual-retrieval)
- [Prompt caching | Amazon Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html)
- [Effectively use prompt caching on Amazon Bedrock | AWS](https://aws.amazon.com/blogs/machine-learning/effectively-use-prompt-caching-on-amazon-bedrock/)

### pgvector
- [pgvector 0.8.0 Released](https://www.postgresql.org/about/news/pgvector-080-released-2952/)
- [pgvector + Aurora PostgreSQL on AWS](https://aws.amazon.com/blogs/database/supercharging-vector-search-performance-and-relevance-with-pgvector-0-8-0-on-amazon-aurora-postgresql/)

### Query Routing / HyDE
- [Data Organization and Query Routing for RAG Systems](https://jxnl.co/writing/2025/09/11/data-organization-and-query-routing-for-rag-systems/)
- [Better RAG with HyDE | Zilliz Learn](https://zilliz.com/learn/improve-rag-and-information-retrieval-with-hyde-hypothetical-document-embeddings)

### 評価
- [RAG Evaluation Metrics 2026](https://blog.koiro.me/en/2026/04/30/rag-evaluation-metrics-2026/)
- [Evaluation of RAG with Ragas | Langfuse](https://langfuse.com/guides/cookbook/evaluation_of_rag_with_ragas)

### 実装例
- [aws-samples/rag-with-amazon-bedrock-and-pgvector](https://github.com/aws-samples/rag-with-amazon-bedrock-and-pgvector)
- [Building a RAG System With Claude, PostgreSQL & Python on AWS | Tiger Data](https://www.tigerdata.com/blog/building-a-rag-system-with-claude-postgresql-python-on-aws)
