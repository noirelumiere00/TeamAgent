---
type: workflow
title: 資料取り込み（ingest）
description: Slack チャネル・Drive フォルダ・共有ドライブ・Sheets から抽出→chunk 化→埋め込み→分類→文脈付与→documents/chunks への保存までを行う IngestRunner と、Drive の増分同期・retry lease・source 健全性の記録、run 末尾の重複排除/テンプレ検出/stale 印、EventBridge→Lambda dispatcher→Fargate のスケジュール実行と手動実行の契約。
tags: [ingest, pgvector, gdrive, slack, gsheets, incremental-sync, retry-lease, ecs-scheduled-task, fail-open]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T07:51:05.076Z
sources:
  - id: openwiki-source-12ebfa30d204595170297d8f
    resource: repo://infra/terraform/ingest_schedule.tf
  - id: openwiki-source-5971d7a2ce4fe090f23af939
    resource: repo://infra/terraform/lambda/ingest_dispatch/handler.py
  - id: openwiki-source-aabfec98b3c141805de3266b
    resource: repo://scripts/ingest_sources.py
  - id: openwiki-source-ed63a46df207937dc180df21
    resource: repo://scripts/run_ingest_fargate.py
  - id: openwiki-source-62bc811c1fd0575d44d9e119
    resource: repo://src/teamagent/ingest/pipeline.py
  - id: openwiki-source-cbafc22e69fac211c4adae53
    resource: repo://src/teamagent/ingest/repository.py
generated: { by: "claude-code", at: "2026-09-29T07:51:05.076Z" }
---

# 資料取り込み（ingest）

## 位置づけ

<!-- openwiki: broken internal link [/openwiki/workflows/knowledge-search.md] link "/openwiki/workflows/knowledge-search.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
社内ナレッジを `documents` + `chunks`（pgvector）へ入れるバッチ処理。MCP gateway の中ではなく、同じ mcp イメージを使う別の ECS Fargate タスクとして走る。ここで作った `cls_*` 分類メタ・`suppressed`・`boilerplate`・`stale` の印を [社内資料検索](/openwiki/workflows/knowledge-search.md) が読む。

| 層 | 場所 | 責任 |
|---|---|---|
| 起動 | `infra/terraform/ingest_schedule.tf`・`infra/terraform/lambda/ingest_dispatch/handler.py` | EventBridge ルール → Lambda dispatcher → `ecs:RunTask` |
| エントリポイント | `scripts/run_ingest_fargate.py` → `scripts/ingest_sources.py` | secret の展開・ソース yaml の解決・終了コード |
| 設定 | `src/teamagent/ingest/loader.py` | `ingest_sources.yaml` を型付き spec に変換（`REPLACE_` 等のプレースホルダは skip） |
| 本体 | `src/teamagent/ingest/pipeline.py` の `IngestRunner` | kind ごとの取り込み・run 末尾のコーパス処理 |
| 保存 | `src/teamagent/ingest/repository.py` の `IngestRepository` | documents/chunks の upsert、retry・cursor・健全性テーブル |
| 段の部品 | `pdf_extract.py`・`office_extract.py`・`classify.py`・`contextualize.py`・`docdedup.py`・`boilerplate.py`・`freshness.py`・`ops_alert.py` | 抽出・分類・文脈付与・重複排除・監視 |

## 起動経路

<!-- openwiki: mermaid parse failed and this diagram was converted to a text fence so it does not break rendering. Fix the diagram source and restore the mermaid fence. Parser error: Heuristic: an unescaped angle bracket inside a label breaks rendering; rephrase the label. -->
```text
flowchart LR
  EB[EventBridge rule<br/>既定 DISABLED] --> L[Lambda ingest_dispatch<br/>同時実行 1]
  L -->|RUNNING 無し| RT[ecs:RunTask]
  L -->|上限内の RUNNING あり| SK[skip]
  L -->|上限超過| ST[StopTask → RunTask]
  M[scripts/aws/run_ingest_task.sh] -->|yaml を S3 へ + run-task| T
  RT --> T[Fargate: run_ingest_fargate.py]
  T --> CLI[ingest_sources.py --commit] --> R[IngestRunner.run]
```

### スケジュール実行

- ルールの既定式は `cron(0 9 ? * MON-FRI *)`（平日 18:00 JST）。`state` は `ingest_rule_enabled`（既定 false）で決まり、既定は DISABLED。live を手で DISABLED にしている運用で、apply のたびに ENABLED へ戻らないようにしたもの。リソース名 `ingest_weekly` と直前のコメント「毎週月 18:00 UTC」は古く、既定式（平日）と食い違う。
- Lambda は `reserved_concurrent_executions = 1` で、EventBridge の重複配送でも判定が直列になる。task family の RUNNING タスクを列挙し、無ければ起動する。経過時間が `INGEST_MAX_RUNTIME_HOURS`（既定 20）以内のものがあれば skip、超えたものは `StopTask` してから起動し直す。`startedAt` が無い起動途中のタスクは `createdAt` で測る。`DescribeTasks` が一部を返さない場合は例外にして起動しない（二重起動の防止を優先）。例外は握らずに投げ直し、EventBridge の再試行（最大 1 回・イベント寿命 3600 秒）に任せる。`RunTask` の `clientToken` は EventBridge のイベント ID と taskdef ARN から作る。
<!-- openwiki: broken internal link [/openwiki/data/rls-and-app-role.md] link "/openwiki/data/rls-and-app-role.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- タスク定義は ARM64、コマンドは `run_ingest_fargate.py`。secret は `DATABASE_URL`・`SLACK_BOT_TOKEN`・`GOOGLE_OAUTH_JSON`（scrape 有効時は `VERTEX_SA_JSON` も）を Secrets Manager から注入する。env で `USE_DOC_CLASSIFY`・`INGEST_RICH_EXTRACT`・`BOILERPLATE_DETECT`・`DOC_DEDUP_DETECT`・`USE_INCREMENTAL_SYNC`・`GOOGLE_FORCE_OAUTH` を ON にし、`BEDROCK_MODEL_ID` は mcp と同じ変数を渡す（未設定だとコード既定のモデルで分類が走り、費用が膨らむため）。`TEAMAGENT_SHARED_COMPANY_DOMAINS` を渡さないと取り込んだ資料の `acl_groups` が空になり、他の社員の検索では見えない（[RLS と実行ロール](/openwiki/data/rls-and-app-role.md)）。
<!-- openwiki: broken internal link [/openwiki/operations/release-gates-and-deploy.md] link "/openwiki/operations/release-gates-and-deploy.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- cpu/memory の terraform 既定（1024/4096）は契約テストに固定された値で、実運用値は CLI の register-task-definition で上書きしている。タスク定義は release gate に依存する（[リリースゲートとデプロイ](/openwiki/operations/release-gates-and-deploy.md)）。

### エントリポイントと終了コード

`run_ingest_fargate.py` は `GOOGLE_OAUTH_JSON` を `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_OAUTH_REFRESH_TOKEN` に展開し（既存の個別 env は上書きしない）、`VERTEX_SA_JSON` を 0600 のファイルにして ADC に渡す。引数は env から作る。

| env | 既定 | 効果 |
|---|---|---|
| `INGEST_SOURCES` | スクリプト既定 `slack,gdrive,gsheets`（terraform は `shared_drives` を足して渡す） | 走らせる kind。`shared_drives` が無いと共有ドライブ crawl は yaml で有効でも走らない |
| `INGEST_DRY_RUN` | `0` | `1` で `--commit` を付けない（DB に書かない） |
| `INGEST_SOURCES_S3_URI` / `INGEST_SOURCES_SHA256` | 未設定 | 設定時は S3 の yaml を使う。取得失敗・sha256 不一致は即 exit 1 で、イメージに焼き込んだ yaml へは戻らない |

どちらの yaml で走ったかは sha256 の先頭 12 桁でログに出る。その後 `runpy` で `ingest_sources.py` を実行し、その `sys.exit` がそのままタスクの終了コードになる。error があれば 1、warning だけなら 2、どちらも無ければ 0（warning 付きの完了を clean success と誤認させないため）。yaml が無い・`DATABASE_URL` 未設定も 2 を返すので、2 だけでは原因を区別できない。

### 手動実行

正規経路は `scripts/aws/run_ingest_task.sh`。タスクロールに yaml 読み取り policy があるかを先に確認し、git 管理の yaml の sha256 を計算して S3 へ置く。続いて `run-task` の `containerOverrides` で `INGEST_SOURCES`・`INGEST_SOURCES_S3_URI`/`_SHA256`・`USE_DOC_KIND_RULES`・Haiku の `BEDROCK_MODEL_ID`・`INGEST_MARK_STALE`（`--mark-stale`）・`INGEST_STALE_ALLOW_MASS`（`--allow-mass-stale`）・`INGEST_ROOT_CHECK_WARN_ONLY` を注入する。STOPPED まで待ち（`WAIT_MAX_MIN` 既定 120 分）、exitCode が 0 以外なら失敗にする（warning の 2 も失敗扱い）。

- 既定の `--sources` は `slack,gdrive,gsheets` で、共有ドライブは含まない。
- dispatcher を通らないため、スケジュール実行との重なりはスクリプトでは防げない。
- 要確認: ネットワーク設定は EventBridge ルールのターゲットの `.EcsParameters` から読む。terraform の定義どおりターゲットが Lambda だと `EcsParameters` が無く、subnets 空で止まる。ターゲットが取れた時点で `SUBNETS` / `SECURITY_GROUPS` env の分岐には入らない。

ローカルでは `python scripts/ingest_sources.py --sources slack --dry-run` のように実行する（`--commit` を付けない限り DB に書かない）。

## IngestRunner.run の流れ

1. `request_id`（`ingest-<12桁>`）を振る。kinds の既定は `slack`・`gdrive`・`gsheets`・`shared_drives`。
2. gdrive を走らせ、かつ yaml に `gdrive_rulebook_root_folder_id` があれば、ルート直下の検査をする。yaml に無い `NN_` フォルダがある、または `99_` 系が yaml に載っていれば exit 1（`INGEST_ROOT_CHECK_WARN_ONLY` で WARNING に下げられる）。
3. `_run_kind` で slack → gdrive → gsheets → shared_drives の順に spec を処理する。spec ごとに try で囲み、失敗は `sources_skipped` と `errors` に数えて次の spec へ進む。失敗時は ops 通知を出し、増分同期 ON なら `connector_state` に失敗を刻む。commit 時は spec ごとの outcome（`success` / `success_with_warnings` / `failed`）を `ingest_connector_runs` に記録する。
4. 全 kind の後に、照合の警告 → 資料まるごと重複排除 → テンプレ検出 → stale 印 → 鮮度検査 の順で run 末尾の処理を行う。

### Drive 1 ファイルの段（`_process_one_gdrive_file`）

| 段 | 処理 | ゲート / 失敗時 |
|---|---|---|
| 変更判定 | 保存済み MD5 と一致する binary は download 以降を丸ごと飛ばす。Google ネイティブ形式は MD5 が無いので必ず処理する。ID+MD5+size+MIME+validator 世代が既知 invalid と一致するものは抑止する | `USE_UNCHANGED_SKIP`（既定 ON）。照会失敗は通常処理へ fail-open |
| 抽出 | PDF・docx/pptx/xlsx・Google ドキュメントは常に抽出。Google スライド/スプレッドシートとテキスト類は `INGEST_RICH_EXTRACT` のときだけ。それ以外の MIME は「題名 + MIME」だけの 1 chunk（`title_only`） | 抽出失敗は warning + retry 記録 + skip |
| chunk 化 | 500 文字・重なり 100。1 ファイルあたり chunk 2,000・埋め込み 2,000・抽出 2,000,000 文字が上限 | 超過は warning + retry 記録 + skip |
| 埋め込み | `embed_passage_batch` があれば `EMBED_BATCH_SIZE`（既定 16）単位、無ければ 1 件ずつ。embedder は検索側と同じ `build_embedder_from_env()` | — |
| 分類 | 先頭 8 chunk と起点フォルダ名を Bedrock に渡し、`cls_*` を `documents.metadata` へ | `USE_DOC_CLASSIFY`。失敗は分類なしで継続 |
| 文脈付与 | 全文を system に載せ、chunk ごとに前置きを作って再埋め込み（Contextual Retrieval） | `USE_CONTEXTUAL_INGEST`（既定 OFF）。chunk 単位で fail-open |
| 保存 | `DocumentUpsert`（Drive の権限から ACL を作る）+ chunks を `_guarded_upsert` へ | 題名だけの版は本文の版を上書きしない |

`_guarded_upsert` は run 内で共有する registry と、DB 側の `upsert_title_only_if_no_content` の 2 段で、folder 経路 → crawl 経路の順に同じファイルが来ても本文を消さないようにする。repository は source 単位のロックを取り、`(source_type, external_id)` で upsert して chunks を削除 → 再投入する（1 トランザクション）。接続は `teamagent_app` ロール・`user_role='admin'`・`application_name=teamagent-ingest`。

### Slack と Sheets

- Slack: チャネル参加者の email を解決し、yaml の `extra_acl_emails` と合わせて `acl_emails` にする。合わせても 0 件ならそのチャネルは skip する（fail-safe）。履歴は 1 ページ（100 件）だけ取り、cursor は保存しない。1 スレッド = 1 document = 1 chunk。
- Sheets: 1 行 = 1 document。タブ名は gid から毎回引き直す（リネームに強い）。営業 FB・ナレッジ共有フォーム・事例集のシートはヘッダ写像で構造化メタに変換する。文脈付与は使わない。
- 差分取り込み `INGEST_DIFFERENTIAL`（既定 OFF・dry-run では無効・Slack と Sheets だけ）: ACL と実行設定（分類/文脈付与の ON/OFF・embedder backend）を含む content hash が保存値と一致すれば、分類・埋め込み・upsert を飛ばして `documents_unchanged` に数える。設定を切り替えた run では hash が変わり、全件を処理し直す。

## 増分同期と retry lease（Drive）

`USE_INCREMENTAL_SYNC`（terraform で ON）のとき、Drive フォルダ経路は次のように動く。

- `connector_state(source_kind, source_id)` の cursor から Drive changes の差分だけを取る。初回・validator 世代の変更・差分取得の失敗ではフル走査になり、走査前に取った start page token を次の基点にする。
- 走査の前に `claim_due_source_retries` を呼ぶ。まず `attempt_count >= 5` の pending 行を resolved にし、その指紋を `ingest_source_health` に `invalid_source`（`retry_exhausted:<理由>`）として永続化する。以後同じバイト列は取り込み前に抑止される（毒ファイル対策）。その後、期限の来た行を `FOR UPDATE SKIP LOCKED` で取り、`lease_owner=request_id`・ランダムな `lease_token`・1800 秒の lease を付ける。
- 処理対象は「変更されたファイル ∪ claim した retry」。claim したファイルは 120 秒ごと（開始時と保存直前は強制）に lease を延長する。延長は owner・token が一致し、期限内の行だけが対象。延長できなければそのファイルを諦める（`retry_lease_lost`）。
- 一時的な失敗は `record_source_retry` で queue に積む。初回は 60 秒後。同じ指紋で失敗が続くと `60 × 2^min(試行回数, 8)` 秒（上限 21600 秒）の指数 backoff になり、中身が変われば試行回数を 1 に戻す。成功時は同じ fence で resolved にする。
- durable な retry 状態を作れなかった（claim 失敗・fence 不備・記録失敗）run では、`IngestDurabilityError` で source を失敗させ、cursor を進めない。cursor の保存自体が失敗した場合も同じ。
- `ingest_jobs` には document 単位の状態を best-effort で残す（成功は `COMMITTED`、失敗は `FAILED_TRANSIENT` で、5 回目に `POISON`）。
- 共有ドライブ crawl は drive ごとに cursor を持つが、retry queue は使わない。
- 追加テーブルが未適用のまま rolling deploy した場合、repository は 60 秒間そのテーブルを使わない。健全性の照会は fail-open、retry の claim は失敗扱いになる（cursor は止まる）。

## 健全性の記録と run 末尾の処理

| テーブル（migration） | 中身 |
|---|---|
| `connector_state`（0012） | source ごとの cursor・連続失敗数・最終エラー・validator 世代 |
| `ingest_jobs`（0005） | document ごとの状態 |
| `ingest_source_health` / `ingest_connector_runs`（0019） | 既知 invalid の指紋 / run×source ごとの outcome・件数・warning 理由 |
| `ingest_source_retries` / `ingest_reconciliation_gaps`（0020・lease token は 0021） | retry queue と lease / 未索引 PDF などの照合ギャップ |

<!-- openwiki: broken internal link [/openwiki/data/postgres-schema-and-migrations.md] link "/openwiki/data/postgres-schema-and-migrations.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
スキーマ全体は [PostgreSQL スキーマとマイグレーション](/openwiki/data/postgres-schema-and-migrations.md)。0021 を当てる前にはスケジュール実行と手動実行を止め、走っているタスクを捌き切る必要がある（旧 lease を解放するため）。

run 末尾の処理（どれも dry-run では走らない）:

- 重複排除 `DOC_DEDUP_DETECT`: 文字 n-gram の MinHash Jaccard（既定 0.7）でほぼ同じ資料を束ね、本文最大の 1 件以外に `suppressed` と `duplicate_of` を付ける。毎回付け直す。fail-open。
- テンプレ検出 `BOILERPLATE_DETECT`: 同じ正規化テキストが `BOILERPLATE_MIN_DOCS`（既定 3）件以上の別資料に出る chunk に `boilerplate` を付ける。suppressed を除いて数えるため、重複排除の後に走る。fail-open。
- stale 印 `INGEST_MARK_STALE`（既定 OFF・手動の `--mark-stale`）: run 中に Drive で見えなかった gdrive 資料に `metadata.stale` を付け（物理削除はしない）、見えたものからは外す。gdrive を走らせていない run、gdrive/shared_drives に error・skip がある run、walk が上限で打ち切られた run では付けも外しもしない。新たな候補が既存の 50% を超えたら exit 1（`INGEST_STALE_ALLOW_MASS` で続行）、30% 超なら WARNING。これだけは fail-open にしない。
- 鮮度検査: slack/gdrive/gsheets それぞれの最新 `ingested_at` が `INGEST_FRESHNESS_MAX_AGE_DAYS`（既定 8）日より古い、または 0 件なら ops へ通知する。ops 通知は `OPS_SLACK_WEBHOOK_URL` が未設定なら何もしない。

## 変更するときの注意

- kind を足すときは `IngestRunner.run` の分岐・`INGEST_SOURCES` のトークン・terraform 変数・手動スクリプトの `--sources` をそろえる。
- 取り込み側の embedder は検索側と同じ構築点（`build_embedder_from_env`）を使う。ずらすとベクトル空間が合わなくなる。
- 増分同期では、retry を durable に残す前に cursor を進めない（取りこぼしの原因になる）。
- 本番へ書き込む実行は `--commit` 付きだけ。env フラグの既定はコード側がほぼ OFF で、ON にしているのは terraform のタスク定義と手動スクリプト。

## テスト

- `tests/ingest/test_pipeline.py`・`test_pipeline_v2.py`（stale・S3 yaml）・`test_pipeline_rich_extract.py`・`test_ingest_speed_guards.py`
- 増分と retry: `test_incremental_sync.py`・`test_shared_drive_incremental.py`・`test_ingest_retry_claim_cap_postgres.py`・`test_repository_connector_state.py`
- 差分と健全性: `test_ingest_differential.py`（`_postgres` 版あり）・`test_ingest_source_health_postgres.py`・`test_invalid_source_observability.py`
- 段の部品: `test_classify.py`・`test_contextualize.py`・`test_docdedup.py`・`test_boilerplate.py`・`test_freshness.py`・`test_loader.py`
- 起動: `tests/infra/test_ingest_dispatch.py`（dispatcher）・`tests/scripts/test_ops_shell_scripts.py`（手動スクリプトの契約文字列）

<!-- openwiki: broken internal link [/openwiki/testing/running-tests.md] link "/openwiki/testing/running-tests.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
走らせ方は [テストの走らせ方](/openwiki/testing/running-tests.md)。
