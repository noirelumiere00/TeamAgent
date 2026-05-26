# Sprint 4 PDF 大量取り込み運用設計書 v1.0

**作成**: 2026-05-26 (Day 6, 着手 Sprint 3 後半 → Sprint 4 準備)
**対象**: 営業 16 名向け提案 PDF 200〜500 件を Google Drive から本番 RDS に一括取り込みする運用設計
**前提**:
- Day 6 で PR #50 (Google OAuth) + PR #51 (Drive folder ingest 本実装) 完成
- documents/chunks スキーマ + RLS + ACL 配線済
- LocalE5 (multilingual-e5-large, 1024 次元) で動作中

---

## 1. 並列化方針 — 決定木 + 推奨

```
Q1. 1 batch あたり何 chunks か？
 ├─ < 5,000 chunks → 順次でOK
 └─ ≥ 5,000 chunks → Q2 へ

Q2. Embedding backend は？
 ├─ LocalE5 継続  → Hybrid (DL 並列 / Embed 単一プロセス内 batch)
 └─ Bedrock Titan Embed v2 切替 → Full async, concurrency=8
```

**推奨**: **Bedrock Titan Embed v2 + asyncio + concurrency=8**
理由:
- LocalE5 (2GB) は Lambda 不可 → 自動化の妨げ
- LocalE5 CPU は 30〜50 chunks/sec → 30k chunks で 10〜15 分計算占有
- Titan Embed v2 は $0.10/1M tokens、500 PDF 全件で **$1.20** （超安い）
- 1024 次元維持 (`multilingual-e5-large` と同次元、移行コスト軽い)
- 並列化容易（API 並列）

**LangChain/LlamaIndex のデファクト**: `IngestionPipeline(num_workers=N)` は multiprocessing.Pool で 3〜15 倍速化。ただし sentence-transformers + CUDA は fork で死ぬので、CPU 時のみ採用可能。

---

## 2. Drive API quota 設計

公式: **325,000 quota units / 分 / user / project** ([Drive API limits](https://developers.google.com/workspace/drive/api/guides/limits))

| 操作 | unit | 500 PDF 試算 |
|---|---|---|
| `files.list` | 1 | 1 |
| `permissions.list` | 1 | 500 |
| `files.get` (download) | 1 | 500 |
| `changes.list` (差分時) | 1 | 10〜50 / 日 |
| **小計 / 一括取り込み** | — | **〜1,000 units** = quota の 0.3% |

→ **quota 引き上げ申請不要**。Workspace Admin 作業をスキップできる。

### リトライ実装 (tenacity デファクト)

```python
from tenacity import retry, stop_after_attempt, wait_random_exponential
from googleapiclient.errors import HttpError

@retry(
    retry=retry_if_exception_type(HttpError),
    wait=wait_random_exponential(multiplier=1, max=64),
    stop=stop_after_attempt(7),
    reraise=True,
)
def download_pdf(file_id: str) -> bytes: ...
```

Google 公式の推奨式 `min(2^n + random_ms, max_backoff)` と完全一致。

---

## 3. 失敗リトライ State Machine

新規 migration `0005_ingest_jobs.sql` で `ingest_jobs` テーブルを追加し、document 単位の state を永続化。

### State 遷移図

```
                  ┌──────────────────────────────────────────────┐
                  │                                              │
                  ▼                                              │
            ┌──────────┐  list_files OK    ┌────────────┐        │
   start ──▶│ SCANNED  │──────────────────▶│ DOWNLOADED │        │
            └──────────┘                   └─────┬──────┘        │
                  │                              │ pypdf OK      │
                  │ Drive 403/429 持続           ▼               │
                  │                        ┌────────────┐        │
                  ▼                        │ EXTRACTED  │        │
            ┌──────────┐                   └─────┬──────┘        │
            │ FAILED_  │                         │ embed OK      │
            │ TRANSIENT│◀── any step             ▼               │
            └──────────┘                   ┌────────────┐        │
                  │ N回失敗                │  EMBEDDED  │        │
                  ▼                        └─────┬──────┘        │
            ┌──────────┐                         │ COPY OK       │
            │ POISON   │                         ▼               │
            └──────────┘                   ┌────────────┐        │
            (人手で確認)                   │ COMMITTED  │────────┘
                                           └────────────┘
```

### 冪等性ガード

1. `documents(external_id UNIQUE)` で document 重複は弾く（migration 0001 で済）
2. chunk 途中失敗の再実行時は冒頭で `DELETE FROM chunks WHERE document_id = ?` してから全 chunk 再生成（500 件規模なら全削除→再生成が単純で安全、checkpoint 復元は不要）
3. transaction: `BEGIN; INSERT documents; COPY chunks; UPDATE state='COMMITTED'; COMMIT;` を 1 PDF 単位で
4. state file 置き場所: **RDS の `ingest_jobs` 一択**（S3 / DynamoDB は overkill）

---

## 4. EventBridge 定期実行構成（S4-03）

### 4.1 Lambda Container は LocalE5 では実質不可

| 観点 | Lambda Container 10GB | EC2 bastion + cron | Step Functions + Fargate |
|---|---|---|---|
| sentence-transformers (E5-large 2GB) | コールドスタート 30〜60s | 0s (常駐) | Fargate Spawn 30s |
| 実行時間上限 | 15 分 | 無制限 | 無制限 |
| 月次コスト (日次 10 分) | $0.5 | **$0** (bastion 既存共用) | $3〜5 |
| pgvector 接続 | VPC Lambda + ENI、遅い | 同 VPC 即時 | VPC Fargate OK |
| 冪等性 | timeout でロスト懸念 | 確実 | 確実 |
| **推奨** | Bedrock Titan 移行後は ◎ | **現状最適** | 将来 1000+ PDF/日になったら |

### 4.2 推奨構成図

```
[EventBridge Schedule]    cron(0 23 * * ? *)  ← 08:00 JST = 23:00 UTC
        │
        ▼
[SSM RunCommand → bastion EC2 i-04fd1f367b454f641]
        │
        ▼
  systemd-run /opt/teamagent/bin/ingest_drive_changes.py
        │
        ├──▶ Drive changes.list (pageToken stored in S3)
        ├──▶ for each changed file:
        │       download → extract → embed → COPY
        └──▶ Slack notify (#teamagent-ops) on completion
        │
        ▼
   RDS PostgreSQL (pgvector 0.8.2)
        │
        ▼
[CloudWatch Logs + structlog → metrics filter]
```

差分 token: `s3://teamagent-state/drive_page_token.json` （次回差分のため）。日次 10〜50 PDF なら 2〜3 分で完了。

### 4.3 Terraform 雛形 (Sprint 4 で実装)

```hcl
# infra/eventbridge_ingest.tf （雛形）
resource "aws_cloudwatch_event_rule" "daily_drive_ingest" {
  name                = "teamagent-daily-drive-ingest"
  description         = "Daily 08:00 JST Drive ingest sync"
  schedule_expression = "cron(0 23 * * ? *)"
}

resource "aws_cloudwatch_event_target" "ssm_run_command" {
  rule      = aws_cloudwatch_event_rule.daily_drive_ingest.name
  target_id = "BastionIngest"
  arn       = "arn:aws:ssm:ap-northeast-1::document/AWS-RunShellScript"
  role_arn  = aws_iam_role.eventbridge_ssm.arn
  run_command_targets {
    key    = "InstanceIds"
    values = [aws_instance.bastion.id]
  }
  input = jsonencode({
    commands = ["sudo -u teamagent /opt/teamagent/bin/ingest_drive_changes.py"]
  })
}
```

---

## 5. コスト試算表

500 PDF × 60 chunks = 30,000 chunks, 1 chunk ≈ 400 tokens 換算。

| 項目 | LocalE5 (現状) | Bedrock Titan Embed v2 | Bedrock Embed v3 (Cohere) |
|---|---|---|---|
| 単価 | $0 | $0.10 / 1M tokens (batch $0.04) | $0.10 / 1M tokens |
| **初回 500 PDF** | $0 + 25〜30 分計算 | **$1.20** | $1.20 |
| 月次差分 (10% = 50 PDF) | $0 + 3 分 | **$0.12** | $0.12 |
| Lambda で動くか | × | ◎ | ◎ |
| ベクトル次元 | 1024 | 1024 | 1024 |
| JP 検索精度 | E5-large は MTEB JP で強い | v2 は JP 評価あり | v3 は多言語強化 |
| **推奨** | Sprint 4 終了時に廃止検討 | **採用** | A/B 評価のみ |

### Contextual Retrieval ON/OFF 判定

- Anthropic 公式: $1.02 / 1M document tokens（prompt cache ON）
- 500 PDF × 30 ページ × 500 tokens ≈ 7.5M tokens → **約 $7.65 / 一括取り込み**
- 効果: 検索失敗率 **5.7% → 3.7% (-35%)**、+BM25 で 2.9% (-49%)、+rerank で 1.9% (-67%)
- **判定**: $8 で 49% 改善は十分元が取れる → **ON 推奨**。ただし最初は 50 PDF で A/B、top-1 hit rate を見て full rollout。

---

## 6. 検索精度モニタリング指標

CloudWatch Embedded Metric Format (EMF) 経由で structlog から流す。Grafana なしでも CW Dashboard で可視化可能。

| # | 指標 | 計測方法 | Alert 閾値 |
|---|---|---|---|
| 1 | top-1 cosine score 中央値 | クエリごとに最高 score を EMF 出力 | < 0.75 で warn |
| 2 | top-5 と top-1 のスコア差 | 同上、差分 | < 0.02 (= フラット, 弱い) で warn |
| 3 | filter_industry hit 率 | フィルタ付クエリで 0 件のとき counter +1 | 7日で 20% 超で見直し |
| 4 | embedding 重複率 | 週次バッチで `<->` 計算、近接 1.0 ペア数 | > 5% で chunk size 見直し |
| 5 | Slack 「再質問」率 | slack_bot.py のメッセージ分類 | > 15% で精度劣化サイン |

実装: structlog の binder → CloudWatch Logs Insights → 集計 → CW Dashboard。

---

## 7. 落とし穴 5 個

1. **pypdf が日本語 PDF の縦書き / 旧 JIS フォントで文字化け** → サイレントに「資料は存在するが検索 hit しない」状態。対策: extract 直後に `len(text.strip()) < 100` で警告 + PyMuPDF (`fitz`) フォールバック検討。

2. **HNSW index の更新コスト**: 30,000 chunks 一括 INSERT で HNSW が裏で再構築 → 検索 latency 2〜10 倍に跳ねる時間帯。対策: 一括取り込み中は `SET LOCAL hnsw.ef_construction = 64` に下げる、または index DROP → INSERT → CREATE INDEX。

3. **Drive Service Account の Subject 委任ミス**: 共有ドライブ配下のファイルが見えない / 個人ドライブだけ落ちる。対策: `supportsAllDrives=True, includeItemsFromAllDrives=True` 必須（既に PR #51 で対応済）。テストで「想定 PDF 数より少ない」が出たらまずここを疑う。

4. **Contextual Retrieval の prompt cache 失効** (5分 TTL): 並列化で「同じ文書チャンクをワーカー間で分割」すると cache がワーカーごとに別々で、コストが**N倍**に。対策: 1 文書 = 1 ワーカーに固定して逐次処理（cache hit 100%）。

5. **Bedrock の `ThrottlingException` は最初の数百リクエストで突然**（Account warm-up）。対策: 初回 batch は concurrency=2 → 数分後に 8 に上げる ramp-up。tenacity の `wait_random_exponential(max=30)` + `retry_if_exception_type(ClientError)` で吸収。

---

## 8. 即実行 Next Action（Sprint 4 着手チェックリスト）

| # | アクション | 担当 | 工数目安 |
|---|---|---|---|
| 1 | `infra/migrations/0005_ingest_jobs.sql` で state machine 永続化 | 🤖 | 1h |
| 2 | Bedrock Titan Embed v2 切替 PoC（LocalE5 と同一クエリで cosine 差分、50 PDF A/B） | 🤖 | 4h |
| 3 | `tenacity` 依存追加 + `gdrive_client.py` リトライ装飾 | 🤖 | 1h |
| 4 | `scripts/ingest_drive_bulk.py` 雛形（state machine + concurrency=8 asyncio） | 🤖 | 3h |
| 5 | EventBridge → SSM RunCommand → bastion cron の Terraform module 雛形 | 🤖 | 2h |
| 6 | CloudWatch EMF 出力を `pgvector_client.search_similar_new_schema()` に注入 | 🤖 | 2h |
| 7 | 営業 PDF 棚卸し（**件数確定、機密区分仕分け**） | 👤 | 4h |
| 8 | S3 state bucket（`teamagent-state`）作成 + IAM role | 🤖+👤 | 1h |
| 9 | 50 PDF で A/B（LocalE5 vs Titan Embed v2 vs Cohere v3）→ 採用判定 | 🤖+👤 | 半日 |
| 10 | 全 500 PDF 一括投入 + CW Dashboard 反映 | 🤖 | 半日 |

合計: 🤖 約 18h / 👤 約 8h → Sprint 4（2 週間）の前半で着地可能。

---

## 参考 Source

- [Google Drive API Usage Limits (公式)](https://developers.google.com/workspace/drive/api/guides/limits)
- [Anthropic Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval)
- [AWS Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)
- [AWS Lambda Quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)
- [LlamaIndex Parallel Ingestion](https://developers.llamaindex.ai/python/examples/ingestion/parallel_execution_ingestion_pipeline/)
- [sentence-transformers efficiency guide](https://sbert.net/docs/sentence_transformer/usage/efficiency.html)

---

## 更新履歴

| 日付 | バージョン | 内容 |
|---|---|---|
| 2026-05-26 | v1.0 | 初版（Day 6 / general-purpose Agent リサーチ統合） |
