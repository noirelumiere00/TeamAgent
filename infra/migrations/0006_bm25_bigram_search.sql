-- ============================================================
-- 0006: pg_bigm によるバイグラム語彙検索 (BM25 ハイブリッドの語彙側)
-- ============================================================
-- Sprint 5: dense (pgvector cosine) に語彙検索を RRF で融合し、
-- 固有名詞リコールを底上げする (gold set 25 の miss 主因 = deal_phase 取り違え /
-- 集約クエリ / 固有名詞希薄)。Anthropic ベンチで hybrid は dense 比 +22%。
--
-- トークナイザ選定の経緯 (2026-05-29 実機確認):
--   - textsearch_ja (MeCab): RDS allowlist 外・日本語 ts_config 無 → 却下
--   - to_tsvector('simple'): 日本語をスペース境界でしか切れず 1 トークンに潰れる → 不可
--   - pg_bigm 1.2: RDS で利用可。2-gram 索引で語分割不要、日本語固有名詞
--     (「日本ガイシ」「マンダム」) の部分一致リコールに最適 → 採用
--   - pg_trgm: 3-gram。2 文字主体の和名には bigram が有利 → 不採用
--
-- スコアリング: pg_bigm の bigm_similarity(content, query) を語彙ランカーとして使い、
-- RRF (Reciprocal Rank Fusion, k=60) で dense ランキングと順位融合する。
-- 真の BM25 スコア (tf-idf) ではないが、RRF は順位のみ使うため融合品質に影響しない。
--
-- 適用対象: chunks.content (生チャンク本文)。contextualized は現状未使用 (USE_CONTEXTUAL=false)
-- のため索引対象外。将来 Contextual Retrieval を有効化する際は別 migration で追加する。
-- ============================================================

-- pg_bigm 拡張。rds_superuser メンバーが CREATE 可能 (RDS allowlist 内)。
CREATE EXTENSION IF NOT EXISTS pg_bigm;

-- chunks.content へのバイグラム GIN 索引。
-- gin_bigm_ops で LIKE '%...%' / bigm_similarity / =% 演算子が索引活用される。
-- CONCURRENTLY は付けない (migration はトランザクション内・初回構築・dev 規模で十分高速)。
CREATE INDEX IF NOT EXISTS chunks_content_bigm_idx
    ON chunks USING gin (content gin_bigm_ops);

COMMENT ON INDEX chunks_content_bigm_idx IS
    'Sprint 5 BM25 ハイブリッド: pg_bigm バイグラム索引。dense 検索と RRF 融合する語彙側ランカー (bigm_similarity)';
