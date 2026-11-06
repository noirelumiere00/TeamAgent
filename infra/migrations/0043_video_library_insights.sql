-- Additive/forward-only; scripts/migrate.py owns the transaction.
CREATE TABLE IF NOT EXISTS vl_video_insights (
    platform TEXT NOT NULL,
    video_id TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    views INTEGER CHECK (views >= 0),
    likes INTEGER CHECK (likes >= 0),
    comments INTEGER CHECK (comments >= 0),
    shares INTEGER CHECK (shares >= 0),
    saves INTEGER CHECK (saves >= 0),
    reach INTEGER CHECK (reach >= 0),
    completion_rate NUMERIC CHECK (completion_rate BETWEEN 0 AND 1),
    avg_watch_seconds NUMERIC CHECK (avg_watch_seconds >= 0),
    profile_views INTEGER CHECK (profile_views >= 0),
    male_ratio NUMERIC CHECK (male_ratio BETWEEN 0 AND 1),
    female_ratio NUMERIC CHECK (female_ratio BETWEEN 0 AND 1),
    age_bands JSONB NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(age_bands) = 'array'),
    retention INTEGER[] NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'analytics_sheet',
    PRIMARY KEY (platform, video_id, captured_at),
    FOREIGN KEY (platform, video_id) REFERENCES vl_videos(platform, video_id)
);

ALTER TABLE vl_video_insights ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_video_insights FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_video_insights FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_video_insights TO teamagent_app;
DROP POLICY IF EXISTS vl_video_insights_read ON vl_video_insights;
CREATE POLICY vl_video_insights_read ON vl_video_insights
    FOR SELECT TO teamagent_app USING (true);
DROP POLICY IF EXISTS vl_video_insights_insert ON vl_video_insights;
CREATE POLICY vl_video_insights_insert ON vl_video_insights
    FOR INSERT TO teamagent_app
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS vl_video_insights_update ON vl_video_insights;
CREATE POLICY vl_video_insights_update ON vl_video_insights
    FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
