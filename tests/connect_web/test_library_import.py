"""PR2 HTTP flow with real intake/observation functions and offline acquisition."""

from __future__ import annotations

import itertools
import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from teamagent.connect_web import library
from teamagent.connect_web.app import create_app
from teamagent.connect_web.library_import import batch_state
from teamagent.ingest.video_library_url_import import process_url
from tests.connect_web.test_library import Pg
from tests.connect_web.test_search_routes import _EMAIL, _auth_cookie, _config
from tests.ingest.test_video_library_url_import import NOW, URL, item, page

ORIGIN = {"origin": "http://testserver"}
BATCH = "8c82b064-9fe0-40e7-86c9-cc58e10a1bae"


class ImportPg(Pg):
    def __init__(self) -> None:
        super().__init__()
        self.batches: dict[str, dict[str, Any]] = {}
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.campaigns: list[dict[str, Any]] = []
        self.paused = False
        self.pause_reason = "http_429"
        self.paused_at = NOW
        self.resumed_by: str | None = None
        self.resumed_at: Any = None
        self.consecutive_failures = 5
        self.events: list[str] = []

    def execute(self, query: str, params: Any = None) -> MagicMock:
        result = super().execute(query, params)
        result.fetchone.return_value = None
        if "INSERT INTO vl_url_import_batches" in query:
            batch, email, key, source, advertiser, campaign, parent, requested_origin = params
            existing = next((b for b in self.batches.values() if b["idempotency_key"] == key), None)
            if existing is None:
                self.batches[batch] = dict(
                    batch_id=batch,
                    requested_by=email,
                    idempotency_key=key,
                    source=source,
                    requested_origin=requested_origin,
                    advertiser=advertiser,
                    campaign=campaign,
                    parent_batch_id=parent,
                    created_at=NOW,
                )
                self.rows[batch] = []
                result.fetchone.return_value = {"batch_id": batch}
                self.events.append("submit")
        elif "INSERT INTO vl_url_import_rows" in query:
            batch, line, url, canonical, vid, duplicate, status, error, registration = params
            self.rows[batch].append(
                dict(
                    line_no=line,
                    input_url=url,
                    canonical_url=canonical,
                    video_id=vid,
                    duplicate_of=duplicate,
                    status=status,
                    error_code=error,
                    registration_result=registration,
                    observation=None,
                )
            )
        elif "SELECT * FROM vl_url_import_batches" in query:
            if "idempotency_key=%s" in query:
                batch = next(
                    (
                        b
                        for b in self.batches.values()
                        if (b["requested_by"], b["idempotency_key"]) == tuple(params)
                    ),
                    None,
                )
            else:
                batch = self.batches.get(params[0])
                if batch and not (batch["requested_by"] == params[1] or params[2]):
                    batch = None
            result.fetchone.return_value = batch.copy() if batch else None
        elif "FROM vl_url_import_rows" in query:
            result.fetchall.return_value = [r.copy() for r in self.rows.get(str(params[0]), [])]
        elif "SELECT batch_id,created_at FROM vl_url_import_batches" in query:
            result.fetchall.return_value = [
                b for b in self.batches.values() if b["requested_by"] == params[0]
            ]
        elif "FROM vl_campaigns" in query:
            matches = [
                r
                for r in self.campaigns
                if params is None or (r["advertiser"], r["campaign"]) == tuple(params)
            ]
            result.fetchall.return_value = matches
            result.fetchone.return_value = matches[0] if matches else None
        elif "INSERT INTO vl_campaigns" in query:
            advertiser, campaign, email = params
            self.campaigns.append(dict(advertiser=advertiser, campaign=campaign, created_by=email))
        elif "UPDATE vl_public_fetch_control" in query and "resumed_by=%s" in query:
            result.rowcount = int(self.paused)
            if self.paused:
                self.paused = False
                self.consecutive_failures = 0
                self.resumed_by, self.resumed_at = params[0], NOW
                self.events.append("resume")
        elif "FROM vl_public_fetch_control" in query:
            result.fetchone.return_value = {
                "paused": self.paused,
                "pause_reason": self.pause_reason,
                "paused_at": self.paused_at,
            }
        elif "SELECT video_id FROM vl_videos_effective" in query:
            result.fetchall.return_value = [{"video_id": "123"}]
        elif "SELECT 1 FROM vl_videos" in query:
            result.fetchone.return_value = {"exists": True} if params[0] == "123" else None
        return result


@pytest.fixture
def intake(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, ImportPg]:
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", "")
    pg = ImportPg()
    client = TestClient(create_app(search_config=_config(), admin_pg=pg))
    client.cookies.update(_auth_cookie())
    return client, pg


def post(client: TestClient, text: str = URL, **fields: str) -> httpx.Response:
    return client.post(
        "/library/import",
        headers=ORIGIN,
        data={"urls": text, "action": "check", "idempotency_key": "key", **fields},
        follow_redirects=False,
    )


def test_viewer_intake_preview_worker_result_retry(intake: tuple[TestClient, ImportPg]) -> None:
    client, pg = intake
    worker = MagicMock()
    worker.wake.side_effect = lambda: pg.events.append("wake")
    client.app.state.video_library_import_worker = worker  # type: ignore[union-attr]
    text = (
        URL
        + "\n"
        + URL
        + "?tracking=1\nhttps://www.youtube.com/watch?v=1\n<script>\nhttps://vt.tiktok.com/short/"
    )
    response = post(client, text)
    assert response.status_code == 200
    for expected in ("登録済み（統合済み）", "重複", "対象外", "不正", "短縮URLは処理時に展開"):
        assert expected in response.text
    assert "<script>\n" not in response.text
    assert not pg.batches and not worker.wake.called
    response = post(client, text, action="submit")
    assert response.status_code == 303
    assert pg.events == ["submit", "wake"]
    batch = response.headers["location"].split("/")[-1]
    assert pg.batches[batch]["requested_by"] == _EMAIL
    assert post(client, text, action="submit").headers["location"].endswith(batch)
    assert len(pg.batches) == 1
    assert post(client, URL, action="submit").status_code == 409
    assert "受付" in client.get(response.headers["location"]).text
    # Real parser/fetch operation; only storage and database acquisition are fake.
    repo = MagicMock()
    repo.thumbnail_missing.return_value = False
    repo.save.side_effect = lambda observation, **kwargs: kwargs["result"]
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=page(item())))
    ) as http:
        outcome = process_url(
            URL, repo, http, MagicMock(), source="url_manual", requested_by=_EMAIL, daily_limit=300
        )
    assert outcome.status == "succeeded"
    pg.rows[batch][0].update(status="succeeded", registration_result=outcome.registration_result)
    pg.rows[batch][-1].update(status="failed", error_code="network_error")
    result = client.get(response.headers["location"])
    soup = BeautifulSoup(result.text, "html.parser")
    # Each status word appears once per result cell (not "失敗 失敗"), and the state is a heading.
    for cell in soup.select("tbody td:nth-of-type(3)"):
        words = cell.get_text(" ", strip=True).split()
        assert not any(a == b for a, b in itertools.pairwise(words)), words
    assert soup.select_one('[role="status"] h2') is not None
    assert soup.select_one('a[href="/library/v/tiktok/123"]')
    assert "一部失敗" in result.text and 'data-import-poll="false"' in result.text
    assert "通信に失敗" in result.text
    assert batch in client.get("/library/import").text
    retry = client.post(
        response.headers["location"] + "/retry",
        headers=ORIGIN,
        data={"idempotency_key": "retry"},
        follow_redirects=False,
    )
    assert retry.status_code == 303
    child = retry.headers["location"].split("/")[-1]
    assert len(pg.rows[child]) == 1 and pg.rows[child][0]["input_url"].endswith("/short/")
    assert pg.batches[child]["parent_batch_id"] == batch


def test_all_import_endpoints_require_authentication(intake: tuple[TestClient, ImportPg]) -> None:
    client, pg = intake
    client.cookies.clear()
    for method, path in [
        ("GET", "/library/import"),
        ("POST", "/library/import"),
        ("POST", "/library/import/campaign"),
        ("GET", f"/library/import/{BATCH}"),
        ("POST", f"/library/import/{BATCH}/retry"),
    ]:
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == 303
    assert not pg.calls


@pytest.mark.parametrize(
    "headers,data,expected",
    [
        ({}, {"urls": URL}, 403),
        ({"origin": "https://evil.test"}, {"urls": URL}, 403),
        ({"origin": "http://testserver", "content-type": "application/json"}, "{}", 415),
        (ORIGIN, {"urls": "\n".join([URL] * 101)}, 400),
        (ORIGIN, {"urls": URL, "requested_by": "forged@example.test"}, 400),
    ],
)
def test_csrf_limits_and_fields_before_db(
    intake: tuple[TestClient, ImportPg], headers: Any, data: Any, expected: int
) -> None:
    client, pg = intake
    response = client.post("/library/import", headers=headers, data=data)
    assert response.status_code == expected
    assert not pg.calls


def test_import_body_limit_duplicate_fields_and_large_valid_form(
    intake: tuple[TestClient, ImportPg],
) -> None:
    client, pg = intake
    headers = {**ORIGIN, "content-type": "application/x-www-form-urlencoded"}
    assert (
        client.post("/library/import", headers=headers, content="urls=a&urls=b").status_code == 400
    )
    assert (
        client.post(
            "/library/import", headers=headers, content="urls=" + ("a" * 1_048_577)
        ).status_code
        == 413
    )
    assert not pg.calls
    assert (
        post(client, "\n".join(URL + f"?q={i}" + "a" * 100 for i in range(100))).status_code == 200
    )


def test_new_campaign_creator_and_selection(intake: tuple[TestClient, ImportPg]) -> None:
    client, pg = intake
    response = client.post(
        "/library/import/campaign",
        headers=ORIGIN,
        data={"advertiser": "広告主 <img>", "campaign": "新規案件", "urls": URL},
    )
    assert response.status_code == 200 and not BeautifulSoup(response.text, "html.parser").select(
        "img"
    )
    assert pg.campaigns[0]["created_by"] == _EMAIL
    assert "表記揺れ" in response.text
    choice = json.dumps(["広告主 <img>", "新規案件"], ensure_ascii=False)
    response = post(client, action="submit", case=choice, requested_origin="own")
    assert response.status_code == 303
    assert next(iter(pg.batches.values()))["advertiser"] == "広告主 <img>"
    assert (
        post(client, case=json.dumps(["hidden", "secret"]), requested_origin="own").status_code
        == 400
    )


def test_owner_results_and_pause_quota_polling(intake: tuple[TestClient, ImportPg]) -> None:
    client, pg = intake
    response = post(client, action="submit")
    location = response.headers["location"]
    batch = location.split("/")[-1]
    html = client.get(location).text
    assert "停止中" in html and "workerが無効" in html
    pg.rows[batch][0].update(status="deferred", error_code="daily_limit")
    html = client.get(location).text
    assert "日次上限" in html and 'data-import-poll="false"' in html
    pg.paused = True
    assert "取得先の制限により停止" in client.get(location).text
    pg.batches[batch]["requested_by"] = "other@example.test"
    assert client.get(location).status_code == 404
    assert (
        client.post(location + "/retry", headers=ORIGIN, data={"idempotency_key": "x"}).status_code
        == 404
    )


@pytest.mark.parametrize(
    "status,paused,enabled,state,poll",
    [
        ("queued", False, True, "受付", True),
        ("processing", False, True, "処理中", True),
        ("succeeded", False, True, "完了", False),
        ("failed", False, True, "一部失敗", False),
        ("queued", True, True, "停止中", False),
        ("deferred", False, True, "停止中", False),
    ],
)
def test_batch_states(status: str, paused: bool, enabled: bool, state: str, poll: bool) -> None:
    assert batch_state([{"status": status}], paused=paused, enabled=enabled) == (state, poll)


def test_badges_filters_and_public_detail(intake: tuple[TestClient, ImportPg]) -> None:
    client, pg = intake
    row = {
        **pg.row,
        "origin": "external",
        "advertiser": None,
        "campaign": None,
        "is_pr": False,
        "business_category": "organic",
        "page_is_ad": True,
        "is_external_case": True,
        "case_inferred": False,
    }
    card = library._video_card(row)
    assert all(label in card for label in ("他社事例", "広告配信あり", "ORGANIC"))
    assert "一般投稿" not in library._video_card({**row, "is_pr": True})
    for field in ("pure_organic", "general", "url_import"):
        where, values, _, _ = library._where({field: "yes"}, False)
        assert "NOT v.is_private" in where
        assert not values
    pure = library._where({"pure_organic": "yes"}, False)[0]
    assert all(
        condition in pure
        for condition in (
            "NOT v.is_pr",
            "pv.page_is_ad=false",
            "pv.page_disclosure->'disclosures'='[]'::jsonb",
        )
    )
    assert "一般投稿" in library.archive_home([], [], [row])
    assert 'id="general"' not in library.archive_home([], [])
    # Restore the base fake's detail query behavior.
    pg.execute = lambda query, params=None: Pg.execute(pg, query, params)  # type: ignore[method-assign]
    pg.row.update(
        row,
        metric_source="public_page",
        captured_at=NOW,
        page_observed_at=NOW,
        page_disclosure={"caption": "投稿 <img> #PR", "disclosures": ["#ad"]},
    )
    response = client.get("/library/v/tiktok/123")
    assert response.status_code == 200
    assert "公開ページの数値（取得日時:" in response.text and "#PR・広告の根拠" in response.text
    assert not BeautifulSoup(response.text, "html.parser").select(".page-evidence img")
    assert not BeautifulSoup(response.text, "html.parser").select(".metric-group.paid")


@pytest.mark.parametrize("admin", [False, True])
def test_paused_intake_only_admin_can_resume(
    intake: tuple[TestClient, ImportPg], monkeypatch: pytest.MonkeyPatch, admin: bool
) -> None:
    client, pg = intake
    pg.paused = True
    pg.pause_reason = "challenge"
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", _EMAIL if admin else "")
    worker = MagicMock()
    worker.wake.side_effect = lambda: pg.events.append("wake")
    client.app.state.video_library_import_worker = worker  # type: ignore[union-attr]
    response = client.get("/library/import")
    assert "取得停止中" in response.text and "認証確認" in response.text
    assert str(NOW) in response.text and "停止した時刻" in response.text
    assert ("取得を再開" in response.text) is admin
    assert ("管理者に再開を依頼してください" in response.text) is not admin
    response = client.post(
        "/library/import/resume", headers=ORIGIN, data={"action": "resume"}, follow_redirects=False
    )
    assert response.status_code == (303 if admin else 403)
    if admin:
        assert not pg.paused and pg.consecutive_failures == 0
        assert pg.resumed_by == _EMAIL and pg.resumed_at == NOW
        assert pg.events == ["resume", "wake"]
        assert (
            client.post(
                "/library/import/resume",
                headers=ORIGIN,
                data={"action": "resume"},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert pg.events == ["resume", "wake"]  # Repeated POST preserves the audit record.
    else:
        assert pg.paused and not pg.events


def test_resume_requires_origin_and_cookie(
    intake: tuple[TestClient, ImportPg], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, pg = intake
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", _EMAIL)
    pg.paused = True
    for headers in ({}, {"origin": "https://evil.test"}):
        assert client.post("/library/import/resume", headers=headers, data={}).status_code == 403
    assert pg.paused and not pg.events
    client.cookies.clear()
    assert (
        client.post(
            "/library/import/resume",
            headers=ORIGIN,
            data={"action": "resume"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert pg.paused and not pg.events


def test_429_result_polls_until_automatic_resume_but_403_does_not(
    intake: tuple[TestClient, ImportPg],
) -> None:
    client, pg = intake
    client.app.state.video_library_import_worker = MagicMock()  # type: ignore[union-attr]
    location = post(client, action="submit").headers["location"]
    pg.paused = True
    assert 'data-import-poll="true"' in client.get(location).text
    pg.pause_reason = "http_403"
    assert 'data-import-poll="false"' in client.get(location).text
    pg.paused = False
    assert 'data-import-poll="true"' in client.get(location).text
