-- ============================================================
-- 0031: digest_delivery に「予約済み（reserved）」の印を足す
-- ============================================================
-- 目的: 2026-10-01 小俣さん裁定「朝ダイジェストの一律 9:30 を撤廃し、その日の最初の予定の
-- 30 分前に送る（上限なし）」。最初の予定が 14:00 の人には 13:30 に届くため、9:30 の
-- 一括実行がその人を **送らずに見送る** 必要がある。
--
-- 仕組み: planner（04:00）が個別に送る人の行を origin='reserved' で先に作る。
--   - 一括実行（bulk）の INSERT ... ON CONFLICT DO NOTHING はこの行に当たって 0 行＝送らない
--   - 予約の発火（scheduled）は UPDATE で 'reserved' → 'scheduled' に変えて送る（1 行なら送る）
--   - 予約（Scheduler）が作れなかったら planner が行を消す（片方だけ残さない）
-- 判定は今までどおり DB の一意制約（user_email, digest_date）に委ねる。
--
-- ロールバック:
--   REVOKE UPDATE ON digest_delivery FROM teamagent_app;
--   ALTER TABLE digest_delivery DROP CONSTRAINT IF EXISTS digest_delivery_origin_check;
--   DELETE FROM digest_delivery WHERE origin = 'reserved';
--   ALTER TABLE digest_delivery ADD CONSTRAINT digest_delivery_origin_check
--     CHECK (origin IN ('scheduled', 'bulk'));
-- 関連: infra/migrations/0026_digest_delivery.sql
-- ============================================================

-- 0026 の列内 CHECK は PostgreSQL の既定名 digest_delivery_origin_check で作られている。
ALTER TABLE digest_delivery DROP CONSTRAINT IF EXISTS digest_delivery_origin_check;
ALTER TABLE digest_delivery ADD CONSTRAINT digest_delivery_origin_check
    CHECK (origin IN ('scheduled', 'bulk', 'reserved'));

-- 予約の発火が 'reserved' → 'scheduled' に書き換えるための UPDATE 権限（RLS で本人行だけ）。
GRANT UPDATE ON digest_delivery TO teamagent_app;
