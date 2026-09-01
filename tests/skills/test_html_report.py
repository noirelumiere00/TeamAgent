"""共通 HTML レポートレンダラのテスト（純粋関数なので I/O なし）。

守りたい不変量:
  1. **LLM/第三者由来の文字列が構造タグにならない**（納品HTML上での任意JS実行を防ぐ）。
  2. リンクは safe_href を通ったものだけ。許可外ホストはプレーン表示へ落ちる。
  3. 行が 0 件のテーブルは描画ごと省く（空の枠だけが残ると「取得失敗」と誤読される）。
"""

from __future__ import annotations

import datetime as dt

from teamagent.skills._html.report import (
    Cell,
    Chip,
    Column,
    Report,
    Table,
    render_body,
    render_report,
)

_NOW = dt.datetime(2026, 9, 1, 10, 30, tzinfo=dt.timezone(dt.timedelta(hours=9)))


def _render(**kw: object) -> str:
    base = {"title": "テスト", "subtitle": "", "chips": [], "body_md": "", "tables": []}
    base.update(kw)
    return render_report(Report(**base), now=_NOW)  # type: ignore[arg-type]


class TestEscaping:
    def test_script_in_body_is_not_a_tag(self) -> None:
        html = _render(body_md="<script>alert(1)</script> の話")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_script_in_table_cell_is_not_a_tag(self) -> None:
        html = _render(
            tables=[
                Table(
                    columns=[Column("説明")],
                    rows=[[Cell("<img src=x onerror=alert(1)>")]],
                )
            ]
        )
        # テキストとして残るのは無害。タグとして解釈され得る形になっていないことを見る。
        assert "<img" not in html
        assert "&lt;img" in html

    def test_title_is_escaped(self) -> None:
        assert "<b>" not in _render(title="<b>太字</b>")

    def test_quotes_in_cell_cannot_break_out_of_attribute(self) -> None:
        # href に " を混ぜて属性から脱出する古典的な経路を塞げているか。
        html = _render(
            tables=[
                Table(
                    columns=[Column("a")],
                    rows=[[Cell("x", href="https://www.tiktok.com/@a/video/1' onmouseover='x")]],
                )
            ]
        )
        assert "onmouseover" not in html or "&#x27;" in html


class TestLinks:
    def test_allowed_host_becomes_link(self) -> None:
        html = _render(
            tables=[
                Table(
                    columns=[Column("a")],
                    rows=[[Cell("@who", href="https://www.tiktok.com/@who/video/123")]],
                )
            ]
        )
        assert "<a href='https://www.tiktok.com/@who/video/123'" in html

    def test_disallowed_scheme_falls_back_to_plain_text(self) -> None:
        html = _render(
            tables=[Table(columns=[Column("a")], rows=[[Cell("危険", href="javascript:alert(1)")]])]
        )
        assert "javascript:" not in html
        assert "危険" in html

    def test_unknown_https_host_is_not_linked(self) -> None:
        html = _render(
            tables=[Table(columns=[Column("a")], rows=[[Cell("外部", href="https://evil.test/x")]])]
        )
        assert "evil.test" not in html


class TestBody:
    def test_heading_and_list_and_bold(self) -> None:
        html = render_body("### 見出し\n- 一つ目\n- 二つ目\n\n**強調**です")
        assert "<h3>見出し</h3>" in html
        assert html.count("<li>") == 2
        assert "<strong>強調</strong>" in html

    def test_numbered_list_becomes_ol(self) -> None:
        assert "<ol>" in render_body("1. 最初\n2. 次")

    def test_empty_body_renders_nothing(self) -> None:
        assert render_body("   ") == ""
        assert "class='body'" not in _render(body_md="")


class TestTables:
    def test_empty_table_is_omitted(self) -> None:
        assert "<table>" not in _render(tables=[Table(columns=[Column("a")], rows=[])])

    def test_bar_width_is_bounded(self) -> None:
        html = _render(
            tables=[Table(columns=[Column("再生", "right")], rows=[[Cell("1", bar=9.9)]])]
        )
        assert "width:90.0px" in html  # 1.0 でクランプされる

    def test_tone_renders_pill(self) -> None:
        html = _render(
            tables=[Table(columns=[Column("率", "right")], rows=[[Cell("2.8%", tone="ok")]])]
        )
        assert "class='pill ok'" in html


class TestPage:
    def test_light_theme_only_no_dark_media_query(self) -> None:
        # x_research と同じ判断（OSダークで白文字が消える納品事故）。分岐を足さないことを固定する。
        assert "prefers-color-scheme" not in _render()

    def test_no_external_asset_and_no_script(self) -> None:
        html = _render(body_md="本文")
        assert "http://" not in html
        assert "<script" not in html
        assert "cdn" not in html.lower()

    def test_footer_carries_stamp_and_confidentiality(self) -> None:
        html = _render()
        assert "2026-09-01 10:30" in html
        assert "社外共有不可" in html

    def test_chip_with_empty_value_is_dropped(self) -> None:
        html = _render(chips=[Chip("件数", ""), Chip("種別", "keyword")])
        assert "件数" not in html
        assert "keyword" in html
