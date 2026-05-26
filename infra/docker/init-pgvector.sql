-- ============================================================
-- TeamAgent v3.0 — pgvector 初期化 SQL
-- docker-compose で PostgreSQL 起動時に自動実行される
-- ============================================================

-- pgvector 拡張を有効化
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- 部分一致検索用
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";   -- UUID 生成

-- 動作確認用：1024 次元のベクトルで簡易テーブル
CREATE TABLE IF NOT EXISTS smoke_test (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    text        TEXT,
    embedding   vector(1024),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- HNSW インデックス（高速類似度検索）
CREATE INDEX IF NOT EXISTS smoke_test_embedding_idx
    ON smoke_test USING hnsw (embedding vector_cosine_ops);

-- 確認: SELECT * FROM pg_extension WHERE extname='vector';

-- ============================================================
-- Sprint 3 / PR-1: 統合 documents + chunks スキーマ + RLS
-- ============================================================
-- 実体は infra/migrations/0001_unified_documents.sql。
-- docker 起動時は `python scripts/migrate.py` を別途実行して取り込む。
-- 本番 RDS は踏み台 EC2 上で同コマンドを SSM tunnel 経由で実行。
--
-- ローカル一発セットアップ:
--   docker compose up -d
--   DATABASE_URL=postgresql://teamagent:teamagent@localhost:5432/teamagent \
--       python scripts/migrate.py
