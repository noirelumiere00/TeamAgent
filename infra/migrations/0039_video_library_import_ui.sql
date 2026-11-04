-- URL intake selections survive worker/browser restarts; existing views keep their columns.
ALTER TABLE vl_url_import_batches
    ADD COLUMN advertiser TEXT,
    ADD COLUMN campaign TEXT,
    ADD COLUMN parent_batch_id UUID REFERENCES vl_url_import_batches(batch_id),
    ADD CONSTRAINT vl_url_import_campaign FOREIGN KEY (advertiser, campaign)
        REFERENCES vl_campaigns(advertiser, campaign),
    ADD CONSTRAINT vl_url_import_campaign_pair CHECK (
        (advertiser IS NULL) = (campaign IS NULL));

-- Employees may add a pair, but cannot edit existing pairs or forge the creator.
CREATE POLICY vl_campaigns_url_import_insert ON vl_campaigns FOR INSERT TO teamagent_app
    WITH CHECK (created_by = current_setting('app.user_email', true)
        AND created_via = 'url_import' AND created_at IS NOT NULL);
