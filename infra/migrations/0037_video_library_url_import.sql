-- URL import PR1. Additive/forward-only; scripts/migrate.py owns the transaction.
ALTER TABLE vl_videos
    ADD COLUMN IF NOT EXISTS page_is_ad BOOLEAN,
    ADD COLUMN IF NOT EXISTS page_ad_label_version JSONB,
    ADD COLUMN IF NOT EXISTS page_disclosure JSONB,
    ADD COLUMN IF NOT EXISTS page_observed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS page_fetch_status TEXT,
    ADD COLUMN IF NOT EXISTS page_last_attempted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS page_last_success_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS page_fetch_error_code TEXT,
    ADD COLUMN IF NOT EXISTS ingest_sources TEXT[] NOT NULL DEFAULT '{sheet}';
ALTER TABLE vl_campaigns
    ADD COLUMN IF NOT EXISTS created_by TEXT,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS created_via TEXT;

-- Preserve both routes when the unchanged sheet writer promotes a URL video to own.
CREATE OR REPLACE FUNCTION vl_video_sheet_source() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.origin = 'own' AND NOT ('sheet' = ANY(NEW.ingest_sources)) THEN
        NEW.ingest_sources := array_append(NEW.ingest_sources, 'sheet');
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER vl_video_sheet_source BEFORE INSERT OR UPDATE ON vl_videos
    FOR EACH ROW EXECUTE FUNCTION vl_video_sheet_source();

CREATE TABLE IF NOT EXISTS vl_url_import_batches (
    batch_id UUID PRIMARY KEY,
    requested_by TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('url_manual', 'p3b')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (requested_by, idempotency_key)
);
CREATE TABLE IF NOT EXISTS vl_url_import_rows (
    batch_id UUID NOT NULL REFERENCES vl_url_import_batches(batch_id),
    line_no INTEGER NOT NULL CHECK (line_no > 0),
    input_url TEXT NOT NULL,
    canonical_url TEXT,
    video_id TEXT,
    duplicate_of INTEGER,
    status TEXT NOT NULL CHECK (status IN
        ('queued','processing','succeeded','partial','failed','invalid','unsupported',
         'duplicate','deferred','classification_pending')),
    registration_result TEXT,
    error_code TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    observation JSONB,
    eligible_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    lease_token UUID,
    PRIMARY KEY (batch_id, line_no),
    FOREIGN KEY (batch_id, duplicate_of) REFERENCES vl_url_import_rows(batch_id, line_no)
);
CREATE INDEX IF NOT EXISTS vl_url_import_queue ON vl_url_import_rows(status, eligible_at);
CREATE INDEX IF NOT EXISTS vl_url_import_video ON vl_url_import_rows(batch_id, video_id);
CREATE TABLE IF NOT EXISTS vl_public_fetch_control (
    platform TEXT PRIMARY KEY CHECK (platform = 'tiktok'),
    paused BOOLEAN NOT NULL DEFAULT false,
    pause_reason TEXT,
    last_request_at TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0)
);
INSERT INTO vl_public_fetch_control(platform) VALUES ('tiktok') ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS vl_public_fetch_daily_usage (
    platform TEXT NOT NULL CHECK (platform = 'tiktok'),
    quota_on DATE NOT NULL,
    used_count INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, quota_on)
);
CREATE TABLE IF NOT EXISTS vl_public_metrics (
    platform TEXT NOT NULL,
    video_id TEXT NOT NULL,
    captured_on DATE NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    total_plays BIGINT CHECK (total_plays >= 0),
    total_likes BIGINT CHECK (total_likes >= 0),
    total_comments BIGINT CHECK (total_comments >= 0),
    total_shares BIGINT CHECK (total_shares >= 0),
    total_saves BIGINT CHECK (total_saves >= 0),
    metric_observation JSONB NOT NULL,
    PRIMARY KEY (platform, video_id, captured_on),
    FOREIGN KEY (platform, video_id) REFERENCES vl_videos(platform, video_id)
);
CREATE TABLE IF NOT EXISTS vl_video_analysis_requests (
    platform TEXT NOT NULL,
    video_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'awaiting_approval' CHECK (status = 'awaiting_approval'),
    requested_by TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('url_manual', 'p3b')),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, video_id, kind, version),
    FOREIGN KEY (platform, video_id) REFERENCES vl_videos(platform, video_id)
);

-- The existing view is intentionally untouched: its pre-0037 column contract is stable.

ALTER TABLE vl_url_import_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_url_import_batches FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_url_import_batches FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_url_import_batches TO teamagent_app;
CREATE POLICY vl_url_import_batches_read ON vl_url_import_batches FOR SELECT TO teamagent_app USING (true);
CREATE POLICY vl_url_import_batches_insert ON vl_url_import_batches FOR INSERT TO teamagent_app WITH CHECK (true);
CREATE POLICY vl_url_import_batches_update ON vl_url_import_batches FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

ALTER TABLE vl_url_import_rows ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_url_import_rows FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_url_import_rows FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_url_import_rows TO teamagent_app;
CREATE POLICY vl_url_import_rows_read ON vl_url_import_rows FOR SELECT TO teamagent_app USING (true);
CREATE POLICY vl_url_import_rows_insert ON vl_url_import_rows FOR INSERT TO teamagent_app WITH CHECK (true);
CREATE POLICY vl_url_import_rows_update ON vl_url_import_rows FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

ALTER TABLE vl_public_fetch_daily_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_public_fetch_daily_usage FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_public_fetch_daily_usage FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_public_fetch_daily_usage TO teamagent_app;
CREATE POLICY vl_public_fetch_daily_usage_read ON vl_public_fetch_daily_usage FOR SELECT TO teamagent_app USING (true);
CREATE POLICY vl_public_fetch_daily_usage_insert ON vl_public_fetch_daily_usage FOR INSERT TO teamagent_app WITH CHECK (true);
CREATE POLICY vl_public_fetch_daily_usage_update ON vl_public_fetch_daily_usage FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

ALTER TABLE vl_public_fetch_control ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_public_fetch_control FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_public_fetch_control FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_public_fetch_control TO teamagent_app;
CREATE POLICY vl_public_fetch_control_read ON vl_public_fetch_control FOR SELECT TO teamagent_app USING (true);
CREATE POLICY vl_public_fetch_control_insert ON vl_public_fetch_control FOR INSERT TO teamagent_app WITH CHECK (current_setting('app.user_role', true) = 'admin');
CREATE POLICY vl_public_fetch_control_update ON vl_public_fetch_control FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

ALTER TABLE vl_public_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_public_metrics FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_public_metrics FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_public_metrics TO teamagent_app;
CREATE POLICY vl_public_metrics_read ON vl_public_metrics FOR SELECT TO teamagent_app USING (true);
CREATE POLICY vl_public_metrics_insert ON vl_public_metrics FOR INSERT TO teamagent_app WITH CHECK (current_setting('app.user_role', true) = 'admin');
CREATE POLICY vl_public_metrics_update ON vl_public_metrics FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

ALTER TABLE vl_video_analysis_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_video_analysis_requests FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_video_analysis_requests FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_video_analysis_requests TO teamagent_app;
CREATE POLICY vl_video_analysis_requests_read ON vl_video_analysis_requests FOR SELECT TO teamagent_app USING (true);
CREATE POLICY vl_video_analysis_requests_insert ON vl_video_analysis_requests FOR INSERT TO teamagent_app WITH CHECK (current_setting('app.user_role', true) = 'admin');
CREATE POLICY vl_video_analysis_requests_update ON vl_video_analysis_requests FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
