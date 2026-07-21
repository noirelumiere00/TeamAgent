"""search_feedback の評価スキーマと INSERT-only 権限の静的契約テスト。"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "infra" / "migrations"
MIG_0015 = MIGRATIONS_DIR / "0015_search_feedback.sql"
MIG_0022 = MIGRATIONS_DIR / "0022_search_feedback_score.sql"


def _without_line_comments(sql: str) -> str:
    """ロールバック等の SQL コメントを除き、実行 SQL だけを検査する。"""
    return "\n".join(line.split("--", maxsplit=1)[0] for line in sql.splitlines())


def test_search_feedback_migrations_exist() -> None:
    assert MIG_0015.exists(), f"missing: {MIG_0015}"
    assert MIG_0022.exists(), f"missing: {MIG_0022}"


def test_0015_original_rating_and_privacy_contract_is_preserved() -> None:
    sql = MIG_0015.read_text(encoding="utf-8")

    assert "CHECK (rating IN (-1, 1))" in sql
    assert "GRANT SELECT, INSERT ON search_feedback TO teamagent_app;" in sql
    assert "本文(answer/chunk content)は保存しない" in sql


def test_0022_adds_score_and_insert_only_contract() -> None:
    sql = MIG_0022.read_text(encoding="utf-8")

    assert "-- 0022:" in sql
    for column in ("score SMALLINT", "search_session_id TEXT", "answer_id TEXT"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql

    assert "search_feedback_score_range" in sql
    assert "score IS NULL OR score BETWEEN 1 AND 4" in sql
    assert "search_feedback_score_rating_map" in sql
    assert "score >= 3 AND rating = 1" in sql
    assert "score <= 2 AND rating = -1" in sql
    assert "REVOKE SELECT, UPDATE, DELETE ON search_feedback FROM teamagent_app;" in sql


def test_0022_documents_rollback_sql() -> None:
    sql = MIG_0022.read_text(encoding="utf-8")

    for statement in (
        "GRANT SELECT ON search_feedback TO teamagent_app;",
        "DROP INDEX IF EXISTS search_feedback_session_idx;",
        "ALTER TABLE search_feedback DROP CONSTRAINT IF EXISTS search_feedback_score_rating_map;",
        "ALTER TABLE search_feedback DROP CONSTRAINT IF EXISTS search_feedback_score_range;",
        "ALTER TABLE search_feedback DROP COLUMN IF EXISTS answer_id;",
        "ALTER TABLE search_feedback DROP COLUMN IF EXISTS search_session_id;",
        "ALTER TABLE search_feedback DROP COLUMN IF EXISTS score;",
    ):
        assert statement in sql


def test_no_migration_from_0022_regrants_search_feedback_read_or_mutation() -> None:
    """0022 以降で app ロールの INSERT-only 契約を再び緩めない。"""
    forbidden_privileges = r"(?:SELECT|UPDATE|DELETE)"
    grant_pattern = re.compile(
        rf"\bGRANT\s+(?=[^;]*\b{forbidden_privileges}\b)[^;]*"
        r"\bON\s+search_feedback\b[^;]*\bTO\s+teamagent_app\b",
        re.IGNORECASE,
    )

    offenders = [
        path.name
        for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        if int(path.name[:4]) >= 22
        and grant_pattern.search(_without_line_comments(path.read_text(encoding="utf-8")))
    ]

    assert offenders == [], f"search_feedback の INSERT-only 契約を緩めています: {offenders}"
