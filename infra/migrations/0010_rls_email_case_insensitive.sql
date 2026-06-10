-- 0010_rls_email_case_insensitive.sql
-- WS-C.2: documents の RLS email/group 比較を「両側 lower()」に統一する。
--
-- 背景（赤チーム指摘）:
--   WS-C で identity.build_rls_metadata が GUC(app.user_email / app.user_groups) を
--   strip+lower 正規化するようになった。一方 documents.owner_email / acl_emails / acl_groups は
--   ingest 時の表記そのまま（混在し得る）。0001 の RLS は生文字 case-sensitive 比較なので、
--   GUC(lower) と DB(混在) が一致せず「本人なのに自分の行が見えない」可用性破壊が起き得る。
--   両側 lower() で「大小無視の一致」を保証する。oauth_tokens は lower 保存済＋GUC lower で既に整合。
--
-- 安全性: 冪等（DROP POLICY IF EXISTS → CREATE）。0001 の documents_user_acl / documents_owner_insert を
--         lower() 版で置換するだけで、可視範囲の意図は不変（admin/owner/acl_emails/acl_groups）。
-- ⚠️ 実DB適用と RLS 動的検証（2ユーザで他人行0・本人行可視）は P0（SSMトンネル・要承認）で行う。

-- SELECT: 自分/ACL/グループに一致した行のみ可視（admin は全件）。email/group を両側 lower() で比較。
DROP POLICY IF EXISTS documents_user_acl ON documents;
CREATE POLICY documents_user_acl ON documents
    FOR SELECT
    USING (
        current_setting('app.user_role', true) = 'admin'
        OR lower(current_setting('app.user_email', true)) = lower(owner_email)
        OR lower(current_setting('app.user_email', true)) = ANY (
            SELECT lower(e) FROM unnest(acl_emails) AS e
        )
        OR (
            current_setting('app.user_groups', true) IS NOT NULL
            AND current_setting('app.user_groups', true) <> ''
            AND EXISTS (
                SELECT 1 FROM unnest(acl_groups) AS g
                WHERE lower(g) = ANY (
                    string_to_array(lower(current_setting('app.user_groups', true)), ',')
                )
            )
        )
    );

-- INSERT: owner_email = 自分（または admin）。同じく両側 lower()。
DROP POLICY IF EXISTS documents_owner_insert ON documents;
CREATE POLICY documents_owner_insert ON documents
    FOR INSERT
    WITH CHECK (
        current_setting('app.user_role', true) = 'admin'
        OR lower(current_setting('app.user_email', true)) = lower(owner_email)
    );

-- 注: chunks_via_document(SELECT) は documents への EXISTS 経由で RLS を継承するため email を直接
--     比較せず、本 migration の対象外。proposals_chunks(旧スキーマ・RLS 未適用)も対象外（別途解消）。
