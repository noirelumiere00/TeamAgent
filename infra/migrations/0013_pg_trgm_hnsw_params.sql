-- 0013 pg_trgm 有効化 + 部分一致トリグラム索引 / HNSW パラメータ方針（spec matrix 検索#22）
--
-- 目的:
--   (1) pg_trgm を有効化し、固有名詞・ファイル名の **部分一致**（ILIKE '%x%' / 類似度 %）を索引化。
--       現状の検索は dense+Cohere Rerank が主だが、固有名詞リコールの補助に trigram を使える土台を作る。
--   (2) HNSW パラメータの方針を明記（下記コメント）。
--
-- 安全性: 全て IF NOT EXISTS で冪等。追加のみ（既存索引・データは不変）。
-- ロールバック: DROP INDEX IF EXISTS documents_title_trgm_idx; DROP EXTENSION IF EXISTS pg_trgm;
--
-- ⚠️ HNSW パラメータ（m / ef_construction）について:
--   既存 chunks_embedding_hnsw_idx（0001）は pgvector 既定パラメータ。明示チューニング
--   （m=16 / ef_construction=64 等）への変更は **既存索引の DROP + 再 CREATE が必要**で、
--   本番 chunks 規模では再構築コスト/ロックが発生する。よって本 migration では**実施せず**、
--   保守窓での CONCURRENTLY 再構築タスクとして decision_register.md に委ねる（既定値で実用上問題なし）。

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- documents.title の部分一致（ファイル名/タイトルの固有名詞ヒット補助）。
-- chunks.content への trigram 索引は規模が大きくコスト高・かつ現状クエリ未使用のため、
-- 必要になった時点（BM25/部分一致を検索経路に組み込む時・検索#9）に別 migration で追加する。
CREATE INDEX IF NOT EXISTS documents_title_trgm_idx
    ON documents USING gin (title gin_trgm_ops);

-- 適用後の検証 (P0・SSMトンネル):
--   SELECT extname FROM pg_extension WHERE extname='pg_trgm';        -- 1行
--   SELECT indexname FROM pg_indexes WHERE indexname='documents_title_trgm_idx';  -- 1行
--   EXPLAIN SELECT * FROM documents WHERE title ILIKE '%伊藤園%';     -- trigram 索引が使われ得る
