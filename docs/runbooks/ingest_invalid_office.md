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

Validation is bounded before extraction:

- the Drive download is capped at 256 MiB in `GDriveClient.download_file_bytes`; this applies to
  folder, recursive folder, shared-drive crawl, and sales-filter-off paths;
- compressed Office input is capped at 256 MiB;
- ZIP member count, each member's uncompressed size, total uncompressed size, and compression
  ratio are capped;
- required OOXML XML parts have separate read/parse limits;
- each bounded ZIP member is read to EOF for size and CRC verification; unbounded
  `ZipFile.testzip()` is not used;
- the required package part, OOXML root element, and `[Content_Types].xml` override must match the
  advertised MIME type;
- encrypted compound Office and encrypted ZIP members are classified as `encrypted_office`.

Deterministic `corrupt_zip`, `format_mismatch`, `unsafe_archive`, and `encrypted_office` outcomes
are persisted in `ingest_source_health`. Transient download, size, checksum, extraction, and empty
text failures are not made permanent.

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

PDF download, extraction, and empty-text failures join the same warning collector. Connector runs
are recorded as `success`, `success_with_warnings`, or `failed`, with reason counts suitable for an
ops notification. Notifications contain only source kind, aggregate reason counts, suppression
count, and request ID—not file names, customer names, raw Drive IDs, or content.

The reconciliation baseline keeps the independently identified three unindexed PDF gaps and nine
missing-original gaps unresolved in `ingest_reconciliation_gaps`. Only non-reversible Drive-ID
hashes are stored. A verified successful content ingest of a matching source resolves its gap;
normal no-change runs remain `success` only when no connector warning or unresolved baseline gap
exists.

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

For a database where the original 0019 checksum is already recorded, the migration runner may warn
that the edited 0019 file differs and leave that version untouched. Migration 0020 is the
forward-only upgrade: it labels old observations `ooxml-legacy-v1`, fills legacy empty MIME values,
installs the full fingerprint constraint, retry/reconciliation tables, RLS, and privilege revokes.
Fresh databases apply the new 0019 and then the idempotent 0020.

Application code treats a missing new table as unavailable and automatically probes it again after
60 seconds, so availability is not cached false forever. Migration-first plus worker restart is
still the supported rollout contract and avoids an old worker temporarily using the legacy
fingerprint semantics.

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
