-- 0020 upgrade path for databases that already applied the original 0019 checksum.
--
-- This migration is intentionally idempotent. Existing 0019 fingerprints are assigned the
-- legacy validator generation, so the current validator re-checks them instead of suppressing.
-- Rollback (new retry/reconciliation data loss only):
--   DROP TABLE IF EXISTS ingest_reconciliation_gaps;
--   DROP TABLE IF EXISTS ingest_source_retries;
-- The fingerprint columns/constraint should remain on rollback because dropping them would
-- collapse distinct validator observations and cannot be losslessly reversed.

ALTER TABLE ingest_source_health
    ADD COLUMN IF NOT EXISTS validator_schema_version TEXT;

UPDATE ingest_source_health
SET validator_schema_version = 'ooxml-legacy-v1'
WHERE validator_schema_version IS NULL OR validator_schema_version = '';

UPDATE ingest_source_health
SET mime_type = 'application/octet-stream'
WHERE mime_type IS NULL OR mime_type = '';

ALTER TABLE ingest_source_health
    ALTER COLUMN mime_type SET NOT NULL,
    ALTER COLUMN validator_schema_version SET NOT NULL;

ALTER TABLE ingest_source_health
    DROP CONSTRAINT IF EXISTS ingest_source_health_payload_unique,
    DROP CONSTRAINT IF EXISTS ingest_source_health_fingerprint_unique;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'ingest_source_health'::regclass
          AND conname = 'ingest_source_health_mime_type_nonempty'
    ) THEN
        ALTER TABLE ingest_source_health
            ADD CONSTRAINT ingest_source_health_mime_type_nonempty
            CHECK (mime_type <> '') NOT VALID;
        ALTER TABLE ingest_source_health
            VALIDATE CONSTRAINT ingest_source_health_mime_type_nonempty;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'ingest_source_health'::regclass
          AND conname = 'ingest_source_health_validator_schema_nonempty'
    ) THEN
        ALTER TABLE ingest_source_health
            ADD CONSTRAINT ingest_source_health_validator_schema_nonempty
            CHECK (validator_schema_version <> '') NOT VALID;
        ALTER TABLE ingest_source_health
            VALIDATE CONSTRAINT ingest_source_health_validator_schema_nonempty;
    END IF;
END
$$;

ALTER TABLE ingest_source_health
    ADD CONSTRAINT ingest_source_health_fingerprint_unique
    UNIQUE (
        source_type, external_id, md5_checksum, size_bytes,
        mime_type, validator_schema_version
    );

CREATE TABLE IF NOT EXISTS ingest_source_retries (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_kind               TEXT NOT NULL,
    source_id                 TEXT NOT NULL,
    source_type               TEXT NOT NULL,
    external_id               TEXT NOT NULL,
    md5_checksum              TEXT
                              CHECK (
                                  md5_checksum IS NULL
                                  OR md5_checksum ~ '^[0-9a-f]{32}$'
                              ),
    size_bytes                BIGINT CHECK (size_bytes IS NULL OR size_bytes >= 0),
    mime_type                 TEXT NOT NULL CHECK (mime_type <> ''),
    validator_schema_version  TEXT NOT NULL CHECK (validator_schema_version <> ''),
    status                    TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'resolved')),
    reason                    TEXT NOT NULL CHECK (reason <> ''),
    attempt_count             BIGINT NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
    next_attempt_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_failed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_failed_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at               TIMESTAMPTZ,
    last_request_id           TEXT,
    lease_owner               TEXT,
    lease_expires_at          TIMESTAMPTZ,
    metadata                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ingest_source_retries_source_unique
        UNIQUE (source_kind, source_id, source_type, external_id)
);

CREATE INDEX IF NOT EXISTS ingest_source_retries_due_idx
    ON ingest_source_retries (source_kind, source_id, next_attempt_at)
    WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS ingest_reconciliation_gaps (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gap_key             TEXT NOT NULL UNIQUE,
    source_kind         TEXT NOT NULL,
    gap_kind            TEXT NOT NULL
                        CHECK (gap_kind IN ('unindexed_pdf', 'source_original_missing')),
    source_ref_hashes   TEXT[] NOT NULL CHECK (cardinality(source_ref_hashes) > 0),
    status              TEXT NOT NULL DEFAULT 'unresolved'
                        CHECK (status IN ('unresolved', 'resolved')),
    first_observed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_observed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ,
    last_request_id     TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS ingest_reconciliation_gaps_status_kind_idx
    ON ingest_reconciliation_gaps (source_kind, status, gap_kind);

ALTER TABLE ingest_source_retries ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingest_source_retries FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ingest_source_retries_admin ON ingest_source_retries;
CREATE POLICY ingest_source_retries_admin ON ingest_source_retries
    FOR ALL
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

ALTER TABLE ingest_reconciliation_gaps ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingest_reconciliation_gaps FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ingest_reconciliation_gaps_admin ON ingest_reconciliation_gaps;
CREATE POLICY ingest_reconciliation_gaps_admin ON ingest_reconciliation_gaps
    FOR ALL
    USING (current_setting('app.user_role', true) = 'admin')
    WITH CHECK (current_setting('app.user_role', true) = 'admin');

REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON ingest_source_health FROM teamagent_app;
REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON ingest_connector_runs FROM teamagent_app;
REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON ingest_source_retries FROM teamagent_app;
REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON ingest_reconciliation_gaps FROM teamagent_app;
GRANT SELECT, INSERT, UPDATE ON ingest_source_health TO teamagent_app;
GRANT SELECT, INSERT, UPDATE ON ingest_connector_runs TO teamagent_app;
GRANT SELECT, INSERT, UPDATE ON ingest_source_retries TO teamagent_app;
GRANT SELECT, INSERT, UPDATE ON ingest_reconciliation_gaps TO teamagent_app;

INSERT INTO ingest_reconciliation_gaps
    (gap_key, source_kind, gap_kind, source_ref_hashes, metadata)
VALUES
    (
        'audit-20260717-unindexed-pdf-01', 'gdrive', 'unindexed_pdf',
        ARRAY[
            'd3551c5a7b30e1f5ed983967c801b2063a70f0ea37b086448705a4f884e954d7',
            '0d52ef9f9545e9121c3b21c3152e9997c579c5e55b64ea8aec9dbea396ae26ec'
        ],
        '{"audit_date":"2026-07-17"}'::jsonb
    ),
    (
        'audit-20260717-unindexed-pdf-02', 'gdrive', 'unindexed_pdf',
        ARRAY[
            'fee54112a0e60e52b6b8dee1da5e160658c1c1f23c8abf525046767eadcfa509',
            '7d16f1f380363705309fa98d2e528ba9db5f3b6d55595bfebd3ea4100fe2e770'
        ],
        '{"audit_date":"2026-07-17"}'::jsonb
    ),
    (
        'audit-20260717-unindexed-pdf-03', 'gdrive', 'unindexed_pdf',
        ARRAY[
            '6a491abdc1d8007310582b2902be09e80f573eeef091482ceca59c0e65aba818',
            '298f38395ace936cc5208d994d8e648214422c38c0ec72c99b386a33d96db694'
        ],
        '{"audit_date":"2026-07-17"}'::jsonb
    ),
    (
        'audit-20260717-source-original-missing-01', 'gdrive',
        'source_original_missing',
        ARRAY['9273564bba4184af6c7dcbda9a84c2008319b1666f1063ed2c39efd8f365c489'],
        '{"audit_date":"2026-07-17"}'::jsonb
    ),
    (
        'audit-20260717-source-original-missing-02', 'gdrive',
        'source_original_missing',
        ARRAY['e20a5400c3b350e138d3a4f6c05e7044b101a829976b952b02b7e0a4d2edfd43'],
        '{"audit_date":"2026-07-17"}'::jsonb
    ),
    (
        'audit-20260717-source-original-missing-03', 'gdrive',
        'source_original_missing',
        ARRAY[
            '4dc2e344a4858c36a580f3486cb25694e11e153430de1714871e609bf3ae5eb7',
            '4802cd9aa352c926cf52f5820791938ea9ec159861d323109451030ce92df23d'
        ],
        '{"audit_date":"2026-07-17"}'::jsonb
    ),
    (
        'audit-20260717-source-original-missing-04', 'gdrive',
        'source_original_missing',
        ARRAY['9a3b99876270579d5b2e239f233ada88740944f9adf69bfe612a28e248169e29'],
        '{"audit_date":"2026-07-17"}'::jsonb
    ),
    (
        'audit-20260717-source-original-missing-05', 'gdrive',
        'source_original_missing',
        ARRAY['41fb01bde2b5d358b96ec0d00ef19a03dd371cc8a09b08888a9ded438ba15499'],
        '{"audit_date":"2026-07-17"}'::jsonb
    ),
    (
        'audit-20260717-source-original-missing-06', 'gdrive',
        'source_original_missing',
        ARRAY[
            '8f9fdfbfbd5464d506009c3c5770ea2646fae9cb3bb064e4319235af2e8af726',
            '5d0b1de0cde88aca43866d2e48811443b59c155884febb78c0f8ce887f2e9492'
        ],
        '{"audit_date":"2026-07-17"}'::jsonb
    ),
    (
        'audit-20260717-source-original-missing-07', 'gdrive',
        'source_original_missing',
        ARRAY[
            '7b54ff314c4327ebfcce25e63b27549a476174ad06b107e5a33750bb45abdefe',
            '5872b9d707d306ae3bc90d5700ff4bfd4921f9d52aaeddb9065993a839778eb2'
        ],
        '{"audit_date":"2026-07-17"}'::jsonb
    ),
    (
        'audit-20260717-source-original-missing-08', 'gdrive',
        'source_original_missing',
        ARRAY[
            '9d5f443632c55b0ca98e3efd23a4ab0c5a29caabf2dd0763869f5fde34f0a0b2',
            '2f7bdb6f76f4e59f4c9a9d5e34705d2ba1239d9906a81a16217d0799767cf509'
        ],
        '{"audit_date":"2026-07-17"}'::jsonb
    ),
    (
        'audit-20260717-source-original-missing-09', 'gdrive',
        'source_original_missing',
        ARRAY['9aa3c866331c389e217e5dc90ae2748ec6f095775de0bf3adc749941e904466e'],
        '{"audit_date":"2026-07-17"}'::jsonb
    )
ON CONFLICT (gap_key) DO NOTHING;
