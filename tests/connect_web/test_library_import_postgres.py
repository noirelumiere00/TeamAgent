"""URL import UI with PostgreSQL transactions/RLS and fake public HTTP only."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from teamagent.connect_web.app import create_app
from teamagent.connect_web.library import (
    _METRIC_SELECT,
    _METRICS_JOIN,
    search_campaigns,
    search_videos,
)
from teamagent.ingest.video_library import METRIC_COLUMNS
from teamagent.ingest.video_library_url_models import ImportResult, URLImportError
from teamagent.ingest.video_library_url_worker import drain_queue
from tests.connect_web.test_search_routes import _EMAIL, _auth_cookie, _config
from tests.ingest.test_video_library_url_import import NOW, URL, item, page
from tests.ingest.test_video_library_url_import_postgres import _DSN, db, save, sheet

__all__ = ["db"]
pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEAMAGENT_TEST_DB_DSN")
ORIGIN = {"origin": "http://testserver"}


def test_viewer_campaign_intake_worker_results_and_creation_rls(
    db: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pg, repo = db
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "")
    monkeypatch.setattr("teamagent.ingest.video_library_url_repository.time.sleep", lambda _: None)
    client = TestClient(create_app(search_config=_config(), admin_pg=pg))
    client.cookies.update(_auth_cookie())
    worker = MagicMock()
    client.app.state.video_library_import_worker = worker  # type: ignore[union-attr]
    assert (
        client.post(
            "/library/import/campaign",
            headers=ORIGIN,
            data={"advertiser": "advertiser", "campaign": "campaign"},
        ).status_code
        == 200
    )
    choice = json.dumps(["advertiser", "campaign"], ensure_ascii=False)
    text = URL + "\n" + URL.replace("123", "124")
    response = client.post(
        "/library/import",
        headers=ORIGIN,
        data={
            "urls": text,
            "action": "submit",
            "idempotency_key": "key",
            "case": choice,
            "requested_origin": "own",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    batch = response.headers["location"].split("/")[-1]
    assert worker.wake.call_count == 1

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host != "www.tiktok.com":
            return httpx.Response(200, content=b"\xff\xd8\xffimage")
        vid = request.url.path.split("/")[-1]
        return httpx.Response(
            200,
            text=page(item(id=vid, desc="#PR" if vid == "123" else "一般投稿", isAd=vid == "123")),
        )

    storage = MagicMock()
    storage.put.return_value = "thumbnail"
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        assert drain_queue(repo, http, storage) == 2
    result = client.get(response.headers["location"])
    assert result.status_code == 200 and 'data-import-poll="false"' in result.text
    assert "完了" in result.text and "/library/v/tiktok/123" in result.text
    with pg.connection(app_role="teamagent_app", user_email=_EMAIL, user_role="viewer") as conn:
        assert conn.execute("SELECT created_by,created_via FROM vl_campaigns").fetchone() == {
            "created_by": _EMAIL,
            "created_via": "url_import",
        }
        assert conn.execute(
            "SELECT advertiser,campaign FROM vl_videos WHERE video_id='123'"
        ).fetchone() == {"advertiser": "advertiser", "campaign": "campaign"}
        assert conn.execute(
            "SELECT advertiser,campaign FROM vl_videos WHERE video_id='124'"
        ).fetchone() == {"advertiser": None, "campaign": None}
        assert (
            conn.execute(
                "SELECT requested_by FROM vl_url_import_batches WHERE batch_id=%s", (batch,)
            ).fetchone()["requested_by"]
            == _EMAIL
        )
        assert len(search_campaigns(conn, {}, admin=False)) == 1
        assert search_videos(conn, {"general": "yes"}, admin=False)["total"] == 0
        assert (
            search_videos(conn, {"pure_organic": "yes"}, admin=False)["videos"][0]["video_id"]
            == "124"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "INSERT INTO vl_campaigns VALUES ('forged','campaign','other',now(),'url_import')"
            )
        assert conn.execute("UPDATE vl_campaigns SET created_by='forged'").rowcount == 0
    # The same key with different contents cannot silently reuse a receipt.
    with pytest.raises(URLImportError, match="idempotency_conflict"):
        repo.submit(URL, requested_by=_EMAIL, idempotency_key="key")


def test_sheet_snapshot_priority_rates_column_contract_and_public_only(db: tuple[Any, Any]) -> None:
    pg, repo = db
    save(repo, desc="general", isAd=False)
    with repo.connection() as conn:
        before = [
            (c.name, c.type_code)
            for c in conn.execute("SELECT * FROM vl_videos_effective LIMIT 0").description
        ]
        rows = search_videos(conn, {"url_import": "yes", "sort": "saves"}, admin=False)["videos"]
        assert len(rows) == 1 and rows[0]["metric_source"] == "public_page"
        assert rows[0]["total_plays"] == 100 and rows[0]["rates"]["save_rate"] == 0.04
        assert rows[0]["organic_plays"] is None and rows[0]["ad_views"] is None
        assert len(search_videos(conn, {"pure_organic": "yes"}, admin=False)["videos"]) == 1
        # isAd alone remains organic and is excluded from pure organic.
        conn.execute("UPDATE vl_videos SET page_is_ad=true")
        assert search_videos(conn, {}, admin=False)["videos"][0]["business_category"] == "organic"
        assert search_videos(conn, {"pure_organic": "yes"}, admin=False)["total"] == 0
        conn.execute(
            'UPDATE vl_videos SET page_is_ad=false,page_disclosure=\'{"disclosures":["#ad"]}\''
        )
        assert search_videos(conn, {"pure_organic": "yes"}, admin=False)["total"] == 0
    sheet(pg)
    save(repo, at=NOW + timedelta(days=2), desc="url", isAd=True)
    with repo.connection() as conn:
        row = search_videos(conn, {}, admin=False)["videos"][0]
        assert row["metric_source"] == "sheet" and row["total_plays"] == 15
        assert row["organic_plays"] == 11 and row["ad_cost"] == 200
        assert row["total_saves"] is None  # No per-column fallback from public.
        cursor = conn.execute(
            f"SELECT {_METRIC_SELECT} FROM vl_videos_effective v {_METRICS_JOIN} LIMIT 0"
        )
        columns = [c.name for c in cursor.description]
        assert columns[: len(METRIC_COLUMNS)] == list(METRIC_COLUMNS)
        assert len(columns) == len(set(columns))
        assert [
            (c.name, c.type_code)
            for c in conn.execute("SELECT * FROM vl_videos_effective LIMIT 0").description
        ] == before


def test_assignment_does_not_move_existing_and_private_choice_at_processing(
    db: tuple[Any, Any],
) -> None:
    _, repo = db
    with repo.connection() as conn:
        conn.execute(
            "INSERT INTO vl_campaigns(advertiser,campaign) VALUES ('one','case'),('two','case')"
        )
    batch = repo.submit(
        URL,
        requested_by=_EMAIL,
        idempotency_key="one",
        requested_origin="own",
        advertiser="one",
        campaign="case",
    )
    row = repo.claim()
    assert row and str(row["batch_id"]) == batch
    from teamagent.ingest.video_library_url_models import normalize_url, parse_observation

    observation = parse_observation(page(item(desc="#PR")), normalize_url(URL), NOW)
    repo.save(
        observation,
        source="url_manual",
        requested_by=_EMAIL,
        thumbnail=None,
        result=ImportResult(status="succeeded", video_id="123"),
        row=row,
    )
    repo.submit(
        URL,
        requested_by=_EMAIL,
        idempotency_key="two",
        requested_origin="own",
        advertiser="two",
        campaign="case",
    )
    row = repo.claim()
    assert row
    repo.save(
        observation,
        source="url_manual",
        requested_by=_EMAIL,
        thumbnail=None,
        result=ImportResult(status="succeeded", video_id="123"),
        row=row,
    )
    with repo.connection() as conn:
        assert conn.execute("SELECT advertiser FROM vl_videos").fetchone()["advertiser"] == "one"
    repo.submit(
        URL.replace("123", "124"),
        requested_by=_EMAIL,
        idempotency_key="private",
        requested_origin="own",
        advertiser="two",
        campaign="case",
    )
    row = repo.claim()
    assert row
    with repo.connection() as conn:
        conn.execute(
            "INSERT INTO vl_campaign_settings(advertiser,campaign,is_private,updated_by) VALUES ('two','case',true,'admin')"
        )
    observation = parse_observation(
        page(item(id="124", desc="#PR")), normalize_url(URL.replace("123", "124")), NOW
    )
    result = repo.save(
        observation,
        source="url_manual",
        requested_by=_EMAIL,
        thumbnail=None,
        result=ImportResult(status="succeeded", video_id="124"),
        row=row,
    )
    assert result.status == "partial" and result.error_code == "campaign_unavailable"
    with repo.connection() as conn:
        assert (
            conn.execute("SELECT advertiser FROM vl_videos WHERE video_id='124'").fetchone()[
                "advertiser"
            ]
            is None
        )


def test_private_alias_target_is_not_an_intake_choice(db: tuple[Any, Any]) -> None:
    _, repo = db
    with repo.connection() as conn:
        conn.execute(
            "INSERT INTO vl_campaigns(advertiser,campaign) VALUES ('source','case'),('target','case')"
        )
        conn.execute("""INSERT INTO vl_case_aliases
            (from_advertiser,from_campaign,to_advertiser,to_campaign,decided_by)
            VALUES ('source','case','target','case','admin')""")
        conn.execute("""INSERT INTO vl_campaign_settings
            (advertiser,campaign,is_private,updated_by) VALUES ('target','case',true,'admin')""")
        assert repo.campaigns(conn) == []
    with pytest.raises(URLImportError, match="campaign_unavailable"):
        repo.submit(
            URL,
            requested_by=_EMAIL,
            idempotency_key="private-alias",
            requested_origin="own",
            advertiser="source",
            campaign="case",
        )


def test_admin_resume_commits_audit_and_unblocks_worker(
    db: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pg, repo = db
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", _EMAIL)
    monkeypatch.setattr("teamagent.ingest.video_library_url_repository.time.sleep", lambda _: None)
    repo.submit(URL, requested_by=_EMAIL, idempotency_key="resume")
    repo.record_fetch_outcome(success=False, stop="challenge")
    assert repo.claim() is None
    with repo.connection() as conn:
        paused_at = conn.execute("SELECT paused_at FROM vl_public_fetch_control").fetchone()[
            "paused_at"
        ]
        assert paused_at is not None
    client = TestClient(create_app(search_config=_config(), admin_pg=pg))
    client.cookies.update(_auth_cookie())
    worker = MagicMock()

    def after_commit() -> None:
        state = repo.fetch_control()  # An independent connection must see the committed resume.
        assert not state["paused"] and state["resumed_by"] == _EMAIL
        assert state["resumed_at"] >= paused_at and state["consecutive_failures"] == 0

    worker.wake.side_effect = after_commit
    client.app.state.video_library_import_worker = worker  # type: ignore[union-attr]
    response = client.post(
        "/library/import/resume", headers=ORIGIN, data={"action": "resume"}, follow_redirects=False
    )
    assert response.status_code == 303
    worker.wake.assert_called_once()
    storage = MagicMock()
    storage.put.return_value = "thumbnail"
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, text=page())
                if request.url.host == "www.tiktok.com"
                else httpx.Response(200, content=b"\xff\xd8\xffimage")
            )
        )
    ) as http:
        assert drain_queue(repo, http, storage) == 1
    with repo.connection() as conn:
        assert (
            conn.execute("SELECT status FROM vl_url_import_rows").fetchone()["status"]
            == "succeeded"
        )


@pytest.mark.parametrize("reason", ["http_429", "http_403", "challenge", "consecutive_failures"])
def test_auto_resume_only_expired_429(
    db: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    _, repo = db
    monkeypatch.setenv("VIDEO_LIBRARY_FETCH_429_COOLDOWN_SECONDS", "1800")
    repo.submit(URL, requested_by=_EMAIL, idempotency_key="automatic")
    repo.record_fetch_outcome(success=False, stop=reason)
    assert repo.claim() is None  # Even 429 cannot resume early.
    with repo.connection() as conn:
        conn.execute(
            "UPDATE vl_public_fetch_control SET paused_at=clock_timestamp()-interval '31 minutes'"
        )
    row = repo.claim()
    state = repo.fetch_control()
    if reason == "http_429":
        assert row is not None
        assert not state["paused"] and state["consecutive_failures"] == 0
        assert state["resumed_by"] == "automatic:http_429" and state["resumed_at"] is not None
    else:
        assert row is None and state["paused"] and state["resumed_at"] is None


def test_429_cooldown_env_and_direct_fetch_gate(
    db: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, repo = db
    monkeypatch.setenv("VIDEO_LIBRARY_FETCH_429_COOLDOWN_SECONDS", "3600")
    repo.record_fetch_outcome(success=False, stop="http_429")
    with repo.connection() as conn:
        conn.execute(
            "UPDATE vl_public_fetch_control SET paused_at=clock_timestamp()-interval '31 minutes'"
        )
    with pytest.raises(URLImportError, match="fetch_paused"):
        repo.before_request(300)
    assert repo.fetch_control()["paused"]
    monkeypatch.setenv("VIDEO_LIBRARY_FETCH_429_COOLDOWN_SECONDS", "60")
    repo.before_request(300)
    assert not repo.fetch_control()["paused"]


def test_403_cannot_be_downgraded_to_an_automatic_pause(db: tuple[Any, Any]) -> None:
    _, repo = db
    repo.record_fetch_outcome(success=False, stop="http_403")
    before = repo.fetch_control()["paused_at"]
    repo.record_fetch_outcome(success=False, stop="http_429")
    state = repo.fetch_control()
    assert state["pause_reason"] == "http_403" and state["paused_at"] == before


def test_viewer_cannot_resume_via_database(db: tuple[Any, Any]) -> None:
    pg, repo = db
    repo.record_fetch_outcome(success=False, stop="challenge")
    with pg.connection(app_role="teamagent_app", user_email=_EMAIL, user_role="viewer") as conn:
        assert not repo.resume_fetches(conn, resumed_by=_EMAIL)
    assert repo.fetch_control()["paused"]


@pytest.mark.parametrize("automatic", [False, True])
def test_thumbnails_share_pause_and_resume(
    db: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch, automatic: bool
) -> None:
    from teamagent.ingest.video_library_thumbs import fill_thumbnails

    pg, repo = db
    monkeypatch.setenv("VIDEO_LIBRARY_FETCH_429_COOLDOWN_SECONDS", "1800")
    save(repo)
    with repo.connection() as conn:
        conn.execute("UPDATE vl_videos SET thumbnail_s3_key=NULL")
    repo.record_fetch_outcome(success=False, stop="http_429" if automatic else "http_403")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return (
            httpx.Response(200, text=page())
            if request.url.host == "www.tiktok.com"
            else httpx.Response(200, content=b"\xff\xd8\xffimage")
        )

    storage = MagicMock()
    storage.put.return_value = "thumbnail"
    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        assert fill_thumbnails(pg, storage, http, limit=1) == {"fetch_paused": 1}
        assert not requests
        with repo.connection() as conn:
            if automatic:
                conn.execute(
                    "UPDATE vl_public_fetch_control SET paused_at=clock_timestamp()-interval '31 minutes'"
                )
            else:
                assert repo.resume_fetches(conn, resumed_by=_EMAIL)
        assert fill_thumbnails(pg, storage, http, limit=1) == {"attempted": 1, "saved": 1}
    assert len(requests) == 2 and not repo.fetch_control()["paused"]
