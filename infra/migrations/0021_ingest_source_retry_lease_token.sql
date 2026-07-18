-- Opaque per-claim fencing token for ingest_source_retries.
--
-- scripts/migrate.py owns the surrounding transaction. The scheduler/manual ingest paths must be
-- paused and drained before this migration: legacy active leases have no token and are released
-- below so the new application can reclaim them with an exact owner/token fence.

ALTER TABLE ingest_source_retries
    ADD COLUMN IF NOT EXISTS lease_token TEXT;

-- Upgrade only legacy/inconsistent leases. Re-running this idempotent file does not disturb a
-- valid tokenized lease.
UPDATE ingest_source_retries
SET lease_owner = NULL,
    lease_token = NULL,
    lease_expires_at = NULL
WHERE NOT (
    (
        lease_owner IS NULL
        AND lease_token IS NULL
        AND lease_expires_at IS NULL
    )
    OR (
        status = 'pending'
        AND lease_owner IS NOT NULL
        AND lease_token IS NOT NULL
        AND lease_expires_at IS NOT NULL
    )
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'ingest_source_retries'::regclass
          AND conname = 'ingest_source_retries_lease_token_nonempty'
    ) THEN
        ALTER TABLE ingest_source_retries
            ADD CONSTRAINT ingest_source_retries_lease_token_nonempty
            CHECK (lease_token IS NULL OR lease_token <> '') NOT VALID;
        ALTER TABLE ingest_source_retries
            VALIDATE CONSTRAINT ingest_source_retries_lease_token_nonempty;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'ingest_source_retries'::regclass
          AND conname = 'ingest_source_retries_lease_fields_consistent'
    ) THEN
        ALTER TABLE ingest_source_retries
            ADD CONSTRAINT ingest_source_retries_lease_fields_consistent
            CHECK (
                (
                    lease_owner IS NULL
                    AND lease_token IS NULL
                    AND lease_expires_at IS NULL
                )
                OR (
                    status = 'pending'
                    AND lease_owner IS NOT NULL
                    AND lease_token IS NOT NULL
                    AND lease_expires_at IS NOT NULL
                )
            ) NOT VALID;
        ALTER TABLE ingest_source_retries
            VALIDATE CONSTRAINT ingest_source_retries_lease_fields_consistent;
    END IF;
END
$$;

-- Reassert the complete table privilege contract in the same forward migration. REVOKE ALL also
-- removes grant options and any privilege outside the narrow writer set before it is restored.
REVOKE ALL PRIVILEGES ON ingest_source_health FROM teamagent_app;
REVOKE ALL PRIVILEGES ON ingest_connector_runs FROM teamagent_app;
REVOKE ALL PRIVILEGES ON ingest_source_retries FROM teamagent_app;
REVOKE ALL PRIVILEGES ON ingest_reconciliation_gaps FROM teamagent_app;
GRANT SELECT, INSERT, UPDATE ON ingest_source_health TO teamagent_app;
GRANT SELECT, INSERT, UPDATE ON ingest_connector_runs TO teamagent_app;
GRANT SELECT, INSERT, UPDATE ON ingest_source_retries TO teamagent_app;
GRANT SELECT, INSERT, UPDATE ON ingest_reconciliation_gaps TO teamagent_app;
