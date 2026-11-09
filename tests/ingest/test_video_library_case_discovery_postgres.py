"""0045 upsert, live eligibility/cooldown and RLS in a rollback-only schema."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from teamagent.adapters.apify_client import ApifyClient
from teamagent.ingest import video_library_case_discovery as discovery
from teamagent.ingest.video_library_account_repository import AccountAddRepository
from tests.ingest.test_video_library_account_add_postgres import MIGRATIONS, accounts, db
from tests.ingest.test_video_library_case_discovery import NOW, response
from tests.ingest.test_video_library_case_discovery_decision import engine
from tests.ingest.test_video_library_postgres import _DSN

__all__ = ["accounts", "db"]
pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEAMAGENT_TEST_DB_DSN")


def seed(conn: Any, count: int = 10) -> None:
    conn.execute(
        "INSERT INTO vl_accounts(platform,handle,operator_label) VALUES ('tiktok','sample','NewsTV')"
    )
    for i in range(1, count + 1):
        conn.execute(
            """INSERT INTO vl_videos
               (platform,video_id,url,account_handle,origin,is_pr,caption,hashtags,posted_at)
               VALUES ('tiktok',%s,%s,'sample','own',true,'DB caption',ARRAY['ＱＴＴＡ','qtta','PR'],%s)""",
            (str(i), f"https://www.tiktok.com/@sample/video/{i}", NOW + timedelta(days=i)),
        )


def test_sampled_upsert_head_migration_repeat_and_effective_filter(
    accounts: tuple[Any, AccountAddRepository],
) -> None:
    conn, repo = accounts
    seed(conn, 14)
    # The effective origin is derived from accounts/sheets, not the raw origin column.
    conn.execute("UPDATE vl_videos SET origin='external' WHERE video_id='1'")
    conn.execute("UPDATE vl_videos SET is_pr=false WHERE video_id='2'")
    conn.execute("UPDATE vl_videos SET campaign='known' WHERE video_id='3'")
    conn.execute("UPDATE vl_videos SET account_handle='external' WHERE video_id='4'")
    client, http_client = MagicMock(spec=ApifyClient), MagicMock()
    client.run_actor_sync.return_value = response()
    first = discovery.collect(
        repo.pg, client, http_client=http_client, request_id="db-first", now=NOW
    )
    assert first.tags == first.sampled == 1 and first.videos == 2
    row = conn.execute("SELECT * FROM vl_case_discovery").fetchone()
    # 0037 defaults ingest_sources to {sheet}; 0041 keeps video 4 own despite its new handle.
    assert (
        conn.execute("SELECT origin FROM vl_videos_effective WHERE video_id='4'").fetchone()[
            "origin"
        ]
        == "own"
    )
    assert row["tag"] == "qtta" and row["video_count"] == 12
    assert row["sample_video_ids"] == ["14", "13"] and row["status"] == "sampled"
    assert row["samples"] == [
        {"video_id": vid, "caption": "DB caption", "subtitle_text": "", "no_subtitle": True}
        for vid in ("14", "13")
    ]
    assert row["attempted_at"] == NOW and row["decided_at"] is None
    conn.execute("RESET ROLE")
    migration = (MIGRATIONS / "0045_video_library_case_discovery.sql").read_text()
    conn.execute(migration)
    conn.execute(migration)
    assert conn.execute("SELECT * FROM vl_case_discovery").fetchone() == row
    second_time = NOW + timedelta(days=1)
    second = discovery.collect(
        repo.pg, client, http_client=http_client, request_id="db-upsert", now=second_time
    )
    assert second.sampled == 1
    assert conn.execute("SELECT count(*) AS n FROM vl_case_discovery").fetchone()["n"] == 1
    assert (
        conn.execute("SELECT attempted_at FROM vl_case_discovery").fetchone()["attempted_at"]
        == second_time
    )
    assert not http_client.mock_calls


@pytest.mark.parametrize(
    "status,days,eligible",
    [
        ("registered", 1000, False),
        ("existing", 1000, False),
        ("candidate", 29, False),
        ("candidate", 31, True),
        ("failed", 29, False),
        ("failed", 31, True),
        ("unidentifiable", 89, False),
        ("unidentifiable", 91, True),
    ],
)
def test_cooldown_uses_persisted_status_and_attempted_at(
    accounts: tuple[Any, AccountAddRepository],
    status: str,
    days: int,
    eligible: bool,
) -> None:
    conn, repo = accounts
    seed(conn)
    conn.execute(
        """INSERT INTO vl_case_discovery(tag,video_count,status,attempted_at,
           advertiser,campaign,confidence,evidence,decided_at)
           VALUES ('qtta',10,%s,now() - %s * interval '1 day',
                   'old advertiser','old campaign',0.5,'{}',now())""",
        (status, days),
    )
    before = conn.execute("SELECT * FROM vl_case_discovery").fetchone()
    client = MagicMock(spec=ApifyClient)
    client.run_actor_sync.return_value = response()
    now = datetime.now(UTC)
    result = discovery.collect(
        repo.pg, client, http_client=MagicMock(), request_id="db-cooldown", now=now
    )
    assert bool(result.sampled) is eligible
    assert client.run_actor_sync.call_count == int(eligible)
    after = conn.execute("SELECT * FROM vl_case_discovery").fetchone()
    if eligible:
        assert after["attempted_at"] == now and after["status"] == "sampled"
        assert all(
            after[key] is None
            for key in ("advertiser", "campaign", "confidence", "evidence", "decided_at")
        )
    else:
        assert after == before


def test_employee_cannot_read_insert_update_or_delete_and_no_public_grants(
    accounts: tuple[Any, AccountAddRepository],
) -> None:
    conn, repo = accounts
    flags = conn.execute(
        """SELECT relrowsecurity,relforcerowsecurity FROM pg_class
           WHERE oid='vl_case_discovery'::regclass"""
    ).fetchone()
    assert flags == {"relrowsecurity": True, "relforcerowsecurity": True}
    assert (
        conn.execute(
            """SELECT count(*) AS n FROM pg_class,
           LATERAL aclexplode(coalesce(relacl,acldefault('r',relowner))) acl
           WHERE oid='vl_case_discovery'::regclass AND acl.grantee=0"""
        ).fetchone()["n"]
        == 0
    )
    with repo.pg.connection(app_role="teamagent_app", user_email=None, user_role="admin"):
        conn.execute(
            "INSERT INTO vl_case_discovery(tag,video_count,status) VALUES ('qtta',10,'sampled')"
        )
        assert (
            conn.execute("SELECT attempted_at FROM vl_case_discovery").fetchone()["attempted_at"]
            is not None
        )
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute("UPDATE vl_case_discovery SET status='invented'")
    with repo.pg.connection(
        app_role="teamagent_app", user_email="member@example.test", user_role="viewer"
    ):
        assert conn.execute("SELECT * FROM vl_case_discovery").fetchall() == []
        assert conn.execute("UPDATE vl_case_discovery SET status='registered'").rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "INSERT INTO vl_case_discovery(tag,video_count,status) VALUES ('other',10,'sampled')"
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("DELETE FROM vl_case_discovery")
    with repo.pg.connection(app_role="teamagent_app", user_email=None, user_role="admin"):
        assert (
            conn.execute("SELECT status FROM vl_case_discovery").fetchone()["status"] == "sampled"
        )


def test_registered_case_is_inserted_once_and_cooled_down(
    accounts: tuple[Any, AccountAddRepository],
) -> None:
    conn, repo = accounts
    seed(conn)
    conn.execute("UPDATE vl_videos SET caption='東洋水産 QTTA #PR'")
    apify = MagicMock(spec=ApifyClient)
    apify.run_actor_sync.return_value = response()
    inference = engine()
    assert (
        discovery.collect(
            repo.pg, apify, http_client=MagicMock(), request_id="register", now=NOW
        ).sampled
        == 1
    )
    registered = discovery.decide(repo.pg, inference, request_id="register")
    assert registered.counts == {"registered": 1}
    assert registered.video_ids == {str(i) for i in range(1, 11)}
    case = conn.execute(
        "SELECT advertiser,campaign,created_by,created_via FROM vl_campaigns"
    ).fetchall()
    assert case == [
        dict(
            advertiser="東洋水産",
            campaign="QTTA",
            created_by="case_discovery@system",
            created_via="case_discovery",
        )
    ]
    row = conn.execute("SELECT status,decided_at FROM vl_case_discovery").fetchone()
    assert row["status"] == "registered" and row["decided_at"] is not None
    assert (
        discovery.collect(
            repo.pg,
            apify,
            http_client=MagicMock(),
            request_id="repeat",
            now=NOW + timedelta(days=100),
        ).sampled
        == 0
    )
    assert discovery.decide(repo.pg, inference, request_id="repeat").counts == {}
    assert conn.execute("SELECT count(*) AS n FROM vl_campaigns").fetchone()["n"] == 1
    assert apify.run_actor_sync.call_count == inference.client.converse.call_count == 1
