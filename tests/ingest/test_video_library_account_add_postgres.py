"""0042 actual RLS and account writes in a disposable, rolled-back schema."""

from pathlib import Path
from typing import Any

import psycopg
import pytest

from teamagent.ingest.video_library_account_repository import (
    OWN_ACCOUNT_OPERATORS,
    AccountAddRepository,
)
from teamagent.ingest.video_library_repository import VideoLibraryRepository
from tests.ingest.test_video_library_postgres import _DSN, db

__all__ = ["db"]
pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEAMAGENT_TEST_DB_DSN")
MIGRATIONS = Path(__file__).resolve().parents[2] / "infra/migrations"
EMAIL = "member@example.test"


@pytest.fixture
def accounts(db: tuple[Any, VideoLibraryRepository]) -> tuple[Any, AccountAddRepository]:
    conn, repository = db
    for name in (
        "0036_video_library_settings.sql",
        "0037_video_library_url_import.sql",
        "0038_video_campaign_links.sql",
        "0039_video_library_import_ui.sql",
        "0040_video_library_fetch_resume.sql",
        "0041_video_library_external_cases.sql",
        "0042_video_library_account_add.sql",
        "0043_video_library_insights.sql",
    ):
        conn.execute((MIGRATIONS / name).read_text())
    return conn, AccountAddRepository(repository.pg)


@pytest.mark.parametrize("operator", OWN_ACCOUNT_OPERATORS)
def test_employee_preview_add_and_retry(
    accounts: tuple[Any, AccountAddRepository], operator: str
) -> None:
    conn, repo = accounts
    lines = ["", "https://www.tiktok.com/@SAMPLE/video/123", "bad!", "@sample"]
    kwargs = dict(operator_label=operator, user_email=EMAIL, request_id="employee-add")
    preview = repo.preview(lines, **kwargs)
    assert [r.status for r in preview] == ["added", "invalid", "duplicate"]
    assert conn.execute("SELECT count(*) AS n FROM vl_accounts").fetchone()["n"] == 0
    added = repo.add(lines, **kwargs)
    assert added == preview
    row = conn.execute("SELECT * FROM vl_accounts").fetchone()
    assert row["handle"] == "sample" and row["operator_label"] == operator
    assert row["added_by"] == EMAIL and row["added_at"] is not None
    assert row["last_fetched_on"] is None
    assert conn.execute("SELECT current_user AS role").fetchone()["role"] == "teamagent_app"
    assert (
        conn.execute("SELECT current_setting('app.user_role') AS role").fetchone()["role"]
        == "viewer"
    )
    assert [r.status for r in repo.add(lines, **kwargs)] == ["exists", "invalid", "duplicate"]
    assert [r.status for r in repo.preview(lines, **kwargs)] == ["exists", "invalid", "duplicate"]
    assert conn.execute("SELECT * FROM vl_accounts").fetchall() == [row]


@pytest.mark.parametrize("method", ["preview", "add"])
@pytest.mark.parametrize("handle", ["sample", "@SAMPLE", "SAMPLE"])
def test_existing_account_is_never_changed(
    accounts: tuple[Any, AccountAddRepository], method: str, handle: str
) -> None:
    conn, repo = accounts
    conn.execute(
        """INSERT INTO vl_accounts(platform,handle,operator_label,display_name,classification)
        VALUES ('tiktok',%s,'他社','既存名','既存分類')""",
        (handle,),
    )
    before = conn.execute("SELECT * FROM vl_accounts").fetchall()
    result = getattr(repo, method)(
        ["sample"], operator_label="NewsTV", user_email=EMAIL, request_id="existing"
    )
    assert result[0].status == "exists" and result[0].operator_label == "他社"
    assert conn.execute("SELECT * FROM vl_accounts").fetchall() == before
    assert before[0]["added_by"] is None and before[0]["added_at"] is None


@pytest.mark.parametrize(
    "added_by,operator",
    [
        ("other@example.test", "NewsTV"),
        (None, "NewsTV"),
        ("", "NewsTV"),
        (EMAIL, "他社"),
        (EMAIL, "newstv"),
        (EMAIL, "NewsTV "),
        (EMAIL, "ベクトル(VCL)"),
        (EMAIL, None),
    ],
)
def test_employee_insert_rls_rejects_forgery_and_other_operators(
    accounts: tuple[Any, AccountAddRepository], added_by: str | None, operator: str | None
) -> None:
    conn, repo = accounts
    with repo.pg.connection(app_role="teamagent_app", user_email=EMAIL, user_role="viewer"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                """INSERT INTO vl_accounts(platform,handle,operator_label,added_by)
                VALUES ('tiktok','sample',%s,%s)""",
                (operator, added_by),
            )


@pytest.mark.parametrize("email", ["", " "])
def test_empty_matching_identity_is_rejected(
    accounts: tuple[Any, AccountAddRepository], email: str
) -> None:
    conn, repo = accounts
    with repo.pg.connection(app_role="teamagent_app", user_role="viewer"):
        conn.execute("SELECT set_config('app.user_email',%s,true)", (email,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                """INSERT INTO vl_accounts(platform,handle,operator_label,added_by)
                VALUES ('tiktok','sample','NewsTV',%s)""",
                (email,),
            )


def test_employee_cannot_update_even_own_account(
    accounts: tuple[Any, AccountAddRepository],
) -> None:
    conn, repo = accounts
    repo.add(["sample"], operator_label="NewsTV", user_email=EMAIL, request_id="update")
    before = conn.execute("SELECT * FROM vl_accounts").fetchall()
    assert (
        conn.execute(
            "UPDATE vl_accounts SET operator_label='gracemode',display_name='changed'"
        ).rowcount
        == 0
    )
    assert conn.execute("SELECT * FROM vl_accounts").fetchall() == before
    conn.execute("SELECT set_config('app.user_role','admin',true)")
    assert conn.execute("UPDATE vl_accounts SET display_name='admin changed'").rowcount == 1
    conn.execute(
        "INSERT INTO vl_accounts(platform,handle,operator_label) VALUES ('tiktok','admin','他社')"
    )


def test_migration_replay_preserves_legacy_nulls_and_view_columns(
    db: tuple[Any, VideoLibraryRepository],
) -> None:
    conn, _ = db
    conn.execute(
        "INSERT INTO vl_accounts(platform,handle,display_name) VALUES ('tiktok','legacy','legacy name')"
    )
    for name in (
        "0036_video_library_settings.sql",
        "0037_video_library_url_import.sql",
        "0038_video_campaign_links.sql",
        "0039_video_library_import_ui.sql",
        "0040_video_library_fetch_resume.sql",
        "0041_video_library_external_cases.sql",
    ):
        conn.execute((MIGRATIONS / name).read_text())
    before = [
        (d.name, d.type_code)
        for d in conn.execute("SELECT * FROM vl_videos_effective LIMIT 0").description
    ]
    migration = (MIGRATIONS / "0042_video_library_account_add.sql").read_text()
    conn.execute(migration)
    conn.execute(migration)
    assert conn.execute("SELECT added_by,added_at,last_fetched_on FROM vl_accounts").fetchone() == {
        "added_by": None,
        "added_at": None,
        "last_fetched_on": None,
    }
    after = [
        (d.name, d.type_code)
        for d in conn.execute("SELECT * FROM vl_videos_effective LIMIT 0").description
    ]
    assert after == before
