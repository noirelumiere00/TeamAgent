-- ============================================================
-- 0028: case_records テーブル — Vault 文書から抽出した事例レコード v1
-- ============================================================
-- 目的: Vault（documents/chunks）に文章として埋まっている施策事例へ「構造」
-- （業種・目的・商品状態・チャネル・特性・結果・出典）を付け、類似事例・競合事例を
-- 機械で引けるようにする（設計: case_knowledge_design_20260916.md v2）。
-- 元文書は消さない。レコードは sources[] で元 document（external_id / URL）を参照する。
--
-- 列の要点:
--   - tag 列（sector / purpose / product_state / channel / traits）は青木 事例DB の語彙
--     （src/teamagent/cases/schema.py が正本・語彙外は 'その他'）。
--   - metrics は本文の数値文字列そのまま＋出典 URL（LLM に計算させない・provenance 規則）。
--   - embedding は要約（product＋result_masked＋winpattern）の 1024 次元ベクトル。
--     chunks.embedding / embedding_cohere と同じ次元（0001 / 0016）。どの embedder で
--     作ったかを embedding_backend（local / cohere）に持ち、検索は同じ backend の行だけ比べる。
--   - reviewed は人の確認（external_use / client_masked の確定）。未確認は draft として
--     社外向け検索の既定から外す。
--
-- セキュリティ / プライバシー:
--   - client_internal（実社名）は社内参照用。社外向け出力は client_masked のみ（裁定 §6-2:
--     社内は実社名可・社外向けは masked）。
--   - RLS は付けない（事例は全社共有のナレッジ。documents の ACL とは独立。
--     裁定「金庫は全員閲覧OK」2026-08-03 と同じ扱い）。teamagent_app に表単位で GRANT。
--
-- 安全性: IF NOT EXISTS で冪等。追加のみ（既存テーブル・データ・索引は不変）。
--
-- ロールバック:
--   REVOKE SELECT, INSERT, UPDATE, DELETE ON case_records FROM teamagent_app;
--   DROP INDEX IF EXISTS case_records_embedding_hnsw_idx;
--   DROP INDEX IF EXISTS case_records_traits_gin;
--   DROP INDEX IF EXISTS case_records_purpose_gin;
--   DROP INDEX IF EXISTS case_records_group_idx;
--   DROP INDEX IF EXISTS case_records_sector_idx;
--   DROP TABLE IF EXISTS case_records;
-- 関連:
--   src/teamagent/cases/schema.py（CaseRecord v1）
--   src/teamagent/cases/store.py（upsert_case_records / search_case_records）
--   scripts/extract_cases.py（一括抽出・--dry-run 既定で書かない）
-- ============================================================

CREATE TABLE IF NOT EXISTS case_records (
    -- 抽出元 document の external_id ＋ '#' ＋ 連番
    case_id           TEXT PRIMARY KEY,
    -- 同じ案件を複数文書から束ねる鍵（社名（正規化）|商材|期間）
    case_group        TEXT NOT NULL,
    client_internal   TEXT NOT NULL DEFAULT '',
    client_masked     TEXT NOT NULL,
    sector            TEXT NOT NULL,
    purpose           JSONB NOT NULL DEFAULT '[]'::jsonb,
    product_state     TEXT NOT NULL,
    channel           JSONB NOT NULL DEFAULT '[]'::jsonb,
    traits            JSONB NOT NULL DEFAULT '[]'::jsonb,
    product           TEXT NOT NULL DEFAULT '',
    scale             TEXT NOT NULL DEFAULT '',
    period            JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- [{name, value(文字列そのまま), unit, source_url}]
    metrics           JSONB NOT NULL DEFAULT '[]'::jsonb,
    result_masked     TEXT NOT NULL DEFAULT '',
    winpattern        TEXT NOT NULL DEFAULT '',
    similar_keys      JSONB NOT NULL DEFAULT '[]'::jsonb,
    competitors       JSONB NOT NULL DEFAULT '[]'::jsonb,
    external_use      TEXT NOT NULL DEFAULT 'unknown'
                      CHECK (external_use IN ('ok', 'ng', 'unknown')),
    -- [{external_id, url, excerpt}]
    sources           JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence        DOUBLE PRECISION NOT NULL DEFAULT 0
                      CHECK (confidence >= 0 AND confidence <= 1),
    reviewed          BOOLEAN NOT NULL DEFAULT false,
    embedding         vector(1024),
    embedding_backend TEXT CHECK (embedding_backend IN ('local', 'cohere')),
    schema_version    SMALLINT NOT NULL DEFAULT 1,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 構造一致（sector / product_state）と案件束ね
CREATE INDEX IF NOT EXISTS case_records_sector_idx ON case_records (sector, product_state);
CREATE INDEX IF NOT EXISTS case_records_group_idx ON case_records (case_group);
-- jsonb の ?| 演算子（既定 jsonb_ops。jsonb_path_ops は ?| に効かない）
CREATE INDEX IF NOT EXISTS case_records_purpose_gin ON case_records USING gin (purpose);
CREATE INDEX IF NOT EXISTS case_records_traits_gin ON case_records USING gin (traits);
-- HNSW: cosine 類似（chunks_embedding_hnsw_idx と同型）
CREATE INDEX IF NOT EXISTS case_records_embedding_hnsw_idx
    ON case_records USING hnsw (embedding vector_cosine_ops);

-- ON CONFLICT ... DO UPDATE は arbiter 列の SELECT 権限を要求する（0024/0025 で実測済み）。
GRANT SELECT, INSERT, UPDATE, DELETE ON case_records TO teamagent_app;

COMMENT ON TABLE case_records IS
    'TeamAgent: Vault 文書から抽出した事例レコード v1（migration 0028・設計 case_knowledge_design v2）';
COMMENT ON COLUMN case_records.metrics IS
    '本文の数値文字列そのまま＋出典 URL。LLM に計算させない（provenance 規則）';
COMMENT ON COLUMN case_records.reviewed IS
    '人の確認済み（external_use / client_masked を確定）。未確認は社外向け検索の既定から外す';
