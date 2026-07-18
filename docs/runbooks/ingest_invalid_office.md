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
deleted. Every claim generates a new opaque `lease_token`; heartbeat, failure recording, and
resolution require the exact `(lease_owner, lease_token, pending, unexpired)` tuple. Reusing an
owner name can never make an old claim valid again.

An exact permanent fingerprint is observed again without downloading its body. A changed
fingerprint is revalidated. The incremental cursor can continue past a transiently failed file
without losing it or forcing all healthy files to repeat.

Cursor advancement is conditional on durable retry state. A missing retry API, claim failure,
`record_source_retry()` returning false/raising, lease loss, or connector-state save failure makes
the connector run fail and leaves the old cursor in place. A claimed row renews its lease during
bounded ZIP reads, Office text traversal, chunk embedding, classification/contextualization
boundaries, and immediately before upsert. Renewal verifies both request ownership and pending
status against the exact token and an unexpired lease; loss stops that file before an upsert.
Claimed success locks and verifies that retry, upserts the document/chunks, and resolves the retry
inside one database transaction. A failed/rejected transaction rolls back every document and chunk
write. Fingerprint equality never overrides a claimed lease. If the atomic operation is unavailable,
raises, or rejects the write, the source is failed, the cursor and stale cleanup remain blocked,
and the retry remains recoverable.

Drive changes and file listings reject repeated pagination tokens and a still-present token at the
safety page limit. Incomplete changes pagination falls back to a full listing. An incomplete full
listing fails the source, so neither cursor advancement nor stale marking can use a partial set.
Recursive folder/shared-drive walk saturation likewise fails the source and disables stale
marking for the affected root. Every `files.list` requests `incompleteSearch`; only an explicit
`false` is accepted. `true`, a missing field, or an invalid value raises
`IncompleteDriveTraversal`.

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
`UPDATE` only. `DELETE`, `TRUNCATE`, `REFERENCES`, `TRIGGER`, and every grant option are forbidden
for `teamagent_app`, including when broad default privileges or inherited roles could otherwise
widen access.

## Rollout order

Production is **NO-GO** until every gate below has named evidence in the change ticket and an
independent reviewer has approved it. Never infer snapshot, scheduler, or drain state from an old
ticket.

### Before the maintenance window

1. Record the approved maintenance window, operator, independent reviewer, application commit,
   target database identifier, and expected migration checksum.
2. Pause both the scheduler rule and every manual/ad-hoc ingest launch path. Record evidence for
   each pause separately; disabling only the scheduler is insufficient.
3. Drain every in-flight ingest worker. The repository labels every active ingest transaction with
   the exact PostgreSQL `application_name` `teamagent-ingest`, but that label is not the sole
   writer detector: inventory every client session and correlate database role, client address,
   state, transaction age, task/process ID, scheduler target, and manual launcher. The operator is
   excluded only by the current `pg_backend_pid()`—never by a broad application-name pattern.
   Confirm the scheduler and manual/ad-hoc launch paths are paused, all tasks/processes have exited,
   and every non-operator session capable of writing is disconnected or independently proven
   read-only. If any writer cannot drain naturally, abort the window.
4. Create or verify a **fresh RDS snapshot or restore point** after the drain and final pre-window
   write. Record its identifier, creation timestamp, database identifier, and a successful
   restorable status check. A snapshot request that is still creating, belongs to another database,
   or predates the final write does not satisfy this gate. Keep all writer launch paths paused while
   the snapshot completes.
5. Capture pre-migration row counts and lock state with an operator connection:

```sql
SELECT count(*) AS source_health_rows FROM ingest_source_health;
SELECT count(*) AS connector_run_rows FROM ingest_connector_runs;

SELECT pid, locktype, mode, granted
FROM pg_locks
WHERE relation = 'ingest_source_health'::regclass
ORDER BY pid, locktype, mode;

-- Record this exact operator backend. Only this PID may be excluded below.
SELECT pg_backend_pid() AS operator_pid,
       current_user AS operator_database_role,
       inet_client_addr() AS operator_client_addr,
       current_setting('application_name') AS operator_application_name;

-- Complete client inventory: do not filter only on application_name.
SELECT pid,
       usename AS database_role,
       application_name,
       client_addr,
       client_port,
       backend_type,
       state,
       backend_xid,
       backend_xmin,
       xact_start,
       query_start,
       state_change,
       wait_event_type,
       wait_event,
       left(query, 160) AS query_sample
FROM pg_stat_activity
WHERE datname = current_database()
  AND backend_type = 'client backend'
ORDER BY (pid = pg_backend_pid()) DESC, state, xact_start NULLS LAST, pid;

-- Drain gate. Run from the recorded operator connection. Zero rows is required; if the
-- environment deliberately retains a read-only session, attach role/client/state evidence and
-- an independent approval instead of weakening this predicate.
SELECT pid,
       usename AS database_role,
       application_name,
       client_addr,
       state,
       backend_xid,
       xact_start,
       wait_event_type,
       wait_event
FROM pg_stat_activity
WHERE datname = current_database()
  AND backend_type = 'client backend'
  AND pid <> pg_backend_pid();
```

The active ingest label must be exactly `teamagent-ingest`; wildcard matches are diagnostic only.
Any unidentified role/client, non-idle or open-transaction session, unexpected writer, ungranted
lock, or unexplained count change makes the window NO-GO. Do not treat the migration operator,
identified solely by its recorded PID, as evidence that another operator session is safe.

### Transactional migration gate

The only supported production path is `scripts/migrate.py` with `autocommit=False`. Do not paste
0020/0021 into an autocommit SQL console, do not use `psql -f`, and do not use `--rerun` in
production. The runner owns one transaction per migration, rejects migration files containing
transaction-control commands, atomically writes `schema_migrations`, and treats checksum drift as
a hard failure. Migration 0021 releases only legacy/inconsistent tokenless leases while writers
are drained, adds the per-claim token constraints, and reasserts the narrow privilege set.

```bash
DATABASE_URL='postgresql://operator@db.example.invalid/teamagent?sslmode=require&application_name=teamagent-migration-operator' \
  python scripts/migrate.py --dry-run

DATABASE_URL='postgresql://operator@db.example.invalid/teamagent?sslmode=require&application_name=teamagent-migration-operator' \
  python scripts/migrate.py
```

The dry run must list both 0020 and 0021 as pending (or show each exact checksum as already
applied) and must not write database state. During the real run, monitor `pg_locks` and abort on an
unexpected blocker. Do not resume any writer until all post-migration checks pass.

### Mandatory post-migration validation

Run these checks in the same maintenance window and attach their output with identifiers redacted:

```sql
SELECT version, filename, checksum_sha, applied_at
FROM schema_migrations
WHERE version IN ('0019', '0020', '0021')
ORDER BY version;

SELECT
    (SELECT count(*) FROM ingest_source_health) AS source_health_rows,
    (SELECT count(*) FROM ingest_connector_runs) AS connector_run_rows,
    (SELECT count(*) FROM ingest_source_retries) AS retry_rows,
    (SELECT count(*) FROM ingest_reconciliation_gaps) AS reconciliation_rows;

SELECT conname, contype, convalidated
FROM pg_constraint
WHERE conrelid IN (
    'ingest_source_health'::regclass,
    'ingest_connector_runs'::regclass,
    'ingest_source_retries'::regclass,
    'ingest_reconciliation_gaps'::regclass
)
ORDER BY conrelid::regclass::text, conname;

SELECT relname, relrowsecurity, relforcerowsecurity
FROM pg_class
WHERE oid IN (
    'ingest_source_health'::regclass,
    'ingest_connector_runs'::regclass,
    'ingest_source_retries'::regclass,
    'ingest_reconciliation_gaps'::regclass
)
ORDER BY relname;

-- Exactly one reviewed FOR ALL policy per table, with the exact admin expression and no extras.
DO $$
DECLARE
    bad_expected TEXT;
    extra_policies TEXT;
BEGIN
    WITH expected(table_name, policy_name) AS (
        VALUES
            ('ingest_source_health', 'ingest_source_health_admin'),
            ('ingest_connector_runs', 'ingest_connector_runs_admin'),
            ('ingest_source_retries', 'ingest_source_retries_admin'),
            ('ingest_reconciliation_gaps', 'ingest_reconciliation_gaps_admin')
    ),
    actual AS (
        SELECT c.relname AS table_name,
               p.polname AS policy_name,
               p.polpermissive,
               p.polcmd,
               p.polroles,
               pg_get_expr(p.polqual, p.polrelid) AS using_expression,
               pg_get_expr(p.polwithcheck, p.polrelid) AS check_expression
        FROM pg_policy AS p
        JOIN pg_class AS c ON c.oid = p.polrelid
        WHERE c.relname IN (
            'ingest_source_health',
            'ingest_connector_runs',
            'ingest_source_retries',
            'ingest_reconciliation_gaps'
        )
          AND c.relnamespace = current_schema()::regnamespace
    )
    SELECT string_agg(f.table_name, ', ' ORDER BY f.table_name)
    INTO bad_expected
    FROM (
        SELECT e.table_name
        FROM expected AS e
        LEFT JOIN actual AS a
          ON a.table_name = e.table_name
         AND a.policy_name = e.policy_name
        GROUP BY e.table_name, e.policy_name
        HAVING count(a.policy_name) <> 1
            OR bool_or(a.polpermissive IS DISTINCT FROM true)
            OR bool_or(a.polcmd IS DISTINCT FROM '*')
            OR bool_or(a.polroles IS DISTINCT FROM ARRAY[0]::oid[])
            OR bool_or(
                a.using_expression IS DISTINCT FROM
                '(current_setting(''app.user_role''::text, true) = ''admin''::text)'
            )
            OR bool_or(
                a.check_expression IS DISTINCT FROM
                '(current_setting(''app.user_role''::text, true) = ''admin''::text)'
            )
    ) AS f;

    WITH expected(table_name, policy_name) AS (
        VALUES
            ('ingest_source_health', 'ingest_source_health_admin'),
            ('ingest_connector_runs', 'ingest_connector_runs_admin'),
            ('ingest_source_retries', 'ingest_source_retries_admin'),
            ('ingest_reconciliation_gaps', 'ingest_reconciliation_gaps_admin')
    )
    SELECT string_agg(c.relname || '.' || p.polname, ', ' ORDER BY c.relname, p.polname)
    INTO extra_policies
    FROM pg_policy AS p
    JOIN pg_class AS c ON c.oid = p.polrelid
    LEFT JOIN expected AS e
      ON e.table_name = c.relname
     AND e.policy_name = p.polname
    WHERE c.relnamespace = current_schema()::regnamespace
      AND c.relname IN (
          'ingest_source_health',
          'ingest_connector_runs',
          'ingest_source_retries',
          'ingest_reconciliation_gaps'
      )
      AND e.policy_name IS NULL;

    IF bad_expected IS NOT NULL THEN
        RAISE EXCEPTION 'RLS policy contract mismatch: %', bad_expected;
    END IF;
    IF extra_policies IS NOT NULL THEN
        RAISE EXCEPTION 'extra policy (including extra permissive policy) forbidden: %',
            extra_policies;
    END IF;
END
$$;

SELECT table_name, privilege_type, is_grantable
FROM information_schema.role_table_grants
WHERE grantee = 'teamagent_app'
  AND table_name IN (
      'ingest_source_health',
      'ingest_connector_runs',
      'ingest_source_retries',
      'ingest_reconciliation_gaps'
  )
ORDER BY table_name, privilege_type;

-- Complete effective privilege matrix for every table privilege in PostgreSQL 16.
-- This query must return zero rows.
WITH target_tables(table_name) AS (
    VALUES
        ('ingest_source_health'),
        ('ingest_connector_runs'),
        ('ingest_source_retries'),
        ('ingest_reconciliation_gaps')
),
expected(privilege_type, allowed) AS (
    VALUES
        ('SELECT', true),
        ('INSERT', true),
        ('UPDATE', true),
        ('DELETE', false),
        ('TRUNCATE', false),
        ('REFERENCES', false),
        ('TRIGGER', false)
)
SELECT t.table_name,
       e.privilege_type,
       e.allowed AS expected,
       has_table_privilege(
           'teamagent_app',
           t.table_name,
           e.privilege_type
       ) AS actual
FROM target_tables AS t
CROSS JOIN expected AS e
WHERE has_table_privilege(
          'teamagent_app',
          t.table_name,
          e.privilege_type
      ) IS DISTINCT FROM e.allowed
ORDER BY t.table_name, e.privilege_type;

-- No privilege, including the three allowed writes, may be grantable. Zero rows is required.
WITH target_tables(table_name) AS (
    VALUES
        ('ingest_source_health'),
        ('ingest_connector_runs'),
        ('ingest_source_retries'),
        ('ingest_reconciliation_gaps')
),
all_privileges(privilege_type) AS (
    VALUES
        ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'),
        ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
)
SELECT t.table_name, p.privilege_type
FROM target_tables AS t
CROSS JOIN all_privileges AS p
WHERE has_table_privilege(
    'teamagent_app',
    t.table_name,
    p.privilege_type || ' WITH GRANT OPTION'
)
ORDER BY t.table_name, p.privilege_type;

-- Role attributes and both directions of membership must match the ticket's approved list.
SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolbypassrls
FROM pg_roles
WHERE rolname = 'teamagent_app';

SELECT granted.rolname AS granted_role,
       member.rolname AS member_role,
       grantor.rolname AS grantor_role,
       m.admin_option,
       m.inherit_option,
       m.set_option
FROM pg_auth_members AS m
JOIN pg_roles AS granted ON granted.oid = m.roleid
JOIN pg_roles AS member ON member.oid = m.member
JOIN pg_roles AS grantor ON grantor.oid = m.grantor
WHERE granted.rolname = 'teamagent_app'
   OR member.rolname = 'teamagent_app'
ORDER BY granted.rolname, member.rolname;

SELECT pg_has_role(current_user, 'teamagent_app', 'MEMBER') AS operator_can_set_role;

-- Executable RLS behavior probe for all four tables. Run only inside this transaction; the final
-- ROLLBACK is mandatory. Every no-admin INSERT must raise insufficient_privilege, blank-GUC
-- SELECTs must see zero probes, and admin-GUC SELECTs must see all four probes.
BEGIN;
SET LOCAL ROLE teamagent_app;
SELECT set_config('app.user_role', 'admin', true);

INSERT INTO ingest_source_health
    (source_type, external_id, md5_checksum, size_bytes, reason,
     mime_type, validator_schema_version)
VALUES
    ('gdrive', 'rls-contract-probe',
     '0123456789abcdef0123456789abcdef', 1, 'rls_contract_probe',
     'application/test', 'runbook-contract-v1');
INSERT INTO ingest_connector_runs
    (request_id, source_kind, source_id, outcome)
VALUES
    ('rls-contract-probe', 'gdrive', 'rls-contract-probe', 'success');
INSERT INTO ingest_source_retries
    (source_kind, source_id, source_type, external_id, mime_type,
     validator_schema_version, reason)
VALUES
    ('gdrive', 'rls-contract-probe', 'gdrive', 'rls-contract-probe',
     'application/test', 'runbook-contract-v1', 'rls_contract_probe');
INSERT INTO ingest_reconciliation_gaps
    (gap_key, source_kind, gap_kind, source_ref_hashes)
VALUES
    ('rls-contract-probe', 'gdrive', 'unindexed_pdf', ARRAY['rls-contract-probe']);

SELECT set_config('app.user_role', '', true);

DO $$
BEGIN
    BEGIN
        INSERT INTO ingest_source_health
            (source_type, external_id, md5_checksum, size_bytes, reason,
             mime_type, validator_schema_version)
        VALUES
            ('gdrive', 'rls-contract-denied',
             'abcdef0123456789abcdef0123456789', 1, 'rls_contract_probe',
             'application/test', 'runbook-contract-v1');
        RAISE EXCEPTION 'no-admin source_health INSERT unexpectedly allowed'
            USING ERRCODE = 'check_violation';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
    BEGIN
        INSERT INTO ingest_connector_runs
            (request_id, source_kind, source_id, outcome)
        VALUES
            ('rls-contract-denied', 'gdrive', 'rls-contract-denied', 'success');
        RAISE EXCEPTION 'no-admin connector_runs INSERT unexpectedly allowed'
            USING ERRCODE = 'check_violation';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
    BEGIN
        INSERT INTO ingest_source_retries
            (source_kind, source_id, source_type, external_id, mime_type,
             validator_schema_version, reason)
        VALUES
            ('gdrive', 'rls-contract-denied', 'gdrive', 'rls-contract-denied',
             'application/test', 'runbook-contract-v1', 'rls_contract_probe');
        RAISE EXCEPTION 'no-admin source_retries INSERT unexpectedly allowed'
            USING ERRCODE = 'check_violation';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
    BEGIN
        INSERT INTO ingest_reconciliation_gaps
            (gap_key, source_kind, gap_kind, source_ref_hashes)
        VALUES
            ('rls-contract-denied', 'gdrive', 'unindexed_pdf',
             ARRAY['rls-contract-denied']);
        RAISE EXCEPTION 'no-admin reconciliation_gaps INSERT unexpectedly allowed'
            USING ERRCODE = 'check_violation';
    EXCEPTION WHEN insufficient_privilege THEN
        NULL;
    END;
END
$$;

SELECT
    (SELECT count(*) FROM ingest_source_health
     WHERE external_id = 'rls-contract-probe') AS source_health_without_admin,
    (SELECT count(*) FROM ingest_connector_runs
     WHERE request_id = 'rls-contract-probe') AS connector_runs_without_admin,
    (SELECT count(*) FROM ingest_source_retries
     WHERE external_id = 'rls-contract-probe') AS source_retries_without_admin,
    (SELECT count(*) FROM ingest_reconciliation_gaps
     WHERE gap_key = 'rls-contract-probe') AS reconciliation_without_admin;

SELECT set_config('app.user_role', 'admin', true);
SELECT
    (SELECT count(*) FROM ingest_source_health
     WHERE external_id = 'rls-contract-probe') AS source_health_with_admin,
    (SELECT count(*) FROM ingest_connector_runs
     WHERE request_id = 'rls-contract-probe') AS connector_runs_with_admin,
    (SELECT count(*) FROM ingest_source_retries
     WHERE external_id = 'rls-contract-probe') AS source_retries_with_admin,
    (SELECT count(*) FROM ingest_reconciliation_gaps
     WHERE gap_key = 'rls-contract-probe') AS reconciliation_with_admin;
ROLLBACK;
```

The 0019/0020/0021 checksum rows must be present; pre-existing table counts must not decrease; the
fingerprint unique constraint, lease-token constraints, and all check constraints must be
validated. All four tables must have both RLS flags true and exactly the reviewed admin policy.
The blank-GUC probe must return four zeros; the admin-GUC probe must return four ones.
`teamagent_app` must be non-superuser/non-bypass-RLS, membership must exactly match the approved
principal list, the privilege-mismatch and grant-option queries must return zero rows, and the
direct grant listing must contain only non-grantable `SELECT`, `INSERT`, and `UPDATE`.

Resume one connector first and perform a staggered first full revalidation. Verify its connector
outcome, cursor behavior, retry lease/resolve behavior, warnings, database load, and row-count
deltas before enabling the next connector. Do not start every folder or shared-drive crawl
simultaneously. Resume manual launches only after the staged run is accepted; resume the scheduler
last. While the canary is actively writing, independently confirm its `pg_stat_activity` row uses
exactly `application_name = 'teamagent-ingest'`, record its database role/client/state, and verify
every claimed retry has a non-empty token that is cleared only when the same atomic document
transaction resolves.

Migration 0019 is byte-for-byte the originally released migration and must never be edited.
Migration 0020 is the forward-only upgrade: it labels old observations `ooxml-legacy-v1`, fills
legacy empty MIME values, installs the full fingerprint constraint, retry/reconciliation tables,
forced RLS, and privilege revokes. Existing databases that already applied 0019 and fresh
databases that apply original 0019 followed by 0020 therefore converge on the same schema.
Reapplying 0020 is supported and must be part of local PostgreSQL verification.
Migration 0021 is the forward-only lease-token upgrade. It releases legacy tokenless leases only
inside the drained maintenance window, validates the three-field lease consistency constraints,
and reasserts the complete table privilege contract. Reapplying 0021 must preserve a valid active
tokenized lease and is part of local PostgreSQL verification.

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

0020 and 0021 are forward-only. There is no routine destructive down migration.

- If `scripts/migrate.py` fails before either migration commits, that migration transaction rolls
  back its DDL/data changes and `schema_migrations` marker. Keep all writers paused, capture the
  error and lock state, correct the cause, and rerun the unchanged migration through
  `scripts/migrate.py`.
- If 0020/0021 committed but the new application is unhealthy, keep writers paused and prefer a
  forward application correction. A pre-token application cannot safely claim under the 0021
  lease constraint; do not resume it as an ingest writer. Keep the additive schema and do not clear
  retry rows, drop constraints, edit 0019/0020/0021, or use `--rerun`.
- If a committed schema defect is found, add a separately reviewed forward recovery migration
  (0022 or later), validate it against a restored copy of the recorded snapshot, then run it through
  `scripts/migrate.py` in a new maintenance window.
- Restoring the verified snapshot is the last-resort recovery path because it discards every
  post-snapshot write. It requires a new incident decision, confirmed writer pause/drain, explicit
  data-loss acceptance, and independent approval.

After either application rollback or forward recovery, repeat the constraint, RLS, grant,
row-count, retry-state, and connector-cursor validation above before staggered restart. Production
remains NO-GO until the post-recovery evidence is independently reviewed.

Replacing a source Office file or ingesting a missing PDF remains a separately reviewed operator
action. This implementation does not mutate Drive or perform those recovery ingests.
