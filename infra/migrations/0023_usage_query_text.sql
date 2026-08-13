-- ============================================================
-- 0023: usage_events — 質問文(query_text)の保存
-- ============================================================
-- 経緯:
--   0007 は「本文は保存しない」を原則としたが、2026-08-13 のユーザー裁定により、
--   利用状況の把握に必要な query_text のみを例外として保存する。
-- セキュリティ / プライバシー:
--   - 閲覧は既存 RLS（app.user_role='admin'）と管理ページの email allowlist
--     （小俣さん限定）の二重ゲートで保護する。
--   - teamagent_app の INSERT / teamagent_dashboard の SELECT はテーブル単位の権限なので、
--     新列にも自動で及ぶ。GRANT の追加は不要。
-- 安全性:
--   - ADD COLUMN IF NOT EXISTS で冪等。追加のみ。
-- ロールバック:
--   ALTER TABLE usage_events DROP COLUMN IF EXISTS query_text;
-- 関連: src/teamagent/runtime/usage_recorder.py, src/teamagent/connect_web/app.py

ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS query_text TEXT;

COMMENT ON COLUMN usage_events.query_text IS
    'ユーザー裁定により保存する質問文（最大2000文字）。RLS admin + 管理ページ email allowlist の二重ゲートで閲覧を制限';
COMMENT ON TABLE usage_events IS
    '管理画面の一次データ: 1リクエスト1行（利用/コスト/レイテンシ/エラー）。本文/PIIは原則保存せず、質問文(query_text)のみ2026-08-13のユーザー裁定による例外';
-- ============================================================
