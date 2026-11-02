-- Independent decisions survive sheet sync. Keep human decisions alongside AI results.
CREATE TABLE IF NOT EXISTS vl_video_campaign_links (
    platform TEXT NOT NULL,
    video_id TEXT NOT NULL,
    advertiser TEXT CHECK (advertiser IS NULL OR length(btrim(advertiser)) > 0),
    campaign TEXT CHECK (campaign IS NULL OR length(btrim(campaign)) > 0),
    source TEXT NOT NULL CHECK (source IN ('ai', 'human')),
    status TEXT NOT NULL CHECK (status IN ('applied', 'candidate', 'unknown', 'rejected')),
    match_type TEXT NOT NULL CHECK (match_type IN ('existing', 'new', 'unknown')),
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence JSONB NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(evidence) = 'array'),
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform, video_id, source),
    FOREIGN KEY (platform, video_id) REFERENCES vl_videos(platform, video_id),
    CHECK (campaign IS NULL OR advertiser IS NOT NULL),
    CHECK (status <> 'applied' OR (advertiser IS NOT NULL AND match_type <> 'new'))
);
ALTER TABLE vl_video_campaign_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_video_campaign_links FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_video_campaign_links FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON vl_video_campaign_links TO teamagent_app;
CREATE POLICY vl_video_campaign_links_read ON vl_video_campaign_links
    FOR SELECT TO teamagent_app USING (true);
CREATE POLICY vl_video_campaign_links_insert ON vl_video_campaign_links
    FOR INSERT TO teamagent_app WITH CHECK (current_setting('app.user_role', true) = 'admin');
CREATE POLICY vl_video_campaign_links_update ON vl_video_campaign_links
    FOR UPDATE TO teamagent_app USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

-- Explicit pre-0037 columns: CREATE OR REPLACE may append, never reorder existing columns.
CREATE OR REPLACE VIEW vl_videos_effective WITH (security_invoker = true) AS
SELECT v.platform, v.video_id, v.url, v.account_name, v.account_handle, v.posted_at,
    v.duration_seconds, v.music_name, v.music_author, v.music_original,
    v.caption, v.hashtags, e.advertiser, e.campaign, v.origin, v.is_pr,
    v.thumbnail_s3_key, v.video_s3_key, v.first_ingested_at, v.updated_at,
    CASE WHEN NOT v.is_pr THEN 'organic' ELSE cs.business_category END AS business_category,
    COALESCE(cs.is_private, false) AS is_private, cs.industry, cs.purpose,
    (l.source = 'ai' AND
        ((NULLIF(v.advertiser, '') IS NULL AND e.advertiser IS NOT NULL) OR
         (NULLIF(v.campaign, '') IS NULL AND e.campaign IS NOT NULL))) IS TRUE AS case_inferred
FROM vl_videos v
LEFT JOIN LATERAL (
    SELECT d.* FROM vl_video_campaign_links d
    WHERE d.platform=v.platform AND d.video_id=v.video_id AND d.status='applied'
      AND (NULLIF(v.advertiser, '') IS NULL OR v.advertiser=d.advertiser)
      AND (NULLIF(v.campaign, '') IS NULL OR v.campaign=d.campaign)
      -- A human rejection/unknown also blocks fallback to an older AI decision.
      AND (d.source='human' OR NOT EXISTS (
          SELECT 1 FROM vl_video_campaign_links h
          WHERE h.platform=v.platform AND h.video_id=v.video_id AND h.source='human'))
      AND (d.campaign IS NULL OR EXISTS (
          SELECT 1 FROM vl_campaigns c WHERE c.advertiser=d.advertiser AND c.campaign=d.campaign))
    ORDER BY (d.source='human') DESC LIMIT 1
) l ON true
CROSS JOIN LATERAL (
    SELECT COALESCE(NULLIF(v.advertiser, ''), l.advertiser) AS advertiser,
           COALESCE(NULLIF(v.campaign, ''), l.campaign) AS campaign
) e
LEFT JOIN vl_campaign_settings cs ON cs.advertiser=e.advertiser AND cs.campaign=e.campaign;
REVOKE ALL ON vl_videos_effective FROM PUBLIC;
GRANT SELECT ON vl_videos_effective TO teamagent_app;
