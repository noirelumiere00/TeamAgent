-- 0011 §G 会社共有: 既存 documents の acl_groups に会社ドメインを backfill（社内ナレッジ横連携の有効化）
--
-- 方針(§G): 社内の営業ナレッジは「会社の資産」で横連携のため全員可視＝per-user 行隔離は不要。
-- 会社メンバー identity（user_groups=会社ドメイン）が RLS の acl_groups intersect で読めるよう、
-- 既存の slack / gsheets 取込分（従来 channel / owner スコープ）に会社ドメインを付与する。
-- GDrive shared-drive crawl は取込時に既に domain を持つため対象外で可。
-- 新規取込は pipeline._company_acl_groups()（TEAMAGENT_SHARED_COMPANY_DOMAINS）が自動付与する＝本 migration は過去分のみ。
--
-- ⚠️ 実RDS適用は P0（SSMトンネル・要承認）。適用前に <COMPANY_DOMAIN> を実値（例: vectorinc.co.jp）へ置換。
--    （代替: 該当ソースを再 ingest しても同じ結果になる。backfill は再取込なしで反映する高速手段。）
-- 冪等: 既に当該ドメインを含む行は変更しない。
--
-- 検証(適用後・P0): 会社メンバー identity（user_groups={<COMPANY_DOMAIN>}）で search/clientkarte が
--   slack 由来の営業FBを読めること / 会社ドメイン外 identity では読めないこと（2ユーザで相互確認）。

UPDATE documents
SET acl_groups = array(SELECT DISTINCT unnest(acl_groups || ARRAY['<COMPANY_DOMAIN>']))
WHERE source_type IN ('slack', 'gsheets')
  AND NOT ('<COMPANY_DOMAIN>' = ANY(acl_groups));
