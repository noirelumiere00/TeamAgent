-- Forward-only employee account registration; legacy attribution stays unknown.
ALTER TABLE vl_accounts ADD COLUMN IF NOT EXISTS added_by TEXT NULL;
ALTER TABLE vl_accounts ADD COLUMN IF NOT EXISTS added_at TIMESTAMPTZ NULL;
ALTER TABLE vl_accounts ADD COLUMN IF NOT EXISTS last_fetched_on DATE NULL;

DROP POLICY IF EXISTS vl_accounts_insert ON vl_accounts;
CREATE POLICY vl_accounts_insert ON vl_accounts FOR INSERT TO teamagent_app
    WITH CHECK (
        current_setting('app.user_role', true) = 'admin'
        OR (
            added_by = current_setting('app.user_email', true)
            AND length(btrim(added_by)) > 0
            AND operator_label IN (
                'NewsTV', 'ベクトル（VCL）', 'ベクトル九州', 'イニシャル', 'gracemode'
            )
        )
    );
