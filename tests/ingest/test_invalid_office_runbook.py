"""Invalid Office production gate and forward-recovery runbook contracts."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_RUNBOOK = _ROOT / "docs/runbooks/ingest_invalid_office.md"
_MIGRATION_0020 = _ROOT / "infra/migrations/0020_ingest_source_retry_upgrade.sql"
_MIGRATION_0021 = _ROOT / "infra/migrations/0021_ingest_source_retry_lease_token.sql"
_MIGRATE_RUNNER = _ROOT / "scripts/migrate.py"


def test_runbook_contains_every_mandatory_production_gate() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    required_contracts = (
        "Production is **NO-GO**",
        "independent reviewer",
        "fresh RDS snapshot or restore point",
        "manual/ad-hoc ingest launch path",
        "in-flight ingest worker",
        "scripts/migrate.py",
        "autocommit=False",
        "pg_locks",
        "pg_stat_activity",
        "teamagent-ingest",
        "pid <> pg_backend_pid()",
        "database_role",
        "client_addr",
        "backend_xid",
        "source_health_rows",
        "schema_migrations",
        "pg_constraint",
        "relforcerowsecurity",
        "information_schema.role_table_grants",
        "has_table_privilege",
        "WITH GRANT OPTION",
        "pg_auth_members",
        "SET LOCAL ROLE teamagent_app",
        "extra permissive policy",
        "forward recovery migration",
        "staggered first full revalidation",
        "resume the scheduler",
    )

    missing = [contract for contract in required_contracts if contract not in runbook]
    assert missing == []


def test_runbook_and_migrations_require_forward_only_recovery() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    rollback_section = runbook.split("## Rollback", maxsplit=1)[1]
    migration_0020 = _MIGRATION_0020.read_text(encoding="utf-8")
    migration_0021 = _MIGRATION_0021.read_text(encoding="utf-8")

    assert "0020 and 0021 are forward-only" in rollback_section
    assert "0022 or later" in rollback_section
    assert "DROP TABLE" not in rollback_section
    assert "forward-only migration" in migration_0020
    assert "DROP TABLE" not in migration_0020
    assert "DROP TABLE" not in migration_0021


def test_runbook_pins_all_four_rls_policies_and_complete_privilege_matrix() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")

    for table in (
        "ingest_source_health",
        "ingest_connector_runs",
        "ingest_source_retries",
        "ingest_reconciliation_gaps",
    ):
        assert f"('{table}', '{table}_admin')" in runbook
    for privilege in (
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    ):
        assert f"('{privilege}'" in runbook
    assert runbook.count("rls-contract-denied") >= 4
    assert "operator is" in runbook
    assert "excluded only by the current `pg_backend_pid()`" in runbook


def test_migration_runner_contract_is_transactional_and_drift_fails_closed() -> None:
    runner = _MIGRATE_RUNNER.read_text(encoding="utf-8")

    assert "psycopg.connect(dsn, autocommit=False)" in runner
    assert "migration connection must use autocommit=False" in runner
    assert "_TRANSACTION_CONTROL_RE.search(sql)" in runner
    assert "conn.rollback()" in runner
    assert "[ERROR]" in runner
    assert "forward fix" in runner
