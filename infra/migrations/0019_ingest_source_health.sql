-- 0019 invalid source observability / connector warning outcomes
--
-- Additive-only migration. Existing enums and tables are not changed so old readers keep
-- working during a rolling deploy. Rollback (data-loss for these observations only):
--   DROP TABLE IF EXISTS ingest_connector_runs;
--   DROP TABLE IF EXISTS ingest_source_health;

CREATE TABLE IF NOT EXISTS ingest_source_health (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type         TEXT NOT NULL,
    external_id         TEXT NOT NULL,
    md5_checksum        TEXT NOT NULL
                        CHECK (md5_checksum ~ '^[0-9a-f]{32}$'),
    size_bytes          BIGINT NOT NULL CHECK (size_bytes >= 0),
    status              TEXT NOT NULL DEFAULT 'invalid_source'
                        CHECK (status IN ('invalid_source')),
    reason              TEXT NOT NULL CHECK (reason <> ''),
    mime_type           TEXT,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    observation_count   BIGINT NOT NULL DEFAULT 1 CHECK (observation_count >= 1),
    last_request_id     TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ingest_source_health_payload_unique
        UNIQUE (source_type, external_id, md5_checksum, size_bytes)
);

CREATE INDEX IF NOT EXISTS ingest_source_health_status_seen_idx
    ON ingest_source_health (status, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS ingest_connector_runs (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id              TEXT NOT NULL,
    source_kind             TEXT NOT NULL,
    source_id               TEXT NOT NULL,
    outcome                 TEXT NOT NULL
                            CHECK (outcome IN ('success', 'success_with_warnings', 'failed')),
    documents_upserted      BIGINT NOT NULL DEFAULT 0 CHECK (documents_upserted >= 0),
    chunks_inserted         BIGINT NOT NULL DEFAULT 0 CHECK (chunks_inserted >= 0),
    warning_count           BIGINT NOT NULL DEFAULT 0 CHECK (warning_count >= 0),
    warning_reasons         JSONB NOT NULL DEFAULT '{}'::jsonb,
    suppressed_retry_count  BIGINT NOT NULL DEFAULT 0 CHECK (suppressed_retry_count >= 0),
    last_error              TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ingest_connector_runs_request_source_unique
        UNIQUE (request_id, source_kind, source_id)
);

CREATE INDEX IF NOT EXISTS ingest_connector_runs_outcome_completed_idx
    ON ingest_connector_runs (outcome, completed_at DESC);

-- Operational fingerprints/source IDs are available only on an admin-GUC connection.
-- IngestRepository._ops_connection() always sets app.user_role='admin'.
ALTER TABLE ingest_source_health ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingest_source_health FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ingest_source_health_admin ON ingest_source_health;
CREATE POLICY ingest_source_health_admin ON ingest_source_health
    FOR ALL
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

ALTER TABLE ingest_connector_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingest_connector_runs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ingest_connector_runs_admin ON ingest_connector_runs;
CREATE POLICY ingest_connector_runs_admin ON ingest_connector_runs
    FOR ALL
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

GRANT SELECT, INSERT, UPDATE ON ingest_source_health TO teamagent_app;
GRANT SELECT, INSERT, UPDATE ON ingest_connector_runs TO teamagent_app;

COMMENT ON TABLE ingest_source_health IS
    'Immutable source payload fingerprint observations; invalid Office never replaces documents/chunks.';
COMMENT ON TABLE ingest_connector_runs IS
    'Per-source ingest result including success_with_warnings without changing legacy connector_state readers.';
