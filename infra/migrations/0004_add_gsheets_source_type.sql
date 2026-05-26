-- ============================================================
-- 0004: document_source_type ENUM に 'gsheets' を追加
-- ============================================================
-- Sprint 3 PR-6 で GSheets ingest を実装したが、ENUM に 'gsheets' が
-- 無かったため `source_type="other"` で暫定保存していた。
--
-- Day 6 (2026-05-26) で本実装フェーズに入るため、正規の 'gsheets' を
-- ENUM に追加し、pipeline.py 側も source_type="gsheets" に切替える。
--
-- Postgres の ENUM は ALTER TYPE ... ADD VALUE で既存値を破壊せず追加可能。
-- IF NOT EXISTS で idempotent（再実行可能）にしておく。
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_enum
        WHERE enumtypid = 'document_source_type'::regtype
          AND enumlabel = 'gsheets'
    ) THEN
        ALTER TYPE document_source_type ADD VALUE 'gsheets';
    END IF;
END
$$;

COMMENT ON TYPE document_source_type IS
    'TeamAgent v3.2: source 横断 ENUM (pdf / gdrive / gmail / slack / gsheets / other). migration 0004 で gsheets 追加';
