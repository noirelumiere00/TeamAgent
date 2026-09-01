"""HTML レポート発行口（段階ゲート・fail-open）のテスト。

守りたい不変量:
  1. フラグ OFF では **S3 を一切触らない**（既存挙動と 1 バイトも変えない）。
  2. ツール単位で開けられる（カンマ区切り）。
  3. 発行の失敗はレポートを諦めるだけで、例外を呼び出し側へ伝播させない。
  4. 一時ファイルを残さない。
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from teamagent.adapters.report_publish import PublishedObject
from teamagent.skills._html.report import Report
from teamagent.skills._shared import report_html

_REPORT = Report(title="テスト", body_md="本文")


@pytest.fixture(autouse=True)
def _bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VSEO_REPORT_BUCKET", "test-bucket")
    monkeypatch.delenv("USE_HTML_REPORTS", raising=False)


class _Spy:
    """publish_html_file_result の差し替え。呼ばれた path を記録する。"""

    def __init__(self, result: PublishedObject | None) -> None:
        self.calls: list[str] = []
        self.result = result
        self.html = ""

    def __call__(self, path: str, **_: Any) -> PublishedObject | None:
        self.calls.append(path)
        with open(path, encoding="utf-8") as fh:
            self.html = fh.read()
        return self.result


def _install(monkeypatch: pytest.MonkeyPatch, spy: _Spy) -> None:
    monkeypatch.setattr(
        "teamagent.adapters.report_publish.publish_html_file_result", spy, raising=True
    )
    monkeypatch.setattr(
        report_html, "delivery_url", lambda result, request_id: f"https://short/{result.key}"
    )


class TestGate:
    def test_flag_off_does_not_publish(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spy = _Spy(PublishedObject(url="u", bucket="b", key="k"))
        _install(monkeypatch, spy)
        assert report_html.publish_report(_REPORT, tool="tiktok_search", request_id="r") is None
        assert spy.calls == []

    def test_flag_true_publishes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORTS", "true")
        spy = _Spy(PublishedObject(url="u", bucket="b", key="vseo-reports/x.html"))
        _install(monkeypatch, spy)
        url = report_html.publish_report(_REPORT, tool="tiktok_search", request_id="r")
        assert url == "https://short/vseo-reports/x.html"
        assert len(spy.calls) == 1

    def test_per_tool_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORTS", "tiktok_search, video_analysis")
        assert report_html.html_reports_enabled("tiktok_search") is True
        assert report_html.html_reports_enabled("video_analysis") is True
        assert report_html.html_reports_enabled("proposal_draft") is False

    def test_no_bucket_means_no_publish(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORTS", "1")
        monkeypatch.delenv("VSEO_REPORT_BUCKET", raising=False)
        spy = _Spy(PublishedObject(url="u", bucket="b", key="k"))
        _install(monkeypatch, spy)
        assert report_html.publish_report(_REPORT, tool="tiktok_search", request_id="r") is None
        assert spy.calls == []


class TestFailOpen:
    def test_publish_returning_none_yields_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORTS", "1")
        _install(monkeypatch, _Spy(None))
        assert report_html.publish_report(_REPORT, tool="t", request_id="r") is None

    def test_exception_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORTS", "1")

        def boom(path: str, **_: Any) -> PublishedObject:
            raise RuntimeError("S3 down")

        monkeypatch.setattr(
            "teamagent.adapters.report_publish.publish_html_file_result", boom, raising=True
        )
        assert report_html.publish_report(_REPORT, tool="t", request_id="r") is None


class TestTempFile:
    def test_temp_file_is_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORTS", "1")
        spy = _Spy(PublishedObject(url="u", bucket="b", key="k"))
        _install(monkeypatch, spy)
        report_html.publish_report(_REPORT, tool="t", request_id="r")
        assert spy.calls and not os.path.exists(spy.calls[0])

    def test_uploaded_file_is_the_rendered_html(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORTS", "1")
        spy = _Spy(PublishedObject(url="u", bucket="b", key="k"))
        _install(monkeypatch, spy)
        report_html.publish_report(_REPORT, tool="t", request_id="r")
        assert "<!doctype html>" in spy.html
        assert "本文" in spy.html


class TestRlsDerivedGate:
    """社内ナレッジ由来の本文は、会社共有モードでない限り URL 化しない（payload_offload と同じ線）。"""

    def test_strict_mode_skips_publish(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORTS", "1")
        monkeypatch.delenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", raising=False)
        spy = _Spy(PublishedObject(url="u", bucket="b", key="k"))
        _install(monkeypatch, spy)
        assert (
            report_html.publish_report(
                _REPORT, tool="proposal_draft", request_id="r", rls_derived=True
            )
            is None
        )
        assert spy.calls == []

    def test_company_shared_mode_publishes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORTS", "1")
        monkeypatch.setenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", "example.co.jp")
        spy = _Spy(PublishedObject(url="u", bucket="b", key="k"))
        _install(monkeypatch, spy)
        assert (
            report_html.publish_report(
                _REPORT, tool="proposal_draft", request_id="r", rls_derived=True
            )
            is not None
        )

    def test_public_data_is_unaffected_by_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # tiktok_search は公開データで RLS を通っていない＝STRICT でも発行してよい。
        monkeypatch.setenv("USE_HTML_REPORTS", "1")
        monkeypatch.delenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", raising=False)
        spy = _Spy(PublishedObject(url="u", bucket="b", key="k"))
        _install(monkeypatch, spy)
        assert report_html.publish_report(_REPORT, tool="tiktok_search", request_id="r") is not None
