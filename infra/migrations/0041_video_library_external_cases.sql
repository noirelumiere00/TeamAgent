-- Shared origin decisions are append-only audit history; sheet/account ownership wins.
ALTER TABLE vl_url_import_batches ADD COLUMN requested_origin TEXT NOT NULL DEFAULT 'external'
    CHECK (requested_origin IN ('own','external'));
CREATE TABLE vl_video_origin_decisions (
    decision_id UUID PRIMARY KEY,
    platform TEXT NOT NULL,
    video_id TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('own','external')),
    decided_by TEXT NOT NULL CHECK (length(btrim(decided_by))>0),
    decided_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (platform,video_id) REFERENCES vl_videos(platform,video_id)
);
CREATE INDEX vl_video_origin_latest ON vl_video_origin_decisions
    (platform,video_id,decided_at DESC,decision_id DESC);
ALTER TABLE vl_video_origin_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE vl_video_origin_decisions FORCE ROW LEVEL SECURITY;
REVOKE ALL ON vl_video_origin_decisions FROM PUBLIC;
GRANT SELECT,INSERT ON vl_video_origin_decisions TO teamagent_app;
CREATE POLICY vl_video_origin_read ON vl_video_origin_decisions
    FOR SELECT TO teamagent_app USING (true);
CREATE POLICY vl_video_origin_insert ON vl_video_origin_decisions
    FOR INSERT TO teamagent_app WITH CHECK (
        decided_by=current_setting('app.user_email',true)
        OR current_setting('app.user_role',true)='admin');

-- One ownership predicate for the view and employee advertiser-write policies.
CREATE FUNCTION vl_video_is_own(p_platform TEXT,p_video_id TEXT) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY INVOKER AS $$
    SELECT 'sheet'=ANY(v.ingest_sources) OR EXISTS (
        SELECT 1 FROM vl_accounts ac WHERE ac.platform=v.platform
        AND lower(ltrim(ac.handle,'@'))=lower(ltrim(v.account_handle,'@'))
        AND ac.operator_label IN ('NewsTV','ベクトル（VCL）','ベクトル九州','イニシャル','gracemode')
    ) OR COALESCE((
        SELECT d.origin='own' FROM vl_video_origin_decisions d
        WHERE d.platform=v.platform AND d.video_id=v.video_id
        ORDER BY d.decided_at DESC,d.decision_id DESC LIMIT 1
    ),false)
    FROM vl_videos v WHERE v.platform=p_platform AND v.video_id=p_video_id
$$;
REVOKE ALL ON FUNCTION vl_video_is_own(TEXT,TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION vl_video_is_own(TEXT,TEXT) TO teamagent_app;

-- Employees can correct only the external advertiser, recording their own identity.
CREATE POLICY vl_external_advertiser_insert ON vl_video_campaign_links
    FOR INSERT TO teamagent_app WITH CHECK (
        source='human' AND prompt_version='external-v1' AND campaign IS NULL
        AND decided_by=current_setting('app.user_email',true)
        AND NOT vl_video_is_own(platform,video_id));
CREATE POLICY vl_external_advertiser_update ON vl_video_campaign_links
    FOR UPDATE TO teamagent_app USING (source='human' AND NOT vl_video_is_own(platform,video_id))
    WITH CHECK (source='human' AND prompt_version='external-v1' AND campaign IS NULL
        AND decided_by=current_setting('app.user_email',true)
        AND NOT vl_video_is_own(platform,video_id));

-- Preserve every existing view column and append the external fields.
CREATE OR REPLACE VIEW vl_videos_effective WITH (security_invoker = true) AS
SELECT v.platform, v.video_id, v.url, v.account_name, v.account_handle, v.posted_at,
    v.duration_seconds, v.music_name, v.music_author, v.music_original,
    v.caption, v.hashtags, e.advertiser, e.campaign, o.origin, v.is_pr,
    v.thumbnail_s3_key, v.video_s3_key, v.first_ingested_at, v.updated_at,
    CASE WHEN NOT v.is_pr THEN 'organic'
         ELSE COALESCE(cs.business_category, sheet_cs.business_category, l.source_business_category)
         END AS business_category,
    (COALESCE(cs.is_private, false) OR COALESCE(sheet_cs.is_private, false)
        OR COALESCE(l.source_is_private, false)) AS is_private,
    COALESCE(cs.industry, sheet_cs.industry, l.source_industry) AS industry,
    COALESCE(cs.purpose, sheet_cs.purpose, l.source_purpose) AS purpose,
    (o.origin='own' AND l.source = 'ai' AND
        ((NULLIF(v.advertiser, '') IS NULL AND e.advertiser IS NOT NULL) OR
         (NULLIF(v.campaign, '') IS NULL AND e.campaign IS NOT NULL))) IS TRUE AS case_inferred,
    CASE WHEN o.origin='external' THEN e.advertiser END AS external_advertiser,
    (o.origin='external' AND (v.is_pr OR COALESCE(v.page_is_ad,false) OR EXISTS (
        SELECT 1 FROM vl_metrics m WHERE m.platform=v.platform AND m.video_id=v.video_id
        AND (m.ad_cost>0 OR m.ad_impressions>0 OR m.ad_views>0)))) AS is_external_case
FROM vl_videos v
CROSS JOIN LATERAL (
    SELECT CASE WHEN vl_video_is_own(v.platform,v.video_id) THEN 'own' ELSE 'external' END AS origin
) o
LEFT JOIN vl_case_aliases a ON a.from_advertiser=v.advertiser AND a.from_campaign=v.campaign
CROSS JOIN LATERAL (
    SELECT COALESCE(a.to_advertiser, NULLIF(v.advertiser, '')) AS advertiser,
           COALESCE(a.to_campaign, NULLIF(v.campaign, '')) AS campaign
) s
LEFT JOIN LATERAL (
    SELECT d.source, resolved.advertiser, resolved.campaign,
           dcs.business_category AS source_business_category, dcs.is_private AS source_is_private,
           dcs.industry AS source_industry, dcs.purpose AS source_purpose
    FROM vl_video_campaign_links d
    LEFT JOIN vl_campaign_settings dcs
        ON dcs.advertiser=d.advertiser AND dcs.campaign=d.campaign
    LEFT JOIN vl_case_aliases da
        ON da.from_advertiser=d.advertiser AND da.from_campaign=d.campaign
    CROSS JOIN LATERAL (
        SELECT COALESCE(da.to_advertiser, d.advertiser) AS advertiser,
               COALESCE(da.to_campaign, d.campaign) AS campaign
    ) resolved
    WHERE d.platform=v.platform AND d.video_id=v.video_id AND d.status='applied'
      AND ((o.origin='external') = (d.prompt_version='external-v1'))
      AND (o.origin='external' OR s.advertiser IS NULL OR s.advertiser=resolved.advertiser)
      AND (o.origin='external' OR s.campaign IS NULL OR s.campaign=resolved.campaign)
      -- A human rejection/unknown also blocks fallback to an older AI decision.
      AND (d.source='human' OR NOT EXISTS (
          SELECT 1 FROM vl_video_campaign_links h
          WHERE h.platform=v.platform AND h.video_id=v.video_id AND h.source='human'
            AND ((o.origin='external') = (h.prompt_version='external-v1'))))
      AND (resolved.campaign IS NULL OR EXISTS (
          SELECT 1 FROM vl_campaigns c LEFT JOIN vl_case_aliases ca
            ON ca.from_advertiser=c.advertiser AND ca.from_campaign=c.campaign
          WHERE COALESCE(ca.to_advertiser,c.advertiser)=resolved.advertiser
            AND COALESCE(ca.to_campaign,c.campaign)=resolved.campaign))
    ORDER BY (d.source='human') DESC LIMIT 1
) l ON true
CROSS JOIN LATERAL (
    SELECT CASE WHEN o.origin='external' AND EXISTS (
               SELECT 1 FROM vl_video_campaign_links h WHERE h.platform=v.platform
               AND h.video_id=v.video_id AND h.source='human' AND h.prompt_version='external-v1'
           ) THEN l.advertiser
           WHEN o.origin='external' THEN COALESCE(l.advertiser,s.advertiser)
           ELSE COALESCE(s.advertiser, l.advertiser) END AS advertiser,
           COALESCE(s.campaign, l.campaign) AS campaign
) e
LEFT JOIN vl_campaign_settings cs ON cs.advertiser=e.advertiser AND cs.campaign=e.campaign
LEFT JOIN vl_campaign_settings sheet_cs
    ON sheet_cs.advertiser=v.advertiser AND sheet_cs.campaign=v.campaign;
REVOKE ALL ON vl_videos_effective FROM PUBLIC;
GRANT SELECT ON vl_videos_effective TO teamagent_app;
