---
type: integration
title: Bedrock / Gemini 呼び出しとリトライ・コスト
description: Adapter 層の bedrock_client（Converse・Rerank・Cohere Embed）と gemini_client（Vertex の動画分析・Google 検索グラウンディング）の構築・価格表・usage ログ・prompt caching、共通の call_with_retry（フルジッタ・429 別枠・deadline）、埋め込みの backend 切替、動画クォータ（video_usage）と外部 SaaS 費用台帳（cost_guard）。
tags: [bedrock, gemini, vertex-ai, retry, cost, prompt-caching, embeddings, quota, cost-guard]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T07:51:05.076Z
sources:
  - id: openwiki-source-40601219bf6258b18d3bc6df
    resource: repo://infra/terraform/cloudwatch_fargate.tf
  - id: openwiki-source-80a8aaae0d367a78369a17ab
    resource: repo://infra/terraform/cloudwatch.tf
  - id: openwiki-source-c6bdc7409c7b563ddbe16f57
    resource: repo://infra/terraform/variables_fargate.tf
  - id: openwiki-source-a60ed4ddb577d09f70e16d20
    resource: repo://src/teamagent/adapters/bedrock_client.py
  - id: openwiki-source-f0beec64f4b48a6f1a9e2d5d
    resource: repo://src/teamagent/adapters/cost_guard.py
  - id: openwiki-source-fc5957b7a515654920171e70
    resource: repo://src/teamagent/adapters/embeddings_client.py
  - id: openwiki-source-2ef152761919cb94ac7b70b2
    resource: repo://src/teamagent/adapters/gemini_client.py
  - id: openwiki-source-d423e1eb7d1bb76681e55202
    resource: repo://src/teamagent/adapters/quota_store.py
  - id: openwiki-source-1ff2c467599c597ccf63387f
    resource: repo://src/teamagent/adapters/retry.py
generated: { by: "claude-code", at: "2026-09-30T04:50:20.078Z" }
---

# Bedrock / Gemini 呼び出しとリトライ・コスト

## 位置づけ

LLM・埋め込み・再ランクの呼び出しは、すべて `src/teamagent/adapters/` の薄いラッパーを通す。Skill は boto3 や google-genai を直接 import しない（[3層分離と Skill の契約](../architecture/layering-and-skill-contract.md)）。ラッパーは「リトライ」「推算コストの構造化ログ」「秘密やプロンプトをログに出さないこと」をまとめて受け持つ。

| モジュール | 役割 |
|---|---|
| `bedrock_client.py` | Bedrock Converse（Claude）・Rerank（Cohere Rerank v3.5）・InvokeModel（Cohere Embed multilingual v3）。価格表と usage ログ |
| `gemini_client.py` | Gemini（Vertex AI または AI Studio キー）。動画分析・テキスト生成・Google 検索グラウンディング |
| `embeddings_client.py` | `Embedder` の実装（ローカル e5 / Bedrock Cohere）と、backend と DB 列の組み合わせ検証 |
| `retry.py` | 依存ゼロの同期リトライ `call_with_retry`（Bedrock と Gemini が共用） |
| `quota_store.py` | 動画分析の月間本数クォータ（Postgres `video_usage`） |
| `cost_guard.py` | 外部 SaaS（Apify など）の月次ドル台帳（DynamoDB） |

## Bedrock（bedrock_client.py）

### 構築と既定値

`BedrockClient.from_env()` は `AWS_REGION`（既定 `ap-northeast-1`）と `BEDROCK_MODEL_ID` を読む。`BEDROCK_MODEL_ID` が無いときの既定は **Haiku 4.5 の JP 推論プロファイル**。以前の既定は Sonnet だったが、env を入れ忘れたタスクが気づかれないまま Sonnet で課金される事故が実際に起きたため変えた。高品質が要る呼び出しは env か `model_id_override`（`proposal_builder`、`omiyage_report` の `OMIYAGE_*_BEDROCK_MODEL_ID`）で明示する。Terraform 側も `var.mcp_model_id` の validation で、MCP が使えるモデルを JP Haiku 4.5 プロファイルだけに固定している（`infra/terraform/variables_fargate.tf`）。

boto3 クライアントの設定:

- botocore の内部リトライは `total_max_attempts=1` で**切る**。リトライは `call_with_retry` にまとめ、待ち時間が掛け算になるのを防ぐ（`max_attempts=1` と書くと初回＋1 回のリトライになってしまうので使わない）。
- `connect_timeout=10`、`read_timeout` は `BEDROCK_READ_TIMEOUT`（既定 120 秒。16k tokens 級の長い生成では延ばす）。
- `tcp_keepalive=True`。VPC/NAT の 350 秒アイドルでの無言切断対策。
- Rerank は別クライアント `bedrock-agent-runtime` を使う。モデル ARN はリージョンから組み立てる（`BEDROCK_RERANK_MODEL_ARN` で上書き可）。

### 3 つの呼び出し

| メソッド | API | 要点 |
|---|---|---|
| `converse()` | Converse | 既定 `temperature=0.1`・`max_tokens=4096`。本文は最初の text ブロック |
| `rerank()` | Agent Runtime Rerank | 文書は 1〜1000 件（外れたら `ValueError`）。1 回 = 1 query として課金を推算 |
| `embed_texts()` | InvokeModel（Cohere Embed） | `input_type` は `search_query` / `search_document` だけ。96 件ずつ分けて呼び、長すぎる文は `truncate: END` で末尾を切る |

### prompt caching

`converse(cache_system=True)` にすると system ブロックの末尾に `{"cachePoint": {"type": "default"}}` を足す。キャッシュされるのは **system プロンプトだけ**で、messages はキャッシュしない。同じ system を何度も使う Skill（search・query_planner・morning_digest・mail 系・proposal 系・ingest の classify / contextualize / entity_extract など）が使っている。contextualize は文書の全文を system に載せてキャッシュする。

### 価格表と usage ログ

`_PRICE_TABLE` のキーは推論プロファイルの地域接頭辞つきの ID（`jp.` / `us.` × Sonnet 4.6 / Haiku 4.5）で、`model_id.startswith(prefix)` で照合する。表に無いモデル（別の地域接頭辞や新しいモデル）は**何も言わずにコスト 0.0** になる。計算式:

- 新規入力 = `inputTokens − cacheRead − cacheWrite`（コードは inputTokens がキャッシュ分を含む前提で差し引いている）
- cache read = 入力単価 × 0.1、cache write = 入力単価 × 1.25、出力は出力単価

各呼び出しは request_id つきの構造化ログを出す。`bedrock_converse`（トークン 4 種・`cost_usd`・`latency_ms`・`stop_reason`）、`bedrock_rerank`（1 query あたり $0.002）、`bedrock_embed`（トークン数が返らないので 4 文字 ≒ 1 token の推算）。リトライは `bedrock_*_retry` の warning（`attempt`・`backoff_s`・`error_code`）で、スロットリングの頻度を見る材料になる。

CloudWatch の metric filter `{ $.cost_usd = * }` が、app ロググループでは `BedrockCostUSD`、mcp ロググループでは `McpCostUSD` として合算する。**キー名で拾う**ので、Bedrock に限らず Gemini や cost_guard の記帳ログなど、`cost_usd` を持つ JSON 行はすべて足される。MCP gateway の usage ログが `tool_cost_usd` という別名を使うのは二重計上を避けるため（[MCP gateway](../architecture/mcp-gateway.md)、[観測・利用記録・コスト管理](../operations/observability-and-cost.md)）。なお L2 オーケストレータの `sdk_runner.Price` はこの価格表とは別の単価（Sonnet 相当の概算）を持つ（[オーケストレータ](../architecture/orchestrator.md)）。

## リトライ（retry.py）

`call_with_retry(fn, is_retryable=...)` の仕組み:

- 「一過性か」の判断は呼び出し側の述語に任せる。本モジュールは特定サービスに依存しない。
- 待ち時間はフルジッタ `uniform(0, min(cap, base × 2^(n−1)))`。同時に失敗した呼び出しのリトライが重ならないように散らす。
- `is_rate_limited` と `RateLimitPolicy` を渡すと、429 は別の回数で粘る（429 はすぐ返るので試行のコストが小さい）。429 以外の失敗は別に数え、`RetryPolicy.max_attempts` に達したら止める。総試行回数の上限は両ポリシーの大きい方。
- `deadline_s` を渡すと「経過時間＋次の待ち＋`attempt_timeout_s`」が上限を超える時点で打ち切る。
- 回数を使い切ったら**最後の例外をそのまま投げ直す**（上位の `ClientError` 処理を壊さない）。`sleep`・`jitter`・`clock` は注入でき、テストは実際には待たない。

| 呼び出し | 通常の失敗 | 429 | deadline |
|---|---|---|---|
| Bedrock（converse / rerank / embed） | 5 回・base 0.5s・cap 20s（`BEDROCK_MAX_ATTEMPTS` / `BEDROCK_RETRY_BASE_S` / `BEDROCK_RETRY_MAX_S`） | 別枠なし | なし |
| Gemini 動画・テキスト | 3 回・0.6s・8s（`GEMINI_RETRY_MAX_ATTEMPTS`） | 8 回・1.0s・12s・最小 0.5s | なし |
| Gemini 検索グラウンディング | 2 回・0.6s・4s（`GEMINI_GROUNDED_RETRY_MAX_ATTEMPTS`） | 8 回・0.6s・4s | `timeout_s × 試行回数 + 4s` |

429 の回数はどちらの Gemini 経路も `GEMINI_RATE_LIMIT_RETRY_MAX_ATTEMPTS` で変えられる。

一過性の判定:

- **Bedrock** `_is_bedrock_retryable`: スロットリング・5xx 系のエラーコード、または HTTP 429/500/502/503/504 の `ClientError`、それと `BotoCoreError`（接続断・読み取りタイムアウト）。`ValidationException`・`AccessDeniedException`・`ServiceQuotaExceededException` はリトライしない。
- **Gemini** `_is_retryable_vertex`: google-genai は版によって例外の型が変わるので、`code` 属性（429/500/503）とメッセージの文字列で判定する。`Cannot fetch content` / `ROBOTED`（URL を取れない）は常に対象外。

グラウンディング経路を 2 回に絞っているのは、3 回にすると OpenClaw のターン上限（実測でおよそ 181 秒）を超え、そのターンの応答がまるごと失われるため。

## Gemini（gemini_client.py）

- **認証**: `GEMINI_USE_VERTEX=true` ＋ `GEMINI_VERTEX_PROJECT`（無ければ `GOOGLE_CLOUD_PROJECT`）の Vertex AI 経路を優先する。認証は ADC で、本番では entrypoint がサービスアカウントの JSON を書き出して `GOOGLE_APPLICATION_CREDENTIALS` を差し替える（[MCP gateway](../architecture/mcp-gateway.md)）。Vertex でないときは `GEMINI_API_KEY` を使い、プレースホルダの値は `RuntimeError` で拒否する。google-genai は最初に使う時点で import する。
- **モデルとロケーション**: 既定は `gemini-3.5-flash` ＋ `global`。Gemini 3 系は Vertex では `global` でしか応答しないので、`resolve_location` が 3 系に地域ロケーションが指定されていても `global` に読み替えて warning を出す。2.5 系は Vertex で 2026-10-16 に廃止予定。`docs/v3.2/system_reference.md` には「Gemini 2.5 Flash」という記述が残っている箇所があるが、既定値はコード側が正しい。
- **呼び出し**: `analyze_video_url`（file_uri で直接取れるのは YouTube 系だけ）、`analyze_video_bytes`（TikTok / Instagram などはダウンロードした bytes を inline で渡す。上限はおよそ 20MB）、`generate_text`、`generate_with_google_search`。
- **グラウンディング**: 出典は LLM の本文からではなく `groundingMetadata` から機械的に組み立てる。`sources` の添字は groundingChunks と 1 対 1（web 以外は空のプレースホルダ）で、`grounded=False`（出典 URI が 1 つも無い）なら呼び出し側は fail-closed にする。Google 検索ツールは構造化出力（responseSchema）と一緒に使えない。
- **コスト**: `_PRICE_TABLE.get(model_id)` による**完全一致**の照合（バージョン接尾辞つきの ID は 0 になる）。thinking tokens は出力単価で足す。グラウンディングで出典が付いた呼び出しには 1 回あたり $0.035 を加える。
- **失敗時**: ログは `logger.exception` に request_id だけを付け、生の URL やプロンプトは残さない。上には例外の型名だけを含む `RuntimeError` を投げる。URL を取得できない場合は `VIDEO_URL_NOT_FETCHABLE:` を付けて、利用者への案内に変えられるようにする。
- **呼び出し元**: video・video_approval・video_algorithm・tiktok_search・web_research・clip_proposal（[動画・TikTok 分析](../workflows/video-and-tiktok-analysis.md)、[リサーチ系ツール](../workflows/research-tools.md)）。

## 埋め込み（embeddings_client.py）

`Embedder` は `embed()`（検索クエリ）と `embed_passage()`（取り込む資料）の 2 つを持つ。どちらの backend も非対称な埋め込みを前提にしている。

- `build_embedder_from_env()` が**唯一の構築点**。`EMBEDDER_BACKEND`（既定 `local`）と `EMBEDDING_COLUMN`（既定 `embedding`）の組み合わせは `local⇄embedding` と `cohere⇄embedding_cohere` しか許さず、それ以外は起動時に `ValueError` で止める。クエリと DB 列でベクトル空間が食い違うと検索が全部壊れるため。列名は SQL 識別子に埋め込むので allowlist で検査する。
- `LocalE5Embedder`（multilingual-e5-large、`LOCAL_EMBED_MODEL`）: クエリには常に `query: ` を付ける。`passage: ` は `USE_E5_PASSAGE_PREFIX`（旧名 `E5_PASSAGE_PREFIX` も使える）が真のときだけで、偽のときは既存のコーパスに合わせて `query: ` を付ける。すでに接頭辞がある文には重ねて付けない。
- `BedrockCohereEmbedder`: 非対称の区別は `input_type`（`search_query` / `search_document`）で表し、呼び出しは `BedrockClient.embed_texts()` に任せる。Cohere 用のベクトルは migration 0016 で追加した並行列 `chunks.embedding_cohere` に入り、env を戻せば e5 に戻せる（[ナレッジ検索](../workflows/knowledge-search.md)、[取り込みパイプライン](../workflows/ingest-pipeline.md)）。

## 費用の上限: quota_store と cost_guard

コスト防御は役割ごとに分かれている（`cost_guard.py` 冒頭）。

| 対象 | 仕組み |
|---|---|
| AWS 請求全体の予算・異常検知 | `infra/terraform/budgets.tf` |
| L2 オーケストレータ 1 回あたりの Bedrock 費用 | `sdk_runner` の cost cap |
| 動画分析の月間本数 | `VideoQuotaStore`（Postgres） |
| 外部 SaaS のドル | `CostGuard`（DynamoDB） |

Bedrock と Gemini の 1 回ごとの呼び出しそのものは、cost_guard でも quota でも止めない。

### VideoQuotaStore（動画分析の本数）

- 台帳は `video_usage(user_email, month, used)`（migration 0017）。month は JST の `YYYY-MM`、上限は `VIDEO_MONTHLY_QUOTA`（既定 20）。`VIDEO_QUOTA_ENABLED` が真のときだけ働き、Terraform の既定は false。
- `try_consume` は、判定と加算を条件付き UPSERT の 1 文で行う（`ON CONFLICT ... DO UPDATE ... WHERE used + EXCLUDED.used <= limit`）。月の最初の INSERT には WHERE が効かないので、`count > limit` は DB に送る前に拒否する。接続は `app_role="teamagent_app"` ＋本人の email（RLS）。
- DB が落ちているときは **fail-open**（`allowed=True`・`used=-1`・`video_quota_failed` の warning）。セキュリティの境界ではなくコスト制御なので、止めないことを優先する。
- ブロック時の文面 `quota_block_message` は事実に合わせて 2 通りに書き分ける。残りが 0 なら `VIDEO_QUOTA_EXCEEDED`、残りがあるなら `VIDEO_QUOTA_PARTIAL_AVAILABLE`（「残り N 本で進めますか？」）。
- 呼び出し元: video Skill は Gemini に投げる直前に 1 本消費する（キャッシュに当たったら消費しない）。video_algorithm は波ごとにまとめて消費し、2 波目以降は残り本数まで減らして続ける。quota が ON で email が無いと `VIDEO_QUOTA_IDENTITY_REQUIRED` で fail-closed にする（store 自体は email が空だと何もせず許可する）。検索上位チェックの続きは `peek_remaining` で読むだけ。

### CostGuard（外部 SaaS のドル）

- `COST_GUARD_TABLE` が空なら `from_env()` は None を返し、ガードは無効になる。上限は `COST_<PROVIDER>_MONTHLY_USD`・`COST_<PROVIDER>_PER_CALL_USD`・`COST_PER_USER_MONTHLY_USD`（未設定の段は無制限）。
- 行のキーは `{provider}#{YYYY-MM}`（全体）と `{provider}#{YYYY-MM}#{email}`（個人）。金額は浮動小数の誤差を避けてマイクロ USD の整数で `ADD` し、90 日の TTL を付ける。
- 実行の流れは `reserve`（概算額を条件付きで原子的に加算）→ 実行 → `settle`（実費との差額で精算）。同時に走っても上限をすり抜けない。個人枠の予約に失敗したら、先に取った全体枠を戻す。1 回の見積もりが月次枠や個人枠を超える場合は、最初から拒否する（その月の最初の行では条件式が素通りしてしまうため）。
- 上限を超えたら `CostLimitExceededError`（fail-close。文面は利用者に件数を減らすか管理者へ連絡するよう促す）。80% を超えると警告を返すが、実行は続ける。DynamoDB の障害は fail-open（warning）。旧 API の `check` / `record` も残っている。
- 使っているのは `ApifyClient`（`ledger=CostGuard.from_env()`）経由の x_research・tiktok_comment_mining・search_surface_check・x_buzz_job と、TikTok 動画のフォールバック。Apify の run が timeout・失敗したときは、概算の上限額で記帳する。

## 変更するときの注意

- 新しいモデルを使うときは価格表に追加する。追加しないとコストが 0 で記録され、ダッシュボードの上では安く見える。Gemini は ID の完全一致で照合する。
- botocore の内部リトライを有効に戻さない。リトライ回数を増やすときは、OpenClaw のターン上限と Slack の応答時間の中に収まるか確かめる。
- 新しいログに `cost_usd` キーを足すと、そのまま CloudWatch のコスト metric に合算される。すでにどこかで記録している費用なら別のキー名を使う。
- Bedrock のコスト計算は「inputTokens がキャッシュ分を含む」というコード内の前提に依存している。Bedrock の現行の usage 仕様との照合はこのページではしていない。

## テスト

- `tests/adapters/test_bedrock_client.py`（価格・caching・rerank）、`tests/adapters/test_bedrock_retry.py`（分類・botocore の総試行回数 1）、`tests/runtime/test_retry.py`（429 の別枠・deadline・最後の例外を投げ直すこと）。
- `tests/adapters/test_gemini_client.py`（分類・ロケーションの読み替え・thinking とグラウンディングの課金・429 の粘り）。
- `tests/adapters/test_embeddings_client.py`・`test_embeddings_client_batch.py`・`test_bedrock_cohere_embedder.py`。
- `tests/adapters/test_quota_store.py`・`tests/adapters/test_cost_guard.py`・`tests/skills/video_algorithm/test_cost_guards.py`。
