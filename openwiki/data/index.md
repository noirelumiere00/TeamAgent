# ファイル

- [PostgreSQL / pgvector のスキーマとマイグレーション](postgres-schema-and-migrations.md) - RDS PostgreSQL 16 + pgvector に置かれる主要テーブル（documents/chunks と埋め込み列・ingest 状態・OAuth token・usage・朝ダイジェストの状態・本人メモ など）と、scripts/migrate.py による forward-only・チェックサム付きマイグレーションの運用。
- [RLS と実行ロール](rls-and-app-role.md) - アプリが master 接続から teamagent_app ロールへ SET ROLE し、app.user_email / app.user_groups / app.user_role を transaction-local に注入して documents・chunks・本人ごとの表の RLS を効かせる仕組み。email の大文字小文字を無視した比較、会社共有グループ、返却時に RESET ROLE する pg_pool、CI の使い捨てテストロールも扱う。
