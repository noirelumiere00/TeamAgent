"""0038 real database contracts, isolated and rolled back; never writes public tables."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from scripts.register_video_case_aliases import INITIAL_ALIASES, register_initial_aliases
from teamagent.connect_web.library import search_campaigns
from teamagent.ingest.video_library_case_models import (
    Case,
    Post,
    Prediction,
    application_prediction,
)
from teamagent.ingest.video_library_case_repository import CaseRepository
from tests.ingest.test_video_library_postgres import _DSN, NOW, db, seed_batch

__all__ = ["db"]
pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEAMAGENT_TEST_DB_DSN")
MIGRATIONS = Path(__file__).resolve().parents[2] / "infra/migrations"


def prepare(conn: Any, repository: Any) -> tuple[CaseRepository, dict[str, Any]]:
    for name in ("0036_video_library_settings.sql", "0037_video_library_url_import.sql"):
        conn.execute((MIGRATIONS / name).read_text())
    before = [
        (c.name, c.type_code)
        for c in conn.execute("SELECT * FROM vl_videos_effective LIMIT 0").description
    ]
    conn.execute((MIGRATIONS / "0038_video_campaign_links.sql").read_text())
    after = [
        (c.name, c.type_code)
        for c in conn.execute("SELECT * FROM vl_videos_effective LIMIT 0").description
    ]
    assert after[:-1] == before
    assert after[-1][0] == "case_inferred"
    assert "page_is_ad" not in dict(after)
    batch = seed_batch()
    batch.videos[0].caption = "test #PR"
    repository.save(batch, request_id="initial", started_at=NOW)
    repo = CaseRepository(repository.pg)
    return repo, repo.load()[1][0]


def result(**changes: Any) -> Prediction:
    return Prediction.model_validate(
        dict(
            advertiser="test",
            campaign="test",
            match_type="existing",
            confidence=0.99,
            evidence=[dict(field="caption", quote="test")],
        )
        | changes
    )


def effective(conn: Any) -> dict[str, Any]:
    return dict(conn.execute("SELECT * FROM vl_videos_effective").fetchone())


def test_sheet_sync_preserves_links_and_effective_priority(db: tuple[Any, Any]) -> None:
    conn, sheet_repo = db
    repo, row = prepare(conn, sheet_repo)
    cases = [Case(advertiser="test", campaign="test")]
    assert repo.save(row, result(), "applied", "hash", "haiku", cases)
    assert effective(conn)["case_inferred"] is True
    assert effective(conn)["campaign"] == "test"
    conn.execute("""INSERT INTO vl_campaign_settings
        (advertiser,campaign,business_category,is_private,updated_by)
        VALUES ('test','test','media',true,'admin')""")
    assert effective(conn)["is_private"] is True
    assert effective(conn)["business_category"] == "media"
    batch = seed_batch()
    batch.videos[0].caption = "test #PR"
    sheet_repo.save(batch, request_id="resync", started_at=NOW)
    assert effective(conn)["campaign"] == "test"
    assert conn.execute("SELECT count(*) AS n FROM vl_video_campaign_links").fetchone()["n"] == 1
    conn.execute("""INSERT INTO vl_video_campaign_links
        SELECT platform,video_id,advertiser,NULL,'human','applied','unknown',1,'[]',
        'human','v1','human-hash','admin',now() FROM vl_video_campaign_links WHERE source='ai'""")
    assert effective(conn)["advertiser"] == "test" and effective(conn)["campaign"] is None
    assert effective(conn)["case_inferred"] is False
    conn.execute("UPDATE vl_video_campaign_links SET campaign='test' WHERE source='human'")
    assert effective(conn)["campaign"] == "test" and effective(conn)["case_inferred"] is False
    batch.videos[0].advertiser = "sheet advertiser"
    batch.videos[0].campaign = "sheet campaign"
    sheet_repo.save(batch, request_id="known", started_at=NOW)
    assert effective(conn)["advertiser"] == "sheet advertiser"
    assert effective(conn)["campaign"] == "sheet campaign"
    assert effective(conn)["case_inferred"] is False
    batch.videos[0].campaign = None
    sheet_repo.save(batch, request_id="partial", started_at=NOW)
    assert effective(conn)["campaign"] is None  # Never mix different advertisers' campaigns.
    assert repo.load()[1] == []  # Human decisions are never reinferred.


@pytest.mark.parametrize("status", ["candidate", "unknown", "rejected"])
def test_unapplied_results_never_create_cases_or_change_organic(
    db: tuple[Any, Any],
    status: str,
) -> None:
    conn, sheet_repo = db
    repo, row = prepare(conn, sheet_repo)
    conn.execute("UPDATE vl_videos SET is_pr=false, advertiser='test'")
    row.update(is_pr=False, advertiser="test")
    p = result(campaign="new", match_type="new") if status == "candidate" else result()
    assert repo.save(row, p, status, "hash", "haiku", repo.load()[0])
    assert effective(conn)["campaign"] is None
    assert effective(conn)["business_category"] == "organic"
    assert conn.execute("SELECT count(*) AS n FROM vl_campaigns").fetchone()["n"] == 1
    assert search_campaigns(conn, {}, admin=True) == []
    assert repo.load()[1][0]["input_hash"] == "hash"


def test_organic_stays_organic_and_case_aggregate_marks_inference(db: tuple[Any, Any]) -> None:
    conn, sheet_repo = db
    repo, row = prepare(conn, sheet_repo)
    conn.execute("UPDATE vl_videos SET is_pr=false, advertiser='test'")
    row.update(is_pr=False, advertiser="test")
    assert repo.save(row, result(), "applied", "hash", "haiku", repo.load()[0])
    assert effective(conn)["is_pr"] is False
    assert effective(conn)["business_category"] == "organic"
    assert search_campaigns(conn, {}, admin=True)[0]["case_inferred"] is True
    conn.execute("UPDATE vl_videos SET campaign='sheet campaign'")
    assert effective(conn)["campaign"] == "sheet campaign"
    assert effective(conn)["case_inferred"] is False


def test_links_rls_human_rejection_stale_inputs_and_settings(db: tuple[Any, Any]) -> None:
    conn, sheet_repo = db
    repo, row = prepare(conn, sheet_repo)
    cases = repo.load()[0]
    assert repo.save(row, result(), "applied", "hash", "haiku", cases)
    conn.execute("UPDATE vl_videos SET caption='changed'")
    assert not repo.save(row, result(), "applied", "hash2", "haiku", cases)
    repo.invalidate(row)
    assert effective(conn)["campaign"] is None
    conn.execute("UPDATE vl_videos SET caption='test #PR'")
    assert not repo.save(row, result(), "applied", "hash2", "haiku", [])
    assert repo.save(row, result(), "applied", "hash2", "haiku", cases)
    conn.execute("""INSERT INTO vl_video_campaign_links
        SELECT platform,video_id,NULL,NULL,'human','rejected','unknown',0,'[]',
        'human','v1','human-hash','admin',now() FROM vl_video_campaign_links WHERE source='ai'""")
    assert effective(conn)["campaign"] is None
    assert not repo.save(row, result(), "applied", "hash3", "haiku", cases)
    conn.execute("SELECT set_config('app.user_role','viewer',true)")
    assert conn.execute("SELECT count(*) AS n FROM vl_video_campaign_links").fetchone()["n"] == 2
    assert conn.execute("UPDATE vl_video_campaign_links SET status='applied'").rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("DELETE FROM vl_video_campaign_links")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("""INSERT INTO vl_video_campaign_links
            (platform,video_id,source,status,match_type,confidence,model,prompt_version,input_hash,decided_by)
            VALUES ('tiktok','1','ai','unknown','unknown',0,'haiku','v1','x','viewer')""")
    assert (
        "security_invoker=true"
        in conn.execute(
            "SELECT reloptions FROM pg_class WHERE oid='vl_videos_effective'::regclass"
        ).fetchone()["reloptions"]
    )


def test_ambiguous_campaign_applies_only_advertiser_and_survives_sheet_sync(
    db: tuple[Any, Any],
) -> None:
    conn, sheet_repo = db
    repo, row = prepare(conn, sheet_repo)
    conn.execute("INSERT INTO vl_campaigns(advertiser,campaign) VALUES ('test','other')")
    conn.execute("UPDATE vl_videos SET caption='test other #PR'")
    row["caption"] = "test other #PR"
    projected = application_prediction(
        result(evidence=[dict(field="caption", quote="test other")]),
        repo.load()[0],
        Post.from_row(row),
    )
    assert repo.save(row, projected, "applied", "ambiguous", "haiku", repo.load()[0])
    assert effective(conn)["advertiser"] == "test"
    assert effective(conn)["campaign"] is None and effective(conn)["case_inferred"] is True
    assert search_campaigns(conn, {}, admin=True) == []
    batch = seed_batch()
    batch.videos[0].caption = "test other #PR"
    sheet_repo.save(batch, request_id="resync-ambiguous", started_at=NOW)
    assert effective(conn)["advertiser"] == "test" and effective(conn)["campaign"] is None


def test_initial_aliases_change_view_candidates_and_truth_without_rewriting_sheet(
    db: tuple[Any, Any],
) -> None:
    conn, sheet_repo = db
    repo, _ = prepare(conn, sheet_repo)
    assert conn.execute("SELECT count(*) AS n FROM vl_case_aliases").fetchone()["n"] == 0
    conn.execute(
        "INSERT INTO vl_campaigns(advertiser,campaign) VALUES ('TikTok','20260717'),('トップバリュ','昭和レトロ')"
    )
    assert register_initial_aliases(repo, decided_by="operator") == len(INITIAL_ALIASES)
    assert register_initial_aliases(repo, decided_by="other") == 0
    assert {
        r["decided_by"] for r in conn.execute("SELECT decided_by FROM vl_case_aliases").fetchall()
    } == {"operator"}
    conn.execute(
        "UPDATE vl_videos SET advertiser='TikTok',campaign='20260717',caption='ラフォーレ セール'"
    )
    # A different human decision cannot override a complete, aliased sheet label.
    conn.execute("""INSERT INTO vl_video_campaign_links
        (platform,video_id,advertiser,campaign,source,status,match_type,confidence,model,prompt_version,input_hash,decided_by)
        VALUES ('tiktok','1','test','test','human','applied','existing',1,'human','v1','h','admin')""")
    assert (effective(conn)["advertiser"], effective(conn)["campaign"]) == ("ラフォーレ", "セール")
    assert effective(conn)["case_inferred"] is False
    assert conn.execute("SELECT advertiser,campaign FROM vl_videos").fetchone() == {
        "advertiser": "TikTok",
        "campaign": "20260717",
    }
    assert set((c.advertiser, c.campaign) for c in repo.load()[0]) == {
        ("test", "test"),
        ("ラフォーレ", "セール"),
        ("AEON", "昭和レトロ"),
    }
    assert [(r["advertiser"], r["campaign"]) for r in repo.evaluation_rows(conn)] == [
        ("ラフォーレ", "セール")
    ]
    conn.execute("UPDATE vl_videos SET advertiser='トップバリュ',campaign='昭和レトロ'")
    assert (effective(conn)["advertiser"], effective(conn)["campaign"]) == ("AEON", "昭和レトロ")
    assert [(r["advertiser"], r["campaign"]) for r in repo.evaluation_rows(conn)] == [
        ("AEON", "昭和レトロ")
    ]
    assert (
        conn.execute(
            "SELECT to_campaign FROM vl_case_aliases WHERE from_advertiser='トップバリュ'"
        ).fetchone()["to_campaign"]
        is None
    )
    conn.execute(
        "UPDATE vl_case_aliases SET to_advertiser='corrected' WHERE from_advertiser='TikTok'"
    )
    assert register_initial_aliases(repo, decided_by="operator") == 0
    assert (
        conn.execute(
            "SELECT to_advertiser FROM vl_case_aliases WHERE from_advertiser='TikTok'"
        ).fetchone()["to_advertiser"]
        == "corrected"
    )


def test_canonical_ai_link_is_existing_without_creating_new_master(db: tuple[Any, Any]) -> None:
    conn, sheet_repo = db
    repo, row = prepare(conn, sheet_repo)
    conn.execute("INSERT INTO vl_campaigns(advertiser,campaign) VALUES ('TikTok','20260717')")
    register_initial_aliases(repo, decided_by="operator")
    assert repo.save(
        row,
        result(advertiser="ラフォーレ", campaign="セール"),
        "applied",
        "hash",
        "haiku",
        repo.load()[0],
    )
    assert effective(conn)["advertiser"] == "ラフォーレ" and effective(conn)["campaign"] == "セール"
    assert effective(conn)["case_inferred"] is True
    assert repo.evaluation_rows(conn) == []  # An AI-filled case is never a ground-truth label.
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM vl_campaigns WHERE advertiser='ラフォーレ'"
        ).fetchone()["n"]
        == 0
    )


def test_alias_settings_retain_privacy_for_sheet_and_historical_decisions(
    db: tuple[Any, Any],
) -> None:
    conn, sheet_repo = db
    repo, row = prepare(conn, sheet_repo)
    conn.execute("INSERT INTO vl_campaigns(advertiser,campaign) VALUES ('TikTok','20260717')")
    register_initial_aliases(repo, decided_by="operator")
    conn.execute("""INSERT INTO vl_campaign_settings
        (advertiser,campaign,business_category,is_private,industry,purpose,updated_by)
        VALUES ('TikTok','20260717','media',true,'old industry','old purpose','admin'),
               ('ラフォーレ','セール','tto',false,'canonical industry','canonical purpose','admin')""")
    conn.execute("UPDATE vl_videos SET advertiser='TikTok',campaign='20260717'")
    assert effective(conn)["is_private"] is True
    assert effective(conn)["industry"] == "canonical industry"
    assert effective(conn)["business_category"] == "tto"
    conn.execute("UPDATE vl_videos SET advertiser=NULL,campaign=NULL")
    assert repo.save(
        row,
        result(advertiser="TikTok", campaign="20260717"),
        "applied",
        "hash",
        "haiku",
        repo.load()[0],
    )
    assert effective(conn)["advertiser"] == "ラフォーレ" and effective(conn)["campaign"] == "セール"
    assert effective(conn)["is_private"] is True
    conn.execute(
        "UPDATE vl_campaign_settings SET industry=NULL,purpose=NULL,business_category=NULL WHERE advertiser='ラフォーレ'"
    )
    assert (
        effective(conn)["industry"] == "old industry"
        and effective(conn)["purpose"] == "old purpose"
    )
    assert effective(conn)["business_category"] == "media"


def test_alias_rls_matches_human_settings(db: tuple[Any, Any]) -> None:
    conn, sheet_repo = db
    repo, _ = prepare(conn, sheet_repo)
    register_initial_aliases(repo, decided_by="operator")
    conn.execute("SELECT set_config('app.user_role','viewer',true)")
    assert conn.execute("SELECT count(*) AS n FROM vl_case_aliases").fetchone()["n"] == len(
        INITIAL_ALIASES
    )
    assert conn.execute("UPDATE vl_case_aliases SET to_advertiser='bad'").rowcount == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("INSERT INTO vl_case_aliases VALUES ('a','b','c',NULL,'viewer',now())")
    with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
        conn.execute("DELETE FROM vl_case_aliases")
    flags = conn.execute(
        "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='vl_case_aliases'::regclass"
    ).fetchone()
    assert flags == {"relrowsecurity": True, "relforcerowsecurity": True}
