"""スライドHTML生成と PPTX 発行（media worker 経由）のテスト。"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.adapters.report_publish import PublishedObject
from teamagent.skills._html.report import Cell, Chip, Column, Report, Section, Table
from teamagent.skills._html.slides import _ROWS_PER_SLIDE, render_slides
from teamagent.skills._shared import report_pptx


def _report(rows: int = 3) -> Report:
    return Report(
        title="春巻の皮 — TikTok 上位 10 本",
        headline="包まないほど伸びている",
        subtitle="検索面の上位動画メタ",
        chips=[Chip("本数", "10"), Chip("空", "")],
        body_md="### サマリ\n時短が通底している",
        sections=[Section("勝ちパターン", "- 時短 8/10本"), Section("空セクション", "")],
        tables=[
            Table(
                columns=[Column("#"), Column(""), Column("再生", "right")],
                rows=[
                    [Cell(str(i)), Cell("", image="https://x/y.jpg"), Cell("180万")]
                    for i in range(rows)
                ],
                caption="上位動画",
            )
        ],
    )


class TestSlides:
    def test_one_slide_per_section_plus_cover_and_table(self) -> None:
        html = render_slides(_report())
        # 表紙 + 中身のあるセクション1枚 + 表1枚（空セクションは描かない）
        assert html.count("class='slide'") == 3

    def test_slide_size_matches_pptx_conversion(self) -> None:
        html = render_slides(_report())
        assert "width:1280px" in html
        assert "height:720px" in html

    def test_image_column_is_dropped_in_slides(self) -> None:
        # スクショ変換なので、幅固定のサムネ列（ラベル空）は表から落とす。
        # 残すと空列ぶんだけ幅を食い、他の列が痩せる。
        html = render_slides(_report())
        assert "<img" not in html
        header_cells = html.count("<th>") + html.count("<th class")
        assert header_cells == 2  # 「#」と「再生」だけ（空ラベル列は出さない）
        assert html.count("<td") == 2 * 3  # 3行 × 2列

    def test_table_is_paginated(self) -> None:
        html = render_slides(_report(rows=_ROWS_PER_SLIDE * 2 + 1))
        assert html.count("class='slide'") == 2 + 3  # 表紙 + セクション + 表3枚
        assert "（1/3）" in html

    def test_confidentiality_on_every_slide(self) -> None:
        html = render_slides(_report())
        assert html.count("社外共有不可") == 3

    def test_escapes_untrusted_text(self) -> None:
        report = Report(title="<script>x</script>", body_md="本文")
        assert "<script>" not in render_slides(report)


class _Spy:
    def __init__(self, body: bytes = b"PK\x03\x04") -> None:
        self.html = ""
        self.body = body

    def slides_to_pptx(self, html: str, **kw: Any) -> bytes:
        self.html = html
        return self.body


def _install(monkeypatch: pytest.MonkeyPatch, spy: _Spy, configured: bool = True) -> None:
    monkeypatch.setattr(
        "teamagent.adapters.media_job.MediaJobClient.is_configured",
        classmethod(lambda cls: configured),
    )
    monkeypatch.setattr(
        "teamagent.adapters.media_job.MediaJobClient.__new__", lambda cls, *a, **k: spy
    )
    monkeypatch.setattr(
        "teamagent.adapters.report_publish.publish_bytes_result",
        lambda body, **kw: PublishedObject(url="u", bucket="b", key="vseo-reports/x.pptx"),
    )
    monkeypatch.setattr(
        report_pptx, "delivery_url", lambda result, request_id: f"https://short/{result.key}"
    )


class TestPublishPptx:
    def test_flag_off_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("USE_HTML_REPORT_PPTX", raising=False)
        spy = _Spy()
        _install(monkeypatch, spy)
        assert report_pptx.publish_pptx(_report(), tool="t", request_id="r") is None
        assert spy.html == ""

    def test_publishes_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORT_PPTX", "1")
        spy = _Spy()
        _install(monkeypatch, spy)
        url = report_pptx.publish_pptx(_report(), tool="t", request_id="r")
        assert url == "https://short/vseo-reports/x.pptx"
        assert "class='slide'" in spy.html

    def test_unconfigured_media_worker_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORT_PPTX", "1")
        _install(monkeypatch, _Spy(), configured=False)
        assert report_pptx.publish_pptx(_report(), tool="t", request_id="r") is None

    def test_media_failure_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORT_PPTX", "1")

        class _Boom(_Spy):
            def slides_to_pptx(self, html: str, **kw: Any) -> bytes:
                raise RuntimeError("media down")

        _install(monkeypatch, _Boom())
        assert report_pptx.publish_pptx(_report(), tool="t", request_id="r") is None

    def test_empty_artifact_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_HTML_REPORT_PPTX", "1")
        _install(monkeypatch, _Spy(body=b""))
        assert report_pptx.publish_pptx(_report(), tool="t", request_id="r") is None


class TestPrintCss:
    """PDF は印刷経由で出す方針なので、印刷CSSが落ちていないことを固定する。"""

    def test_print_rules_exist(self) -> None:
        from teamagent.skills._html.report import render_report

        html = render_report(_report())
        assert "@media print" in html
        assert "size:A4" in html
        assert "display:table-header-group" in html  # 表の見出し行を各ページに出す
        assert "print-color-adjust:exact" in html  # ピル/バーの色を落とさない
