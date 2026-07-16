"""レポート配信URL決定（短縮URL /r か presigned か）の単体テスト。

背景（2026-07-15 実機事故）: openclaw の LLM が presigned のクエリを書き落とし、裸S3 URL が
営業に渡って AccessDenied になった。短縮URL(/r)はその根治だが、前提（鍵・BASE_URL）が欠けると
**無言で** presigned に落ちて「フラグONしたのに直らない」を招く。ここではその無言化の防止
（＝欠けた前提を名指しで警告する）を固定する。
"""

from __future__ import annotations

from typing import Any

import pytest
from structlog.testing import capture_logs

from teamagent.adapters.report_publish import PublishedObject
from teamagent.skills._shared.report_delivery import delivery_url, short_url_enabled

_OBJ = PublishedObject(
    url="https://b.s3.amazonaws.com/vseo-reports/x.html?AWSAccessKeyId=A&Signature=S&Expires=1",
    bucket="b",
    key="vseo-reports/x.html",
    region="ap-northeast-1",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("USE_REPORT_SHORTURL", "MAIL_ACTION_HMAC_SECRET", "CONNECT_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def _prereq_warnings(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "report_short_url_prereq_missing"]


def test_flag_off_returns_presigned_silently() -> None:
    """既定OFF＝短縮URL化せず presigned。意図した無音なので警告も出さない（後方互換）。"""
    assert short_url_enabled() is False
    with capture_logs() as logs:
        assert delivery_url(_OBJ, request_id="r1") == _OBJ.url
    assert _prereq_warnings(logs) == []


def test_flag_on_without_secret_warns_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """本命: フラグONでも鍵(MAIL_ACTION_HMAC_SECRET)が無ければ presigned へ。

    ただし**黙って落ちない**＝欠けた前提を名指しで warning する。これが無いと terraform apply
    忘れ（＝鍵未注入）に気づけず「ONにしたのに直らない」が延々続く。
    """
    monkeypatch.setenv("USE_REPORT_SHORTURL", "1")
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example")
    with capture_logs() as logs:
        assert delivery_url(_OBJ, request_id="r1") == _OBJ.url  # fail-open（配信は止めない）
    warns = _prereq_warnings(logs)
    assert len(warns) == 1
    assert warns[0]["log_level"] == "warning"
    assert "MAIL_ACTION_HMAC_SECRET" in warns[0]["missing"]  # 何が足りないかを名指し


def test_flag_on_without_base_url_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """CONNECT_BASE_URL 未設定も同様に名指しで警告して presigned へ。"""
    monkeypatch.setenv("USE_REPORT_SHORTURL", "1")
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "s3cret")
    with capture_logs() as logs:
        assert delivery_url(_OBJ, request_id="r1") == _OBJ.url
    assert "CONNECT_BASE_URL" in _prereq_warnings(logs)[0]["missing"]


def test_flag_on_disallowed_key_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """allowlist 外 prefix はトークンを出すと 404 になるので発行せず警告して presigned へ。"""
    monkeypatch.setenv("USE_REPORT_SHORTURL", "1")
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "s3cret")
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example")
    bad = PublishedObject(url=_OBJ.url, bucket="b", key="secrets/x.html", region="ap-northeast-1")
    with capture_logs() as logs:
        assert delivery_url(bad, request_id="r1") == bad.url
    assert "key_prefix_not_allowed" in _prereq_warnings(logs)[0]["missing"]


def test_missing_prereqs_are_all_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """前提が複数欠けたら全部を列挙する（1つ直して再挑戦…の往復を避ける）。"""
    monkeypatch.setenv("USE_REPORT_SHORTURL", "1")
    with capture_logs() as logs:
        delivery_url(_OBJ, request_id="r1")
    missing = _prereq_warnings(logs)[0]["missing"]
    assert "CONNECT_BASE_URL" in missing and "MAIL_ACTION_HMAC_SECRET" in missing


def test_all_prereqs_met_returns_short_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """全条件充足＝クエリ無しの /r/<token>。これが openclaw に壊されない形。"""
    monkeypatch.setenv("USE_REPORT_SHORTURL", "1")
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "s3cret")
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example")
    url = delivery_url(_OBJ, request_id="r1")
    assert url.startswith("https://connect.example/r/")
    assert "?" not in url  # ← クエリが無いことが本質（LLMが削る対象を作らない）


def _report_skills() -> list[tuple[str, Any]]:
    """HTMLレポートURLを @AiLa 経由で人へ渡す skill の _publish_html 相当を集める。"""
    from teamagent.skills.search_surface_check.skill import SearchSurfaceCheckSkill
    from teamagent.skills.tiktok_comment_mining.skill import TikTokCommentMiningSkill
    from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill
    from teamagent.skills.x_research.skill import XVoiceSearchSkill

    return [
        ("x_research", XVoiceSearchSkill()._publish_html),
        ("tiktok_comment_mining", TikTokCommentMiningSkill()._publish_html),
        ("search_surface_check", SearchSurfaceCheckSkill()._publish_html),
        # video_algorithm は _publish（引数が位置指定）なのでシグネチャを合わせて包む
        (
            "video_algorithm",
            lambda html, *, request_id, query: VideoAlgorithmSkill()._publish(
                _tmp_html(html), request_id, query
            ),
        ),
    ]


def _tmp_html(html: str) -> str:
    import tempfile

    path = tempfile.mktemp(suffix=".html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


@pytest.fixture
def _fake_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    """S3を叩かずに PublishedObject を返す（publisher 注入せず実経路を通すため）。"""
    monkeypatch.setenv("VSEO_REPORT_BUCKET", "b")
    monkeypatch.setattr(
        "teamagent.adapters.report_publish.publish_html_file_result",
        lambda path, *, request_id="vseo", query="": _OBJ,
    )


@pytest.mark.parametrize("name", [n for n, _ in _report_skills()])
def test_report_skills_fall_back_to_presigned_when_flag_off(
    name: str, _fake_publish: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既定OFF: 全レポート skill が従来どおり presigned をそのまま返す（後方互換）。"""
    fn = dict(_report_skills())[name]
    assert fn("<html></html>", request_id="r1", query="q") == _OBJ.url


@pytest.mark.parametrize("name", [n for n, _ in _report_skills()])
def test_report_skills_emit_short_url_when_prereqs_met(
    name: str, _fake_publish: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本命の回帰: 前提充足時、**全てのレポート skill** が /r 短縮URLを返す。

    #213 は x_research にしか短縮URL化を入れておらず、tiktok/surface/video_algorithm は
    presigned のままだった＝同じ事故（openclaw がクエリを落として裸URL化）が残っていた。
    ここは文字列 grep ではなく実経路を走らせて固定する。
    """
    monkeypatch.setenv("USE_REPORT_SHORTURL", "1")
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "s3cret")
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example")
    fn = dict(_report_skills())[name]
    url = fn("<html></html>", request_id="r1", query="q")
    assert url.startswith("https://connect.example/r/"), f"{name} が短縮URL化されていない"
    assert "?" not in url


def test_encode_failure_falls_back_to_presigned(monkeypatch: pytest.MonkeyPatch) -> None:
    """encode が壊れても配信は止めない（fail-open）＋警告は残す。"""

    def _boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("boom")

    monkeypatch.setenv("USE_REPORT_SHORTURL", "1")
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "s3cret")
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example")
    monkeypatch.setattr("teamagent.adapters.report_link_token.encode_report_token", _boom)
    with capture_logs() as logs:
        assert delivery_url(_OBJ, request_id="r1") == _OBJ.url
    assert any(e["event"] == "report_short_url_encode_failed" for e in logs)


def test_empty_key_is_rejected_not_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """空 key は allowlist を素通りさせない（素通りさせると decode 不能な /r を発行し 404）。"""
    monkeypatch.setenv("USE_REPORT_SHORTURL", "1")
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "s3cret")
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example")
    empty = PublishedObject(url=_OBJ.url, bucket="b", key="", region="ap-northeast-1")
    with capture_logs() as logs:
        assert delivery_url(empty, request_id="r1") == empty.url
    assert "key_prefix_not_allowed" in _prereq_warnings(logs)[0]["missing"]
