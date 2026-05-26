-- ============================================================
-- 0003: chunks への INSERT / UPDATE / DELETE 用 RLS policy
-- ============================================================
-- Sprint 3 / PR-6 本投入時に発覚した不具合の修正。
--
-- 課題：
--   migration 0001 で `chunks` テーブルに RLS を有効化したが、policy が
--   `FOR SELECT` のみだったため、INSERT/UPDATE/DELETE がすべて
--   "new row violates row-level security policy" で拒否されていた。
--
-- 修正：
--   admin role bypass + 親 documents 経由の SELECT 可能チェックを
--   全 DML に対して追加。documents も ON CONFLICT 経由の UPDATE 用 policy を追加。
-- ============================================================

-- ============================================================
-- chunks: INSERT / UPDATE / DELETE policy
-- ============================================================
DROP POLICY IF EXISTS chunks_insert_via_document ON chunks;
CREATE POLICY chunks_insert_via_document ON chunks
    FOR INSERT
    WITH CHECK (
        -- admin role なら全て通す（ingest パイプライン用）
        current_setting('app.user_role', true) = 'admin'
        -- それ以外: 親 documents が見える（RLS で SELECT 可能）なら子 chunks 操作 OK
        OR EXISTS (SELECT 1 FROM documents d WHERE d.id = chunks.document_id)
    );

DROP POLICY IF EXISTS chunks_update_via_document ON chunks;
CREATE POLICY chunks_update_via_document ON chunks
    FOR UPDATE
    USING (
        current_setting('app.user_role', true) = 'admin'
        OR EXISTS (SELECT 1 FROM documents d WHERE d.id = chunks.document_id)
    )
    WITH CHECK (
        current_setting('app.user_role', true) = 'admin'
        OR EXISTS (SELECT 1 FROM documents d WHERE d.id = chunks.document_id)
    );

DROP POLICY IF EXISTS chunks_delete_via_document ON chunks;
CREATE POLICY chunks_delete_via_document ON chunks
    FOR DELETE
    USING (
        current_setting('app.user_role', true) = 'admin'
        OR EXISTS (SELECT 1 FROM documents d WHERE d.id = chunks.document_id)
    );

-- ============================================================
-- documents: ON CONFLICT 経由の UPDATE / DELETE policy も追加
-- ============================================================
-- migration 0001 では INSERT 用 policy だけだったが、ON CONFLICT DO UPDATE で
-- 既存 row 更新時に UPDATE policy が要求されるので追加。
DROP POLICY IF EXISTS documents_owner_update ON documents;
CREATE POLICY documents_owner_update ON documents
    FOR UPDATE
    USING (
        current_setting('app.user_role', true) = 'admin'
        OR current_setting('app.user_email', true) = owner_email
        OR current_setting('app.user_email', true) = ANY(acl_emails)
    )
    WITH CHECK (
        current_setting('app.user_role', true) = 'admin'
        OR current_setting('app.user_email', true) = owner_email
    );

DROP POLICY IF EXISTS documents_owner_delete ON documents;
CREATE POLICY documents_owner_delete ON documents
    FOR DELETE
    USING (
        current_setting('app.user_role', true) = 'admin'
        OR current_setting('app.user_email', true) = owner_email
    );

-- ============================================================
-- 検証用コメント
-- ============================================================
COMMENT ON POLICY chunks_insert_via_document ON chunks IS
    'admin bypass + 親 documents が見えれば INSERT OK (Sprint 3 / migration 0003)';
COMMENT ON POLICY documents_owner_update ON documents IS
    'ON CONFLICT DO UPDATE 経由の更新を admin と owner に許可 (Sprint 3 / migration 0003)';
