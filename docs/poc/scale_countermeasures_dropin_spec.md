# 40名同時利用 スケール対策 — 本番Bot drop-in 仕様書

> 対象読者: 本番 TeamAgent（`~/Documents/TeamAgent`）に取り込む実装者。
> 本書は **PoC（teamagent-orchestrator-poc）で実装・テスト済みの3対策** を、本番Botへ
> 最小差分で移植するための配置・配線・設定・根拠をまとめる。**本番コードは本書時点で未改修**
> （PoC で設計を実証した段階）。コピー元の実体は PoC の下記ファイル。

## 0. TL;DR（結論）

40名同時で壊れる箇所は3つ。**入口・LLM・DB の3層すべてに総量規制**を入れて初めて耐える。

| # | 対策 | 何を防ぐ | PoC 実体 | 既定値 |
|---|------|---------|---------|--------|
| P0-0 | **RequestGate**（同時≤4＋FIFOキュー） | 全体の同時実行爆発（ThreadPool/メモリ/下流全部） | `src/teamagent/runtime/request_gate.py` | concurrency=4, queue_max=64 |
| P0-② | **Bedrock リトライ**（指数バックオフ+フルジッタ） | ThrottlingException / 5xx / 一時断での即エラー | `src/teamagent/runtime/retry.py` + `adapters/bedrock_client.py` | 5回 / base0.5s / cap20s |
| P0-① | **DB コネクションプール**（返却時 RESET ROLE） | RDS `max_connections` 枯渇・接続storm | `src/teamagent/adapters/pg_pool.py` + `adapters/pgvector_client.py` | max_size=8 |

**3つが揃って初めて効く理由**: RequestGate だけでは throttle と接続枯渇は残る。リトライだけでは
同時実行を絞らないと throttle が雪崩れてリトライ嵐になる。プールだけでは LLM 側が詰まる。
入口で 4 に絞る → 下流の throttle/接続を「絞られた量」で捌く、という依存関係。

---

## 1. 背景：40名同時で何が壊れるか（現状＝無対策）

- **同時実行制御が無い**: Slack ハンドラは到着順に `dispatch_auto()` を即実行。40名が同時に投げると
  40 本の重い処理（LLM 複数回 + 検索 + rerank）が並走 → ThreadPool 飽和・メモリ・下流の同時叩き。
- **Bedrock はリトライ無し**: `converse()` / `rerank()` は素の boto3 呼び出し。`ThrottlingException` が
  来たら即ユーザにエラー。本番 bot も同様（リトライ実装なしを確認済み）。
- **DB は毎回 connect→close**: `PgVectorClient.connection()` がリクエストごとに新規接続を張って閉じる。
  40同時 + ingest で TLS+認証の往復が嵩み、RDS の `max_connections` を圧迫。

---

## 2. P0-0 RequestGate（入口の総量規制）

### 2.1 設計
`asyncio.Semaphore(concurrency)` の待機列をそのまま FIFO キューに使う。待機が `queue_max` を超えたら
**即時・明示拒否**（無言ドロップ禁止＝ユーザに「混雑中」を返す）。`acquire_timeout_s` で待ち過ぎも拒否。
`try/finally` でスロットを必ず返す（実行中タスクが cancel されてもリークしない）。観測値 `GateMetrics`
（in_flight / peak_in_flight / waiting / rejected_* / completed）を持つ。

### 2.2 本番への配線（最小差分）
1. `request_gate.py` を `src/teamagent/runtime/` にコピー。
2. **プロセスに1個**（module-level / アプリ起動時）生成:
   ```python
   from teamagent.runtime.request_gate import RequestGate, QueueFullError, GateTimeoutError
   _GATE = RequestGate(concurrency=4, queue_max=64, acquire_timeout_s=None)
   ```
3. `build_app()` 内の **`handle_app_mention` と `handle_message`** で、**ack は Gate の外**（Slack の
   3秒 ack を守る）、**重い `dispatch_auto` だけを Gate に通す**:
   ```python
   await say(text=build_ack_message(query))          # ← Gate の外（即時）
   try:
       text, blocks = await _GATE.submit(disp.dispatch_auto, query, request_id, user_id)
   except QueueFullError:
       await say(text="ただいま混雑しています。少し待って再度お試しください。🙏")
       return
   except GateTimeoutError:
       await say(text="順番待ちが長くなっています。後ほどお試しください。🙏")
       return
   ```
4. スラッシュコマンド（`handle_teamagent_*`）も `await ack()`（3秒ack）は Gate の外、`respond()` する
   重い本体を `_GATE.submit(...)` で包む。

### 2.3 注意
- Gate は**1プロセス1個**を共有（複数生成すると上限が掛け算になり無意味）。
- ack/受付メッセージを Gate に通さないこと（通すと混雑時に3秒 ack を落とす）。
- **暗黙の executor 上限に注意**: skill は `loop.run_in_executor(None, ...)` でデフォルト
  ThreadPoolExecutor（`max_workers=min(32, cpu+4)`、2 vCPU なら 6）に流れる。Gate の concurrency=4 と
  整合させ、かつ「Gate を通った処理だけが重い executor を使う」状態を保つこと。必要なら明示的に
  `ThreadPoolExecutor(max_workers=concurrency+α)` を作って `loop.set_default_executor` する。
- **PoC 配線済み（参照実装）**: `runtime/slack_bot.py` の `build_app()` で gate を1個生成し、
  `handle_app_mention` / `handle_message` の `dispatch_auto` を `gate.submit(...)` で包み、
  `QueueFullError`/`GateTimeoutError` を「混雑中」メッセージで握っている。env
  `REQUEST_GATE_CONCURRENCY`(=4) / `REQUEST_GATE_QUEUE_MAX`(=64) で調整可。本番も同型で配線する。

---

## 3. P0-② Bedrock リトライ

### 3.1 設計
`runtime/retry.py` の汎用 `call_with_retry(fn, is_retryable, policy, sleep, jitter, on_retry)`。
フルジッタ（AWS 推奨）で thundering herd を散らす。リトライ枯渇時は**最後の例外をそのまま送出**
（上位の ClientError ハンドラを壊さない）。Bedrock 固有の分類は `bedrock_client._is_bedrock_retryable`
（Throttling 系 / 429・5xx / `BotoCoreError`=接続断 を一過性とし、ValidationException 等は非リトライ）。

### 3.2 本番への配線
1. `retry.py` を `src/teamagent/runtime/` にコピー。
2. 本番 `bedrock_client.py` に以下を移植（PoC の差分と同型）:
   - `_is_bedrock_retryable` / `_RETRYABLE_ERROR_CODES` / `_RETRYABLE_HTTP_STATUS` を追加。
   - `__init__` で **botocore Config を付与**して内部リトライを一元化:
     ```python
     boto_config = Config(
         retries={"total_max_attempts": 1, "mode": "standard"},  # ← max_attempts ではなく total_max_attempts
         connect_timeout=10, read_timeout=120,
         tcp_keepalive=True,                                      # VPC/NAT 350s 無言切断対策
     )
     self._client = client or boto3.client("bedrock-runtime", region_name=region, config=boto_config)
     ```
     - **⚠️ 必ず `total_max_attempts` を使う**。Config の `max_attempts` は「リトライ回数（初回を
       含まない）」と解釈され、`max_attempts=1` でも**解決値は total_max_attempts=2（初回+1リトライ）**
       になる（実機 botocore 1.43.19 で確認：`client.meta.config.retries == {'total_max_attempts': 2}`）。
       これだと自前 `call_with_retry` と二重化し、最悪 5×2=10 回 API を叩く。`total_max_attempts=1`
       なら解決値も 1（初回のみ）になる。**回帰防止テスト**（実 boto3 client の解決値を assert）を併設すること。
     - `tcp_keepalive=True`: NAT/NLB/VPC endpoint の固定 350s アイドルで TCP が無言切断され、
       再利用時に 70s+ の cold-start/接続リセットになるのを防ぐ（AWS Bedrock 公式推奨）。
       **OS 側も `net.ipv4.tcp_keepalive_time < 350`**（例 45）を sysctl で設定（デプロイ runbook）。
   - `converse()` / `rerank()` の boto3 呼び出しを `call_with_retry(lambda: ..., is_retryable=_is_bedrock_retryable, policy=self._retry_policy, on_retry=self._make_retry_logger(...))` で包む。
   - latency 計測は **retry を含めた全体** を測る（start を retry の外に置く＝ユーザ体感を正直に反映）。
3. env 上書き（任意）: `BEDROCK_MAX_ATTEMPTS`(=5) / `BEDROCK_RETRY_BASE_S`(=0.5) / `BEDROCK_RETRY_MAX_S`(=20)。

### 3.3 最悪待ち時間の見積り（Slack UX との両立）
既定 5回（=初回+4リトライ）、フルジッタなので待ちは各回 [0, cap] の一様乱数。cap 系列 = 0.5,1,2,4。
**最悪**（毎回 cap 上限を引く）で 0.5+1+2+4 = **7.5s** の追加待ち。受付メッセージを先に出しているので
3秒 ack は割れない。ただし throttle が**慢性的**なら待ちが伸びるので、その時は同時実行（concurrency）を
下げるか Bedrock の上限緩和申請を行う（`bedrock_*_retry` ログの頻度が一次データ）。

**推奨（多層防御）**: 1リクエスト1コール内で複数回 LLM を叩く Skill もあるため、リトライ待ちが積み上がる
最悪ケースに備えて **dispatch 全体に上限**を設ける（例: `asyncio.wait_for(gate.submit(dispatch_auto, ...),
timeout=60)`）。タイムアウト時は「時間がかかっています」をユーザに返し、處理は打ち切る。
`read_timeout=120s` × 二重リトライ排除（`total_max_attempts=1`）で1コールの上限は抑えてあるが、
コール多重時の総和はアプリ層タイムアウトで頭打ちにするのが堅い。

---

## 4. P0-① DB コネクションプール

### 4.1 設計
`adapters/pg_pool.py` の `ConnectionPool`（依存ゼロ・スレッドセーフ）。`threading.Semaphore(max_size)` で
**総貸出数 ≤ max_size** を保証、`threading.Lock` で idle 集合を保護。**総物理接続数 ≤ max_size** の不変条件
（新規生成は idle が空で permit 保持時のみ）。`PoolStats`（in_use/idle/open_total/timeouts/reset_failures）を観測。

**RLS の肝**: `SET ROLE` は**セッション持続**（commit 跨ぎ）。使い回す接続に前の借用者のロールが残ると
越権の温床。よって **返却時に必ず rollback→`RESET ROLE`→commit**（commit しないと RESET が巻き戻る）。
reset に失敗した接続（＝壊れている）は**プールに戻さず破棄**。`set_config(...,is_local=true)` の GUC は
txn-local なので借用側 commit/rollback で消えるが、返却 reset の rollback でも掃除する。

### 4.2 本番への配線
1. `pg_pool.py` を `src/teamagent/adapters/` にコピー。
2. 本番 `pgvector_client.py` に PoC と同型の改修:
   - `__init__(self, dsn, *, pool=None)`、`from_env()` で **既定プール化**（`PGVECTOR_POOL_MAX`>0 のとき）。
   - `connection()` を **プール経路 / 直結経路** に分岐（直結は後方互換）。SET ROLE+GUC は共通 `_apply_session`。
   - `close()` を追加し、**アプリ shutdown で呼ぶ**（保有接続を解放）。
3. env: `PGVECTOR_POOL_MAX`(=8) / `PGVECTOR_POOL_MIN`(=0、ウォームアップ) / `PGVECTOR_POOL_TIMEOUT_S`(=10)。
   `PGVECTOR_POOL_MAX=0` で**プール無効＝旧挙動**（緊急フォールバック）。

### 4.3 本番は psycopg_pool でも可（推奨オプション）
本書の `pg_pool.py` は**依存を増やさず同じ不変条件を満たす最小実装**。本番は成熟した
`psycopg_pool.ConnectionPool` に置換しても良い。その場合の設定対応:
- `configure=` で `register_vector(conn)`（接続生成時に1回）。
- `reset=` で `RESET ROLE`（＝本書の `_default_reset` 相当）。
- `check=ConnectionPool.check_connection` で貸出時の死活確認。
- `max_size` / `timeout` は本書と同じ考え方。

### 4.4 ⚠️ イベントループ・ブロッキングの注意（重要）
`pg_pool` の `connection()` 取得は **ブロッキング**（`threading.Semaphore.acquire(timeout)`）。
**現状の本番も `psycopg.connect()` を同期で呼んでおり、すでにイベントループをブロックしている**ため
本対策で悪化はしないが、根本的には:
- DB アクセスは `run_in_executor` でスレッドへ逃がす（同期プール＝スレッドプールと相性良い）。
- もしくは完全 async 化するなら `psycopg_pool.AsyncConnectionPool` を使う。
- 当面は **`PGVECTOR_POOL_MAX` を「同時に DB を触り得る最大数」以上**に設定してブロック頻度を下げる
  （RequestGate=4 + ingest/slash 等の非ゲート分を見込んで 8 を既定とした）。

---

## 5. 推奨設定値（40名規模・初期）

| env | 既定 | 根拠 |
|-----|------|------|
| RequestGate concurrency | 4 | ユーザ要望「4並列、以降キュー」。Bedrock throttle と DB 負荷の実効上限。 |
| RequestGate queue_max | 64 | 40名 × 想定1.5本程度の溜まりを許容。超過は「混雑中」。 |
| BEDROCK_MAX_ATTEMPTS | 5 | 最悪追加待ち 7.5s（§3.3）。3秒 ack は別途死守。 |
| PGVECTOR_POOL_MAX | 8 | concurrency=4 + 非ゲート経路（ingest/slash/oauth）分の余裕。**RDS `max_connections` と他コンシューマの合計が上限を超えないこと**を必ず確認。 |
| PGVECTOR_POOL_TIMEOUT_S | 10 | 枯渇時に無限ハングさせない。タイムアウトは `PoolTimeoutError` で表面化。 |

> RDS インスタンスクラスの `max_connections` を確認し、`PGVECTOR_POOL_MAX × プロセス数 + ingest +
> 予備 < max_connections` を満たすこと。スケールアウト（複数プロセス/コンテナ）時は **プールはプロセス毎**
> なので合算で見積もる。

---

## 6. 観測（管理画面 Phase B 連携）

3対策はそのまま「混雑・コスト・健全性」の一次データになる:
- `RequestGate.metrics`（GateMetrics）: 実効並列度・必要キュー深さ・拒否数 → **混雑度の可視化**。
- `bedrock_converse_retry` / `bedrock_rerank_retry` 構造化ログ（attempt / backoff_s / error_code）→
  **throttle 頻度**。多発なら concurrency 調整 or 上限緩和申請の判断材料。
- `ConnectionPool.stats()`（PoolStats）: in_use / idle / timeouts / reset_failures → **DB 逼迫度**。
- 既存の `bedrock_converse` ログ（cost_usd / tokens / latency_ms）→ **コスト・レイテンシ**。

---

## 7. ロールアウト手順とフォールバック

1. まず **DBプール**と **Bedrockリトライ**を入れる（ユーザ影響は基本的にプラスのみ、挙動非破壊）。
2. 次に **RequestGate** を入れて concurrency=4 で開始。`peak_waiting` と拒否率を見て queue_max を調整。
3. 異常時フォールバック（env だけで無効化可能・コード変更不要）:
   - DBプール無効化: `PGVECTOR_POOL_MAX=0`（旧 connect→close に戻る）。
   - Bedrock リトライ実質無効化: `BEDROCK_MAX_ATTEMPTS=1`。
   - RequestGate: concurrency を上げる（実質ゲート緩和）/ ハンドラの `submit` 経由を外す。

---

## 8. PoC でのテスト状況（移植時の参照）

- `tests/runtime/test_request_gate.py`（6）: 同時≤4・全件完了・キュー満杯拒否・cancel解放・FIFO・タイムアウト。
- `tests/runtime/test_retry.py`（8）+ `tests/adapters/test_bedrock_retry.py`（12）: throttle→成功 / 枯渇送出 /
  非リトライ分類 / 5xx / バックオフ系列 / フルジッタ境界。
- `tests/adapters/test_pg_pool.py`（9）: 総数上限・再利用・RESET ROLE 返却・壊れ接続破棄・idle失効退避・
  ウォームアップ・close・**40スレッドで同時貸出≤4**。
- いずれも **課金0・実DB0・決定論**（sleep/jitter/接続を注入）。本番移植後も同テストを持ち込み可能。

---

_作成: 2026-06-04 / PoC branch poc/multiskill-orchestrator。本番反映は別途レビュー後。_
