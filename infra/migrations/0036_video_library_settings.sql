-- P3 human-managed settings. Additive, forward-only; ingest never writes these values.
CREATE TABLE IF NOT EXISTS vl_campaign_settings (
    advertiser TEXT NOT NULL,
    campaign TEXT NOT NULL,
    business_category TEXT REFERENCES vl_business_categories(code),
    industry TEXT,
    purpose TEXT,
    is_private BOOLEAN NOT NULL DEFAULT false,
    updated_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (advertiser, campaign)
);
CREATE TABLE IF NOT EXISTS vl_account_settings (
    platform TEXT NOT NULL CHECK (platform IN ('tiktok', 'instagram', 'youtube')),
    handle TEXT NOT NULL,
    display_name TEXT,
    note TEXT,
    fetch_enabled BOOLEAN NOT NULL DEFAULT false,
    registered_by TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, handle)
);
CREATE TABLE IF NOT EXISTS vl_video_analysis (
    platform TEXT NOT NULL,
    video_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    version TEXT NOT NULL,
    payload JSONB NOT NULL,
    analyzed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, video_id, kind, version),
    FOREIGN KEY (platform, video_id) REFERENCES vl_videos(platform, video_id)
);

CREATE OR REPLACE VIEW vl_videos_effective WITH (security_invoker = true) AS
SELECT v.*,
    CASE WHEN NOT v.is_pr THEN 'organic' ELSE cs.business_category END AS business_category,
    COALESCE(cs.is_private, false) AS is_private,
    cs.industry, cs.purpose
FROM vl_videos v
LEFT JOIN vl_campaign_settings cs ON cs.advertiser = v.advertiser AND cs.campaign = v.campaign;
REVOKE ALL ON vl_videos_effective FROM PUBLIC;
GRANT SELECT ON vl_videos_effective TO teamagent_app;

ALTER TABLE vl_campaign_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_campaign_settings FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_campaign_settings FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_campaign_settings TO teamagent_app;
DROP POLICY IF EXISTS vl_campaign_settings_read ON vl_campaign_settings;
CREATE POLICY vl_campaign_settings_read ON vl_campaign_settings FOR SELECT TO teamagent_app USING (true);
DROP POLICY IF EXISTS vl_campaign_settings_insert ON vl_campaign_settings;
CREATE POLICY vl_campaign_settings_insert ON vl_campaign_settings FOR INSERT TO teamagent_app
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS vl_campaign_settings_update ON vl_campaign_settings;
CREATE POLICY vl_campaign_settings_update ON vl_campaign_settings FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

ALTER TABLE vl_account_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_account_settings FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_account_settings FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_account_settings TO teamagent_app;
DROP POLICY IF EXISTS vl_account_settings_read ON vl_account_settings;
CREATE POLICY vl_account_settings_read ON vl_account_settings FOR SELECT TO teamagent_app USING (true);
DROP POLICY IF EXISTS vl_account_settings_insert ON vl_account_settings;
CREATE POLICY vl_account_settings_insert ON vl_account_settings FOR INSERT TO teamagent_app
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS vl_account_settings_update ON vl_account_settings;
CREATE POLICY vl_account_settings_update ON vl_account_settings FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

ALTER TABLE vl_video_analysis ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_video_analysis FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_video_analysis FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_video_analysis TO teamagent_app;
DROP POLICY IF EXISTS vl_video_analysis_read ON vl_video_analysis;
CREATE POLICY vl_video_analysis_read ON vl_video_analysis FOR SELECT TO teamagent_app USING (true);
DROP POLICY IF EXISTS vl_video_analysis_insert ON vl_video_analysis;
CREATE POLICY vl_video_analysis_insert ON vl_video_analysis FOR INSERT TO teamagent_app
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS vl_video_analysis_update ON vl_video_analysis;
CREATE POLICY vl_video_analysis_update ON vl_video_analysis FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
