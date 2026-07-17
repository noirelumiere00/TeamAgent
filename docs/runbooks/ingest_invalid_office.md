# Invalid Office source observability

TeamAgent validates Drive `docx` / `pptx` / `xlsx` payloads before any document upsert:

- downloaded size and Drive-advertised MD5 must match;
- the ZIP central directory must open and `ZipFile.testzip()` must pass;
- the required OOXML part (`word/document.xml`, `ppt/presentation.xml`, or
  `xl/workbook.xml`) must exist and parse as XML.

An invalid payload is skipped without updating `documents`, deleting `chunks`, or making the
observed Drive file stale. Deterministic `corrupt_zip` / `format_mismatch` results are recorded in
`ingest_source_health` by `(source_type, external_id, md5_checksum, size_bytes)`.

## Retry and recovery behavior

An exact known-invalid fingerprint is reported again but its body is not downloaded. A changed MD5
or size is a new fingerprint and is fully revalidated, so replacing the Drive object with a valid
revision recovers on the next ingest. Download-response, size, and checksum mismatches are warnings
but are not cached as permanent invalid sources because they may be transient.

Connector completion is recorded in `ingest_connector_runs` as `success`,
`success_with_warnings`, or `failed`. Incremental Drive state also carries aggregate warning counts
and reasons in the existing `connector_state.metadata` JSON, preserving compatibility with readers
that only understand the legacy columns. Both new operational tables enforce RLS and are readable
or writable only through an `app.user_role=admin` connection.

## Deployment and rollback

Apply `infra/migrations/0019_ingest_source_health.sql` before or alongside the application rollout.
The application tolerates the tables being absent during a rolling deployment and falls back to
revalidation.

Rollback of the application requires no schema change. If the observation data must also be
removed, after rolling back all writers:

```sql
DROP TABLE IF EXISTS ingest_connector_runs;
DROP TABLE IF EXISTS ingest_source_health;
```

These tables are operational observations only; dropping them does not modify indexed documents or
chunks. Recovery of a missing PDF or replacement of a source Office file remains an explicit,
separate operator action.
