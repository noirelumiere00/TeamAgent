# Invalid Office source observability

TeamAgent validates Drive `docx` / `pptx` / `xlsx` payloads before any document or chunk
upsert. Invalid input is skipped without replacing an existing document, deleting chunks, or
making an observed Drive file stale.

## Validation contract

The known-invalid fingerprint is:

`(source_type, Drive file ID, md5Checksum, size, MIME type, validator schema version)`

The current validator version is defined by
`OFFICE_VALIDATOR_SCHEMA_VERSION` in `office_extract.py`. A Drive MD5, size, or MIME correction,
or a validator version change, therefore forces a fresh download and validation.
Incremental connectors also store that version in `connector_state.metadata`. On the first run
after a generation change (including an older row with no version), the connector deliberately
does a full listing/revalidation and records a new Drive cursor only after it completes.

Validation is bounded before extraction:

- the Drive download is capped at 256 MiB in `GDriveClient.download_file_bytes`; this applies to
  folder, recursive folder, shared-drive crawl, and sales-filter-off paths;
- compressed Office input is capped at 256 MiB;
- ZIP member count, each member's uncompressed size, total uncompressed size, and compression
  ratio are capped;
- required OOXML XML parts have separate read/parse limits;
- DTDs and internal/external entity declarations are rejected before ElementTree or an Office
  library parses the XML;
- each bounded ZIP member is read to EOF for size and CRC verification; unbounded
  `ZipFile.testzip()` is not used;
- the required package part, OOXML root element, and `[Content_Types].xml` override must match the
  advertised MIME type;
- encrypted compound Office and encrypted ZIP members are classified as `encrypted_office`.
- extracted Office text is capped at 2,000,000 characters per file;
- the shared PDF/Office/native pipeline rejects more than 2,000,000 extracted characters before
  chunk construction;
- every XML/relationship member is secure-preparsed; each is capped at 32 MiB and aggregate XML
  at 128 MiB, while the required main part and content-types part retain tighter limits;
- every PDF, Office, and native-text path is capped at 2,000 chunks and 2,000 embedding calls per
  file. The chunk builder stops while constructing the list rather than after allocating it.

Deterministic `corrupt_zip`, `format_mismatch`, `unsafe_archive`, `unsafe_content_volume`, and
`encrypted_office` outcomes are persisted in `ingest_source_health`. Transient download, size,
checksum, extraction, and empty text failures are not made permanent.

## Durable retry and connector outcomes

`ingest_source_retries` carries transient failures across incremental cursor advancement. A row
stores the connector scope, Drive ID, complete fingerprint, reason, attempt count, next attempt,
and a bounded lease. Claims use `FOR UPDATE SKIP LOCKED`; the same request/fingerprint is
idempotent, later failures use exponential backoff, and a changed fingerprint resets the attempt
sequence. A successful ingest or permanent invalid result marks the retry `resolved`; rows are not
deleted.

An exact permanent fingerprint is observed again without downloading its body. A changed
fingerprint is revalidated. The incremental cursor can continue past a transiently failed file
without losing it or forcing all healthy files to repeat.

Cursor advancement is conditional on durable retry state. A missing retry API, claim failure,
`record_source_retry()` returning false/raising, lease loss, or connector-state save failure makes
the connector run fail and leaves the old cursor in place. A claimed row renews its lease during
bounded ZIP reads, Office text traversal, chunk embedding, classification/contextualization
boundaries, and immediately before upsert. Renewal verifies both request ownership and pending
status; loss stops that file before an upsert.

Drive changes and file listings reject repeated pagination tokens and a still-present token at the
safety page limit. Incomplete changes pagination falls back to a full listing. An incomplete full
listing fails the source, so neither cursor advancement nor stale marking can use a partial set.
Recursive folder/shared-drive walk saturation likewise fails the source and disables stale
marking for the affected root.

PDF download, extraction, and empty-text failures join the same warning collector. Connector runs
are recorded as `success`, `success_with_warnings`, or `failed`, with reason counts suitable for an
ops notification. Notifications contain only source kind, aggregate reason counts, suppression
count, and request ID—not file names, customer names, raw Drive IDs, or content.

The reconciliation baseline keeps the independently identified three unindexed PDF gaps and nine
missing-original gaps unresolved in `ingest_reconciliation_gaps`. Only non-reversible Drive-ID
hashes are stored. A verified successful content ingest of a matching source resolves its gap;
normal no-change runs remain `success` only when no connector warning or unresolved baseline gap
exists.

The CLI exit-code contract is:

- `0`: clean success;
- `1`: at least one ingest error;
- `2`: completed with warnings, or an execution-precondition/configuration error.

Schedulers must treat both `1` and `2` as non-clean outcomes. Console and connector-run output
contains aggregate reason counts only; notification payloads never include document content,
title, customer name, raw Drive ID, or per-document metadata.

All operational tables use forced RLS with the admin GUC and grant `SELECT`, `INSERT`, and
`UPDATE` only. `DELETE`, `TRUNCATE`, `REFERENCES`, and `TRIGGER` are explicitly revoked from
`teamagent_app`, including when broad default privileges exist.

## Rollout order

Run database migrations through `0020_ingest_source_retry_upgrade.sql` before rolling out or
restarting ingest workers:

1. Pause new scheduled worker starts and let an in-flight run finish.
2. Apply migrations through 0020.
3. Verify the four operational tables, forced RLS policies, and minimal privileges.
4. Roll out and restart workers with the new validator.
5. Confirm the first run reports expected `success_with_warnings` counts and drains due retries.

0020 replaces the legacy unique constraint and therefore needs a normal migration maintenance
window; inspect `ingest_source_health` row count and lock activity first. The first worker run after
the validator bump intentionally does a full Drive listing/revalidation, so stagger connectors
instead of starting every folder simultaneously.

Migration 0019 is byte-for-byte the originally released migration and must never be edited.
Migration 0020 is the forward-only upgrade: it labels old observations `ooxml-legacy-v1`, fills
legacy empty MIME values, installs the full fingerprint constraint, retry/reconciliation tables,
forced RLS, and privilege revokes. Existing databases that already applied 0019 and fresh
databases that apply original 0019 followed by 0020 therefore converge on the same schema.
Reapplying 0020 is supported and must be part of local PostgreSQL verification.

Application code treats a missing new table as unavailable and automatically probes it again after
60 seconds, so availability is neither cached false forever nor queried once per file while a
migration is absent. Exact fingerprint lookup uses the six-column unique index. Migration-first
plus worker restart is still the supported rollout contract and avoids an old worker temporarily
using the legacy fingerprint semantics.

## Monitoring and cleanup

Run aggregate checks with an operator/admin connection. Do not export raw `external_id`,
`source_id`, titles, or metadata into dashboards:

```sql
SELECT outcome, count(*)
FROM ingest_connector_runs
WHERE completed_at >= now() - interval '24 hours'
GROUP BY outcome;

SELECT reason, count(*)
FROM ingest_source_retries
WHERE status = 'pending'
GROUP BY reason;

SELECT
    count(*) FILTER (WHERE next_attempt_at <= now()) AS due,
    min(next_attempt_at) AS oldest_due,
    max(attempt_count) AS max_attempts,
    count(*) FILTER (
        WHERE lease_expires_at < now() AND lease_owner IS NOT NULL
    ) AS expired_leases
FROM ingest_source_retries
WHERE status = 'pending';

SELECT gap_kind, count(*)
FROM ingest_reconciliation_gaps
WHERE status = 'unresolved'
GROUP BY gap_kind;
```

Alert when a run is `failed`, warnings persist across two schedules, due retry age exceeds the
schedule interval plus backoff, an expired lease remains after the next run, or the reconciliation
counts differ from the reviewed baseline (3 unindexed PDFs / 9 missing originals before repair).

There is no application-side automatic deletion. Pending retries and unresolved reconciliation
gaps are never retention-cleaned. A database operator may delete only completed operational
history under a separately reviewed ticket: connector runs older than 90 days and resolved retry
rows older than 180 days are the default candidates. `ingest_source_health` and unresolved gaps
are retained indefinitely unless a separate data-retention decision explicitly supersedes this
runbook.

Before any cleanup, run counts in a transaction and review them. `teamagent_app` intentionally has
no `DELETE`; cleanup requires a dedicated operator role with delete privilege and the admin GUC:

```sql
BEGIN;
SET LOCAL app.user_role = 'admin';

SELECT count(*) FROM ingest_connector_runs
WHERE completed_at < now() - interval '90 days';
SELECT count(*) FROM ingest_source_retries
WHERE status = 'resolved'
  AND resolved_at < now() - interval '180 days';

-- Execute only after the reviewed counts are approved:
-- DELETE FROM ingest_connector_runs
-- WHERE completed_at < now() - interval '90 days';
-- DELETE FROM ingest_source_retries
-- WHERE status = 'resolved'
--   AND resolved_at < now() - interval '180 days';

ROLLBACK;  -- replace with COMMIT only in the separately approved cleanup window
```

## Rollback

Rolling application code back does not require dropping tables. Keep the additive schema during an
application rollback; the old worker fails open if it cannot use a newer observation constraint,
while indexed documents and chunks remain untouched.

Only after all writers are rolled back, and only if losing operational history is explicitly
accepted, the new tables can be removed in dependency-safe order:

```sql
DROP TABLE IF EXISTS ingest_reconciliation_gaps;
DROP TABLE IF EXISTS ingest_source_retries;
DROP TABLE IF EXISTS ingest_connector_runs;
DROP TABLE IF EXISTS ingest_source_health;
```

Replacing a source Office file or ingesting a missing PDF remains a separately reviewed operator
action. This implementation does not mutate Drive or perform those recovery ingests.
