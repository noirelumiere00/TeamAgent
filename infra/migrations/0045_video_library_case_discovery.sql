-- Additive/forward-only; scripts/migrate.py owns the transaction.
CREATE TABLE IF NOT EXISTS vl_case_discovery (
    tag TEXT PRIMARY KEY,
    video_count INT NOT NULL CHECK (video_count >= 0),
    sample_video_ids TEXT[] NOT NULL DEFAULT '{}',
    samples JSONB NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(samples) = 'array'),
    status TEXT NOT NULL CHECK (status IN
        ('sampled','registered','existing','candidate','unidentifiable','failed')),
    advertiser TEXT,
    campaign TEXT,
    confidence NUMERIC,
    evidence JSONB,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at TIMESTAMPTZ
);

ALTER TABLE vl_case_discovery ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_case_discovery FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_case_discovery FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_case_discovery TO teamagent_app;
DROP POLICY IF EXISTS vl_case_discovery_read ON vl_case_discovery;
CREATE POLICY vl_case_discovery_read ON vl_case_discovery
    FOR SELECT TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS vl_case_discovery_insert ON vl_case_discovery;
CREATE POLICY vl_case_discovery_insert ON vl_case_discovery
    FOR INSERT TO teamagent_app
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS vl_case_discovery_update ON vl_case_discovery;
CREATE POLICY vl_case_discovery_update ON vl_case_discovery
    FOR UPDATE TO teamagent_app
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');
