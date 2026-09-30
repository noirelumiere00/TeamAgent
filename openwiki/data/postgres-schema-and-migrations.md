---
type: data
title: PostgreSQL / pgvector のスキーマとマイグレーション
description: RDS PostgreSQL 16 + pgvector に置かれる主要テーブル（documents/chunks と埋め込み列・ingest 状態・OAuth token・usage・朝ダイジェストの状態・本人メモ など）と、scripts/migrate.py による forward-only・チェックサム付きマイグレーションの運用。
tags: [database, postgresql, pgvector, migrations, schema]
verified:
  - by: openwiki/0.6.1
    at: 2026-09-29T07:51:05.076Z
sources:
  - id: openwiki-source-6cbcf9c6a2e5d58ba7368e54
    resource: repo://infra/docker/init-pgvector.sql
  - id: openwiki-source-ac9587d3edbf63fff2d050a6
    resource: repo://infra/migrations/0001_unified_documents.sql
  - id: openwiki-source-fa02783b110a844143a50441
    resource: repo://infra/migrations/0016_chunks_embedding_cohere.sql
  - id: openwiki-source-6eef6c0b1f149a8267e36782
    resource: repo://infra/migrations/0025_digest_ack.sql
  - id: openwiki-source-e4d79125c80d20b76d991c6b
    resource: repo://infra/migrations/0026_digest_delivery.sql
  - id: openwiki-source-9f8bd41e893c9d3ff4d926bb
    resource: repo://infra/migrations/0027_digest_notice.sql
  - id: openwiki-source-e6819902c68e3a3c3ac96b0d
    resource: repo://scripts/migrate.py
  - id: openwiki-source-fc5957b7a515654920171e70
    resource: repo://src/teamagent/adapters/embeddings_client.py
  - id: openwiki-source-2b3301692709467d8760c20b
    resource: repo://tests/test_migrate_runner.py
generated: { by: "claude-code", at: "2026-09-29T07:51:05.076Z" }
---

# PostgreSQL / pgvector のスキーマとマイグレーション

## 前提

- 本番は RDS PostgreSQL 16 + pgvector（CLAUDE.md §1 では 0.8.2）。pgvector は 0.8.0 以上が必須（古いとフィルタ付き検索で結果が 0 件になる不具合がある。CLAUDE.md §2）。
- スキーマの正本は `infra/migrations/NNNN_*.sql`。ORM のマイグレーション（alembic）は依存に入っているが、実際の適用は `scripts/migrate.py` が行う。
- ローカルは `infra/docker/docker-compose.yml` の PostgreSQL が `init-pgvector.sql` で `vector`・`pg_trgm`・`uuid-ossp` 拡張を作る。テーブルは別途 `python scripts/migrate.py` で入れる。

## 主要テーブル

| 区分 | テーブル（作成 migration） | 内容 |
|---|---|---|
| 検索コーパス | `documents`（0001） | 資料 1 件のメタデータと ACL。`source_type`（ENUM: pdf / gdrive / gmail / slack / other、0004 で gsheets 追加）、`external_id`（`(source_type, external_id)` で UNIQUE＝冪等投入）、`owner_email`、`acl_emails[]`、`acl_groups[]`、`client_code`、`metadata` JSONB、`modified_at` |
| | `chunks`（0001） | 検索対象の本文断片。`document_id`（CASCADE）、`chunk_idx`、`content`、`contextualized`（文脈付与済みテキスト）、`embedding vector(1024)`、`page_num`、`metadata`。HNSW（cosine）索引 |
| | `chunks.embedding_cohere`（0016） | Bedrock Cohere Embed 移行用の並行列（同じ 1024 次元） |
| 取り込み | `ingest_jobs`（0005） | 一括取り込みの状態機械 |
| | `connector_state`（0012） | 増分同期の cursor（source 種別 × source ID） |
| | `ingest_source_health`・`ingest_connector_runs`（0019）、`ingest_source_retries`・`ingest_reconciliation_gaps`（0020）、lease token（0021） | ソースごとの健全性・再試行のリース（0021 で所有者＋トークンの fencing） |
| | `audit_log`（0014） | 取り込み系の監査記録（あわせて metadata の GIN 索引） |
<!-- openwiki: broken internal link [/openwiki/integrations/google-oauth-and-token-store.md] link "/openwiki/integrations/google-oauth-and-token-store.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| 連携 | `oauth_tokens`（0006） | 本人ごとの Google refresh token（[Google OAuth](/openwiki/integrations/google-oauth-and-token-store.md)） |
| | `slack_oauth_tokens`（0018） | 本人ごとの Slack user token（xoxp） |
| 利用・観測 | `usage_events`・`usage_event_calls`（0007）、`query_text` 列（0023） | 管理画面の一次データ（1 リクエスト 1 行） |
| | `runtime_metrics`（0008） | RequestGate / 接続プールの定期スナップショット |
| | `search_feedback`（0015、0022 で score など追加） | 資料検索 Web UI の評価 |
| | `video_usage`（0017） | 動画分析の利用者×月のクォータ台帳 |
<!-- openwiki: broken internal link [/openwiki/workflows/morning-digest.md] link "/openwiki/workflows/morning-digest.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| 朝ダイジェスト | `digest_ack`（0025）、`digest_delivery`（0026）、`digest_notice`（0027） | 確認済み項目、その日の送信済み印、お知らせ DM の重複防止印（[朝ダイジェスト](/openwiki/workflows/morning-digest.md)） |
<!-- openwiki: broken internal link [/openwiki/architecture/hermes-personal-memory.md] link "/openwiki/architecture/hermes-personal-memory.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
| 本人メモ | `personal_memory_profiles`・`_entries`・`_audit`（0029） | 専用ロールだけが触れる本人メモ（[本人メモ](/openwiki/architecture/hermes-personal-memory.md)） |

番号 `0028` は欠番（本人メモの計画書では「先行 draft の 0028 が入る前提で 0029 を仮置き」とされている）。0018 のファイル内見出しコメントは「0016」と書かれているが、ファイル名どおり 0018 として適用される。

## 埋め込み列の選び方

`adapters/embeddings_client.py` は `EMBEDDER_BACKEND`（既定 `local` = multilingual-e5-large）と `EMBEDDING_COLUMN`（既定 `embedding`）の組を検証する。正準ペアは `local ⇄ embedding`、`cohere ⇄ embedding_cohere`。ずれると問い合わせベクトルと保存ベクトルが別の空間になり検索が全壊するので、起動時に fail-loud で落とす。列名は SQL 識別子に埋め込むため許可リストで検査する。

HNSW のパラメータ（`m` / `ef_construction`）は pgvector 既定のままで、変更には索引の作り直しが要る（0013 の注記）。探索幅は実行時に `SEARCH_HNSW_EF_SEARCH`（MCP task env では 100）で与える。

## RLS とロール

<!-- openwiki: broken internal link [/openwiki/data/rls-and-app-role.md] link "/openwiki/data/rls-and-app-role.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
`documents` / `chunks` は RLS（FORCE）付きで、アプリは `teamagent_app` ロール（0002 で作成・NOLOGIN NOBYPASSRLS）に切り替えて session 変数を注入してから検索する。利用状況画面は `teamagent_dashboard`（0007）。usage / metrics 系の `teamagent_app` 権限は INSERT 中心に最小化されている（0009、0024 で ON CONFLICT に必要な最小 SELECT を追加）。詳細は [RLS と実行ロール](/openwiki/data/rls-and-app-role.md)。

## マイグレーションの実行（scripts/migrate.py）

- **forward-only**: rollback は持たない。down が必要なら新しい番号で forward fix する。
- `schema_migrations(version, filename, checksum_sha, applied_at)` を自分で作り（冪等）、未適用のファイルだけを番号順に適用する。
- **改竄検知**: 適用済みのファイルの内容が変わっている（SHA-256 が一致しない）と、エラーで止まる。適用済み migration は編集しない。
- 1 ファイル = 1 トランザクション。runner がトランザクションを所有するので、SQL 内に `BEGIN` / `COMMIT` / `ROLLBACK` を書くと拒否される（リポジトリ内の全 migration がこれを満たすことをテストで固定）。
- `--dry-run` は適用予定を表示するだけで DB に書き込みを残さない。`--rerun NNNN` は開発専用の強制再適用。
- 接続先は env `DATABASE_URL`。未設定なら終了コード 2。

本番 RDS へは踏み台 EC2 の SSM port-forward 経由で流す（`migrate_tunneled.sh dry` → `apply`。まず dry で確認する）。手順の詳細は `docs/v3.2/ops/local_dev_with_tunnel.md`。**本番 DB の変更は人間の承認が必要な操作**で、`0021` のように「ingest を止めて排出してから流す」前提が migration 冒頭に書かれているものもあるので、必ず先頭コメントを読む。

新しい migration の書き方で注意すること（既存 migration の教訓）:

- 最小権限ロールでは `ON CONFLICT` や `RETURNING` が SELECT 権限不足で失敗しうる（0024 の経緯）。
- admin 例外（`OR app.user_role='admin'`）を安易に写さない（本人メモ 0029 は意図的に入れていない）。
- 既存ロールの属性を ALTER できない前提で、危険な属性を検知したら migration ごと止める書き方がある（0029）。

## テスト

- `tests/test_migrate_runner.py`: 番号の一意性・昇順、チェックサム、DSN 未設定、autocommit 拒否、トランザクション制御文の拒否、dry-run が書き込みを残さないこと。
- `tests/test_rls_email_ci_migration.py`、`tests/test_hmac_migration_contract.py` など、個別 migration の契約テスト。
<!-- openwiki: broken internal link [/openwiki/testing/running-tests.md] link "/openwiki/testing/running-tests.md" is root-absolute, which no real consumer resolves against the repository root (not a coding agent reading the page, not GitHub's Markdown renderer, not a local viewer); use a path relative to this file instead. Fix the href or restore the target, then delete this comment. -->
- CI は使い捨ての PostgreSQL テストロールを用意して DB テストを流す（[テストの走らせ方と CI](/openwiki/testing/running-tests.md)）。
