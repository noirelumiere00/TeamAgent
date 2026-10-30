-- P1 video library. Additive and forward-only; no existing tables change.
-- Shared internal catalog: teamagent_app reads, only admin-GUC ingest writes.
-- is_private is publication metadata, not a company-internal access restriction.
CREATE TABLE IF NOT EXISTS vl_business_categories (
    code TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    sort_order INTEGER NOT NULL
);
INSERT INTO vl_business_categories (code, display_name, sort_order) VALUES
    ('media', 'メディア', 10), ('tto', 'TTO', 20),
    ('kirinuki', '切り抜き', 30), ('other', 'その他', 40),
    ('organic', 'オーガニック', 50)
ON CONFLICT (code) DO NOTHING;

CREATE TABLE IF NOT EXISTS vl_accounts (
    platform TEXT NOT NULL CHECK (platform IN ('tiktok', 'instagram', 'youtube')),
    handle TEXT NOT NULL,
    display_name TEXT,
    operator_label TEXT,
    classification TEXT,
    PRIMARY KEY (platform, handle)
);

CREATE TABLE IF NOT EXISTS vl_campaigns (
    advertiser TEXT NOT NULL,
    campaign TEXT NOT NULL,
    business_category TEXT REFERENCES vl_business_categories(code),
    industry TEXT,
    purpose TEXT,
    is_private BOOLEAN NOT NULL DEFAULT false,
    report_drive_file_id TEXT,
    PRIMARY KEY (advertiser, campaign)
);

CREATE TABLE IF NOT EXISTS vl_videos (
    platform TEXT NOT NULL CHECK (platform IN ('tiktok', 'instagram', 'youtube')),
    video_id TEXT NOT NULL,
    url TEXT NOT NULL,
    account_name TEXT,
    account_handle TEXT,
    posted_at TIMESTAMPTZ,
    duration_seconds DOUBLE PRECISION,
    music_name TEXT,
    music_author TEXT,
    music_original BOOLEAN,
    caption TEXT,
    hashtags TEXT[] NOT NULL DEFAULT '{}',
    advertiser TEXT,
    campaign TEXT,
    business_category TEXT REFERENCES vl_business_categories(code),
    origin TEXT NOT NULL DEFAULT 'own' CHECK (origin IN ('own', 'external')),
    is_pr BOOLEAN NOT NULL DEFAULT false,
    is_private BOOLEAN NOT NULL DEFAULT false,
    thumbnail_s3_key TEXT,
    video_s3_key TEXT,
    first_ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, video_id)
);

CREATE TABLE IF NOT EXISTS vl_metrics (
    platform TEXT NOT NULL,
    video_id TEXT NOT NULL,
    captured_on DATE NOT NULL,
    organic_plays DOUBLE PRECISION,
    organic_likes DOUBLE PRECISION,
    organic_shares DOUBLE PRECISION,
    organic_comments DOUBLE PRECISION,
    organic_saves DOUBLE PRECISION,
    total_plays DOUBLE PRECISION,
    total_likes DOUBLE PRECISION,
    total_shares DOUBLE PRECISION,
    total_comments DOUBLE PRECISION,
    total_saves DOUBLE PRECISION,
    ad_cost DOUBLE PRECISION,
    ad_impressions DOUBLE PRECISION,
    ad_clicks DOUBLE PRECISION,
    ad_ctr DOUBLE PRECISION,
    ad_views DOUBLE PRECISION,
    ad_views_2s DOUBLE PRECISION,
    ad_views_6s DOUBLE PRECISION,
    ad_views_25pct DOUBLE PRECISION,
    ad_views_50pct DOUBLE PRECISION,
    ad_views_75pct DOUBLE PRECISION,
    ad_completions DOUBLE PRECISION,
    ad_average_watch_seconds DOUBLE PRECISION,
    ad_rate_2s DOUBLE PRECISION,
    ad_rate_6s DOUBLE PRECISION,
    ad_rate_25pct DOUBLE PRECISION,
    ad_rate_50pct DOUBLE PRECISION,
    ad_rate_75pct DOUBLE PRECISION,
    ad_rate_100pct DOUBLE PRECISION,
    ad_likes DOUBLE PRECISION,
    ad_shares DOUBLE PRECISION,
    ad_comments DOUBLE PRECISION,
    ad_saves DOUBLE PRECISION,
    PRIMARY KEY (platform, video_id, captured_on),
    FOREIGN KEY (platform, video_id) REFERENCES vl_videos(platform, video_id)
);

CREATE TABLE IF NOT EXISTS vl_ingest_runs (
    request_id TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    rows_read INTEGER NOT NULL DEFAULT 0 CHECK (rows_read >= 0),
    videos_upserted INTEGER NOT NULL DEFAULT 0 CHECK (videos_upserted >= 0),
    skipped JSONB NOT NULL DEFAULT '{}',
    warnings JSONB NOT NULL DEFAULT '{}',
    error TEXT
);

ALTER TABLE vl_business_categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_business_categories FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_business_categories FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_business_categories TO teamagent_app;
DROP POLICY IF EXISTS vl_business_categories_read ON vl_business_categories;
CREATE POLICY vl_business_categories_read ON vl_business_categories FOR SELECT TO teamagent_app USING (true);
DROP POLICY IF EXISTS vl_business_categories_insert ON vl_business_categories;
CREATE POLICY vl_business_categories_insert ON vl_business_categories FOR INSERT TO teamagent_app
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS vl_business_categories_update ON vl_business_categories;
CREATE POLICY vl_business_categories_update ON vl_business_categories FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

ALTER TABLE vl_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_accounts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_accounts FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_accounts TO teamagent_app;
DROP POLICY IF EXISTS vl_accounts_read ON vl_accounts;
CREATE POLICY vl_accounts_read ON vl_accounts FOR SELECT TO teamagent_app USING (true);
DROP POLICY IF EXISTS vl_accounts_insert ON vl_accounts;
CREATE POLICY vl_accounts_insert ON vl_accounts FOR INSERT TO teamagent_app
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS vl_accounts_update ON vl_accounts;
CREATE POLICY vl_accounts_update ON vl_accounts FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

ALTER TABLE vl_campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_campaigns FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_campaigns FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_campaigns TO teamagent_app;
DROP POLICY IF EXISTS vl_campaigns_read ON vl_campaigns;
CREATE POLICY vl_campaigns_read ON vl_campaigns FOR SELECT TO teamagent_app USING (true);
DROP POLICY IF EXISTS vl_campaigns_insert ON vl_campaigns;
CREATE POLICY vl_campaigns_insert ON vl_campaigns FOR INSERT TO teamagent_app
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS vl_campaigns_update ON vl_campaigns;
CREATE POLICY vl_campaigns_update ON vl_campaigns FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

ALTER TABLE vl_videos ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_videos FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_videos FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_videos TO teamagent_app;
DROP POLICY IF EXISTS vl_videos_read ON vl_videos;
CREATE POLICY vl_videos_read ON vl_videos FOR SELECT TO teamagent_app USING (true);
DROP POLICY IF EXISTS vl_videos_insert ON vl_videos;
CREATE POLICY vl_videos_insert ON vl_videos FOR INSERT TO teamagent_app
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS vl_videos_update ON vl_videos;
CREATE POLICY vl_videos_update ON vl_videos FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

ALTER TABLE vl_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_metrics FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_metrics FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_metrics TO teamagent_app;
DROP POLICY IF EXISTS vl_metrics_read ON vl_metrics;
CREATE POLICY vl_metrics_read ON vl_metrics FOR SELECT TO teamagent_app USING (true);
DROP POLICY IF EXISTS vl_metrics_insert ON vl_metrics;
CREATE POLICY vl_metrics_insert ON vl_metrics FOR INSERT TO teamagent_app
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS vl_metrics_update ON vl_metrics;
CREATE POLICY vl_metrics_update ON vl_metrics FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

ALTER TABLE vl_ingest_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_ingest_runs FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_ingest_runs FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_ingest_runs TO teamagent_app;
DROP POLICY IF EXISTS vl_ingest_runs_read ON vl_ingest_runs;
CREATE POLICY vl_ingest_runs_read ON vl_ingest_runs FOR SELECT TO teamagent_app USING (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS vl_ingest_runs_insert ON vl_ingest_runs;
CREATE POLICY vl_ingest_runs_insert ON vl_ingest_runs FOR INSERT TO teamagent_app
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS vl_ingest_runs_update ON vl_ingest_runs;
CREATE POLICY vl_ingest_runs_update ON vl_ingest_runs FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
