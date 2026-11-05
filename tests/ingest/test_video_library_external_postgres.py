"""0041 origin precedence, external isolation, audit/RLS, inference and shared shelves.

Uses only explicit TEAMAGENT_TEST_DB_DSN and the disposable schema fixture.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from teamagent.connect_web.app import create_app
from teamagent.connect_web.library import recent_imports, search_campaigns, search_videos
from teamagent.ingest.video_library_case_inference import Inference, run
from teamagent.ingest.video_library_case_repository import CaseRepository
from teamagent.ingest.video_library_url_models import ImportResult, normalize_url, parse_observation
from tests.connect_web.test_library_import import ORIGIN
from tests.connect_web.test_search_routes import _EMAIL, _auth_cookie, _config
from tests.ingest.test_video_library_case_inference import client as fake_bedrock
from tests.ingest.test_video_library_url_import import NOW, URL, item, page
from tests.ingest.test_video_library_url_import_postgres import _DSN, db

__all__ = ["db"]
pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEAMAGENT_TEST_DB_DSN")


def seed(conn: Any, video_id: str = "123", *, pr: bool = True) -> None:
    conn.execute(
        """INSERT INTO vl_videos(platform,video_id,url,account_handle,caption,is_pr,origin,ingest_sources)
        VALUES ('tiktok',%s,%s,'sample','MOFTの新商品 #PR',%s,'external','{url_manual}')""",
        (video_id, URL.replace("123", video_id), pr),
    )


def decide(conn: Any, origin: str, video_id: str = "123") -> None:
    conn.execute(
        """INSERT INTO vl_video_origin_decisions(decision_id,platform,video_id,origin,decided_by)
        VALUES (%s,'tiktok',%s,%s,%s)""",
        (uuid.uuid4(), video_id, origin, _EMAIL),
    )


def effective(conn: Any, video_id: str = "123") -> Any:
    return conn.execute(
        "SELECT * FROM vl_videos_effective WHERE video_id=%s", (video_id,)
    ).fetchone()


@pytest.mark.parametrize(
    "operator", ["NewsTV", "ベクトル（VCL）", "ベクトル九州", "イニシャル", "gracemode"]
)
def test_0041_three_ownership_conditions_and_priority(db: tuple[Any, Any], operator: str) -> None:
    _, repo = db
    with repo.connection() as conn:
        seed(conn)
        assert effective(conn)["origin"] == "external"
        decide(conn, "own")
        assert effective(conn)["origin"] == "own"  # Commissioned KOL, no sheet/account required.
        decide(conn, "external")
        assert effective(conn)["origin"] == "external"
        conn.execute(
            "INSERT INTO vl_accounts(platform,handle,operator_label) VALUES ('tiktok','@SAMPLE',%s)",
            (operator,),
        )
        assert effective(conn)["origin"] == "own"  # Account beats human external.
        conn.execute("UPDATE vl_accounts SET operator_label='他社'")
        assert effective(conn)["origin"] == "external"
        conn.execute("UPDATE vl_videos SET origin='own'")  # Unchanged sheet writer/trigger.
        assert (
            "sheet"
            in conn.execute("SELECT ingest_sources FROM vl_videos").fetchone()["ingest_sources"]
        )
        decide(conn, "external")
        assert effective(conn)["origin"] == "own"  # Sheet beats human external too.
        assert (
            conn.execute("SELECT count(*) AS n FROM vl_video_origin_decisions").fetchone()["n"] == 3
        )


def test_0041_external_with_campaign_excluded_from_own_cases_and_totals(
    db: tuple[Any, Any],
) -> None:
    _, repo = db
    with repo.connection() as conn:
        conn.execute("INSERT INTO vl_campaigns(advertiser,campaign) VALUES ('MOFT','case')")
        seed(conn)
        conn.execute("UPDATE vl_videos SET advertiser='MOFT',campaign='case'")
        assert effective(conn)["is_external_case"] is True
        assert search_campaigns(conn, {}, admin=True) == []
        assert search_videos(conn, {"external_case": "yes"}, admin=True)["total"] == 1
        assert search_videos(conn, {}, admin=True)["videos"][0]["campaign_total_plays"] is None
        seed(conn, "124")
        decide(conn, "own", "124")
        conn.execute("UPDATE vl_videos SET advertiser='MOFT',campaign='case' WHERE video_id='124'")
        conn.execute(
            "INSERT INTO vl_public_metrics(platform,video_id,captured_on,captured_at,total_plays,metric_observation) VALUES ('tiktok','123',current_date,now(),999,'{}'),('tiktok','124',current_date,now(),10,'{}')"
        )
        assert search_campaigns(conn, {}, admin=True)[0]["video_count"] == 1
        assert search_campaigns(conn, {}, admin=True)[0]["total_plays"] == 10
        result = search_videos(conn, {}, admin=True)["videos"]
        assert {r["video_id"]: r["campaign_total_plays"] for r in result} == {
            "123": None,
            "124": 10,
        }


def test_0041_external_pr_ad_and_general_tags(db: tuple[Any, Any]) -> None:
    _, repo = db
    with repo.connection() as conn:
        seed(conn, pr=False)
        assert not effective(conn)["is_external_case"]
        conn.execute("UPDATE vl_videos SET page_is_ad=true")
        assert effective(conn)["is_external_case"]
        conn.execute("UPDATE vl_videos SET page_is_ad=false")
        conn.execute(
            "INSERT INTO vl_metrics(platform,video_id,captured_on,ad_impressions) VALUES ('tiktok','123',current_date,10)"
        )
        assert effective(conn)["is_external_case"]
        assert search_videos(conn, {"general": "yes"}, admin=True)["total"] == 0


def test_0041_external_advertiser_ai_human_and_origin_race(
    db: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    pg, repo = db
    with repo.connection() as conn:
        seed(conn)
    cases = CaseRepository(pg)
    raw = dict(
        advertiser="MOFT",
        campaign=None,
        match_type="new",
        confidence=0.99,
        evidence=[dict(field="caption", quote="MOFT")],
    )
    fake = fake_bedrock(json.dumps(raw))
    assert (
        run(
            cases,
            Inference(fake, max_cost_usd=1),
            policy=None,
            limit=10,
            dry_run=False,
            request_id="external-db",
        )["applied"]
        == 1
    )
    with repo.connection() as conn:
        row = effective(conn)
        assert row["advertiser"] == row["external_advertiser"] == "MOFT"
        assert row["campaign"] is None and not row["case_inferred"]
        assert conn.execute(
            "SELECT prompt_version,source FROM vl_video_campaign_links"
        ).fetchone() == {"prompt_version": "external-v1", "source": "ai"}
        assert search_campaigns(conn, {}, admin=True) == []
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "")
    http = TestClient(create_app(search_config=_config(), admin_pg=pg))
    http.cookies.update(_auth_cookie())
    for value in ("人が修正", "再修正", ""):
        assert (
            http.post(
                "/library/v/tiktok/123/advertiser",
                headers=ORIGIN,
                data={"advertiser": value},
                follow_redirects=False,
            ).status_code
            == 303
        )
        with repo.connection() as conn:
            assert effective(conn)["external_advertiser"] == (value or None)
    assert cases.load()[1] == []  # Even a human blank blocks AI fallback/reinference.
    with repo.connection() as conn:
        conn.execute("UPDATE vl_video_campaign_links SET prompt_version='v1' WHERE source='human'")
    row = cases.load()[1][0]
    with repo.connection() as conn:
        decide(conn, "own")
    from teamagent.ingest.video_library_case_models import Prediction

    assert not cases.save(
        row, Prediction(**{**raw, "match_type": "unknown"}), "applied", "stale", "haiku", []
    )
    with repo.connection() as conn:
        assert effective(conn)["external_advertiser"] is None
        assert effective(conn)["advertiser"] is None  # External AI is never an own-case label.


def test_0041_switch_is_idempotent_audited_and_employee_rls(
    db: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pg, repo = db
    with repo.connection() as conn:
        seed(conn)
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "")
    http = TestClient(create_app(search_config=_config(), admin_pg=pg))
    http.cookies.update(_auth_cookie())
    key = str(uuid.uuid4())
    for _ in range(2):
        assert (
            http.post(
                "/library/v/tiktok/123/origin",
                headers=ORIGIN,
                data={"origin": "own", "decision_id": key},
                follow_redirects=False,
            ).status_code
            == 303
        )
    with pg.connection(app_role="teamagent_app", user_email=_EMAIL, user_role="viewer") as conn:
        assert effective(conn)["origin"] == "own"
        rows = conn.execute("SELECT * FROM vl_video_origin_decisions").fetchall()
        assert len(rows) == 1 and rows[0]["decided_by"] == _EMAIL and rows[0]["decided_at"]
        for operation in (
            "UPDATE vl_video_origin_decisions SET origin='external'",
            "DELETE FROM vl_video_origin_decisions",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(operation)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "INSERT INTO vl_video_origin_decisions VALUES (%s,'tiktok','123','external','forged',now())",
                (uuid.uuid4(),),
            )
    assert (
        http.post(
            "/library/v/tiktok/123/origin",
            headers=ORIGIN,
            data={"origin": "external", "decision_id": str(uuid.uuid4())},
            follow_redirects=False,
        ).status_code
        == 303
    )
    with repo.connection() as conn:
        assert effective(conn)["origin"] == "external"
        assert (
            conn.execute("SELECT count(*) AS n FROM vl_video_origin_decisions").fetchone()["n"] == 2
        )


def test_0041_own_intake_and_recent_shared_12_newest(db: tuple[Any, Any]) -> None:
    pg, repo = db
    with repo.connection() as conn:
        assert recent_imports(conn, admin=False) == []
    for n in range(14):
        video_id = str(100 + n)
        url = URL.replace("123", video_id)
        batch = repo.submit(
            url,
            requested_by=f"member{n}@example.test",
            idempotency_key="key",
            requested_origin="own" if n == 0 else "external",
        )
        row = repo.claim()
        assert row and str(row["batch_id"]) == batch
        observation = parse_observation(page(item(id=video_id)), normalize_url(url), NOW)
        repo.save(
            observation,
            source="url_manual",
            requested_by=row["requested_by"],
            thumbnail=None,
            result=ImportResult(status="succeeded", video_id=video_id),
            row=row,
        )
    with pg.connection(app_role="teamagent_app", user_email=_EMAIL, user_role="viewer") as conn:
        assert effective(conn, "100")["origin"] == "own"
        assert effective(conn, "101")["origin"] == "external"
        rows = recent_imports(conn, admin=False)
        assert len(rows) == 12
        assert [r["video_id"] for r in rows] == [str(i) for i in range(113, 101, -1)]
        columns = [
            d.name for d in conn.execute("SELECT * FROM vl_videos_effective LIMIT 0").description
        ]
        assert columns[-3:] == ["case_inferred", "external_advertiser", "is_external_case"]
