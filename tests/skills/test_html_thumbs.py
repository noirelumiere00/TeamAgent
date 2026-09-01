"""サムネイル取得・再ホスト（SSRF/上限/縦横比）のテスト。ネットワークには出ない。"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.skills._html import thumbs
from teamagent.skills._html.report import Cell, Column, Report, Table, render_report

_OK_URL = "https://p16-common-sign.tiktokcdn.com/tos/abc~tplv-origin.image?x-expires=1"


class _FakeResp:
    def __init__(self, body: bytes, content_type: str) -> None:
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self, n: int) -> bytes:
        return self._body[:n]

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


def _net(monkeypatch: pytest.MonkeyPatch, body: bytes, content_type: str = "image/jpeg") -> None:
    monkeypatch.setattr(thumbs, "_resolves_global", lambda host: True)
    monkeypatch.setattr(thumbs, "urlopen", lambda req, timeout: _FakeResp(body, content_type))


class TestSsrf:
    def test_unknown_host_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _net(monkeypatch, b"\xff\xd8data")
        assert thumbs.fetch_image("https://evil.test/x.jpg") is None

    def test_http_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _net(monkeypatch, b"\xff\xd8data")
        assert thumbs.fetch_image("http://p16.tiktokcdn.com/x.jpg") is None

    def test_lookalike_suffix_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # "tiktokcdn.com.evil.test" を末尾一致で通さないこと。
        _net(monkeypatch, b"\xff\xd8data")
        assert thumbs.fetch_image("https://tiktokcdn.com.evil.test/x.jpg") is None

    def test_private_ip_resolution_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(thumbs, "_resolves_global", lambda host: False)
        monkeypatch.setattr(
            thumbs, "urlopen", lambda req, timeout: _FakeResp(b"\xff\xd8data", "image/jpeg")
        )
        assert thumbs.fetch_image(_OK_URL) is None

    def test_allowed_host_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _net(monkeypatch, b"\xff\xd8data")
        assert thumbs.fetch_image(_OK_URL) == (b"\xff\xd8data", "image/jpeg")


class TestLimits:
    def test_non_image_content_type_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _net(monkeypatch, b"<html>", "text/html")
        assert thumbs.fetch_image(_OK_URL) is None

    def test_oversized_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _net(monkeypatch, b"x" * (thumbs._MAX_BYTES + 1))
        assert thumbs.fetch_image(_OK_URL) is None

    def test_empty_body_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _net(monkeypatch, b"")
        assert thumbs.fetch_image(_OK_URL) is None

    def test_network_error_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(thumbs, "_resolves_global", lambda host: True)

        def boom(req: Any, timeout: int) -> Any:
            raise TimeoutError

        monkeypatch.setattr(thumbs, "urlopen", boom)
        assert thumbs.fetch_image(_OK_URL) is None


class TestRehostMany:
    def test_flag_off_does_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("USE_HTML_REPORT_THUMBS", raising=False)
        called: list[str] = []
        monkeypatch.setattr(thumbs, "rehost", lambda u, request_id: called.append(u))
        assert thumbs.rehost_many([_OK_URL], request_id="r") == {}
        assert called == []

    def test_maps_only_successful_ones(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORT_THUMBS", "1")
        monkeypatch.setattr(
            thumbs, "rehost", lambda u, request_id: None if u.endswith("b") else f"https://r/{u}"
        )
        got = thumbs.rehost_many(["a", "b"], request_id="r")
        assert got == {"a": "https://r/a"}

    def test_count_is_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORT_THUMBS", "1")
        seen: list[str] = []

        def fake(u: str, request_id: str) -> str:
            seen.append(u)
            return f"https://r/{u}"

        monkeypatch.setattr(thumbs, "rehost", fake)
        thumbs.rehost_many([str(i) for i in range(50)], request_id="r")
        assert len(seen) == thumbs._MAX_IMAGES


class TestRendering:
    def _html(self, src: str) -> str:
        return render_report(
            Report(
                title="T",
                tables=[Table(columns=[Column("")], rows=[[Cell("", image=src)]])],
            )
        )

    def test_aspect_ratio_is_preserved(self) -> None:
        # 幅だけ指定し、高さは CSS の auto に任せる＝元の縦横比のまま。
        html = self._html("https://connect.example/r/abc")
        assert "class='thumb'" in html
        assert "height:auto" in html
        assert "height=" not in html  # 高さを固定する属性を書かない

    def test_lazy_and_no_referrer(self) -> None:
        html = self._html("https://connect.example/r/abc")
        assert "loading='lazy'" in html
        assert "referrerpolicy='no-referrer'" in html

    def test_non_https_image_is_dropped(self) -> None:
        html = self._html("javascript:alert(1)")
        assert "javascript:" not in html
        assert "<img" not in html
