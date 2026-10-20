from pathlib import Path

from tests.connect_web.test_admin_usage import _OTHER, _OWNER, _client, _cookies


def test_quality_route_requires_admin_and_uses_existing_login_return(monkeypatch) -> None:
    from teamagent.connect_web.app import _safe_next

    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", _OWNER)
    client, pg = _client()
    response = client.get("/admin/quality", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/search/login?next=/admin"
    assert _safe_next("/admin") == "/admin"
    assert client.get("/admin/quality", cookies=_cookies(_OTHER)).status_code == 404
    assert not pg.connection_kwargs


def test_quality_missing_report_is_not_a_zero_or_confirmed_value(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", _OWNER)
    monkeypatch.setenv("DM_QUALITY_REPORT_PATH", str(tmp_path / "missing.json"))
    client, _ = _client()
    response = client.get("/admin/quality", cookies=_cookies(_OWNER))
    assert response.status_code == 200
    assert "未計測" in response.text
    assert "0.0%" not in response.text and "確定値" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_quality_page_reads_only_aggregate_and_hides_rate_when_incomplete(
    monkeypatch, tmp_path
) -> None:
    from teamagent.dm_quality import summarize, write_report
    from tests.adapters.test_dm_quality_reader import END, START, ReadOnlySlack, extract

    report = summarize(extract(ReadOnlySlack()), {}, start=START, end=END, observed_until=END)
    path = tmp_path / "quality.json"
    write_report(path, report)
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", _OWNER)
    monkeypatch.setenv("DM_QUALITY_REPORT_PATH", str(path))
    client, pg = _client()
    response = client.get("/admin/quality", cookies=_cookies(_OWNER))
    assert "欠測" in response.text and "確定値" not in response.text
    assert "合成依頼" not in response.text and "example.com" not in response.text
    assert "全DM良い率" in response.text
    assert not pg.connection_kwargs
