"""tiktok_search のレポート詰め替え（保存率の導出・情報設計）のテスト。"""

from __future__ import annotations

from teamagent.skills._html.report import render_report
from teamagent.skills.tiktok_search.report import _compact, build_report
from teamagent.skills.tiktok_search.schema import TikTokSearchOutput, TikTokVideoOut


def _video(rank: int, play: int, collect: int, **kw: object) -> TikTokVideoOut:
    base = {
        "rank": rank,
        "url": f"https://www.tiktok.com/@a{rank}/video/{rank}",
        "author": f"a{rank}",
        "author_followers": 1000,
        "desc": "説明1行目\n説明2行目（レシピ全文）",
        "play_count": play,
        "digg_count": 10,
        "comment_count": 1,
        "share_count": 2,
        "collect_count": collect,
        "engagement_rate": 0.04,
        "duration": 20,
        "hashtags": ["簡単レシピ"],
    }
    base.update(kw)
    return TikTokVideoOut(**base)  # type: ignore[arg-type]


def _out(*videos: TikTokVideoOut) -> TikTokSearchOutput:
    return TikTokSearchOutput(
        query="春巻の皮",
        search_type="keyword",
        count=len(videos),
        videos=list(videos),
        analysis="### サマリ\n- 包まない系が伸びている",
        model_id="gemini-2.5-flash",
        total_cost_usd=0.0009,
    )


class TestSaveRate:
    def test_high_rate_is_ok_tone(self) -> None:
        html = render_report(build_report(_out(_video(1, 100_000, 2_500))))  # 2.5%
        assert "class='pill ok'" in html
        assert "2.50%" in html

    def test_mid_rate_is_warn_tone(self) -> None:
        html = render_report(build_report(_out(_video(1, 100_000, 1_500))))  # 1.5%
        assert "class='pill warn'" in html

    def test_low_rate_is_muted_tone(self) -> None:
        html = render_report(build_report(_out(_video(1, 100_000, 100))))  # 0.1%
        assert "class='pill muted'" in html

    def test_zero_play_does_not_claim_zero_percent(self) -> None:
        # 取得漏れ（再生0）を「保存率0%」と断言しない＝データが無いことを示す。
        html = render_report(build_report(_out(_video(1, 0, 5))))
        assert "0.00%" not in html
        assert "—" in html


class TestTable:
    def test_bar_is_relative_to_max_in_the_set(self) -> None:
        report = build_report(_out(_video(1, 2_000_000, 100), _video(2, 1_000_000, 100)))
        bars = [row[2].bar for row in report.tables[0].rows]
        assert bars == [1.0, 0.5]

    def test_only_first_line_of_desc_is_used(self) -> None:
        html = render_report(build_report(_out(_video(1, 100, 1))))
        assert "説明1行目" in html
        assert "レシピ全文" not in html

    def test_video_url_is_linked(self) -> None:
        html = render_report(build_report(_out(_video(1, 100, 1))))
        assert "https://www.tiktok.com/@a1/video/1" in html


class TestHeader:
    def test_analysis_becomes_body(self) -> None:
        html = render_report(build_report(_out(_video(1, 100, 1))))
        assert "<h3>サマリ</h3>" in html

    def test_chips_carry_counts_and_model(self) -> None:
        html = render_report(build_report(_out(_video(1, 1_800_000, 1))))
        assert "180万" in html
        assert "gemini-2.5-flash" in html


class TestCompact:
    def test_units(self) -> None:
        assert _compact(1_800_000) == "180万"
        assert _compact(32_400) == "3.2万"
        assert _compact(9_999) == "9,999"


class TestSectionSplit:
    """固定フォーマットの分析文を、見出し単位のカードへ割る。"""

    _ANALYSIS = (
        "### 1. この検索結果のサマリ\n通底パターンは時短。\n\n"
        "### 2. 伸びている勝ちパターン（最大 4、頻度/再生順）\n- **時短** — 8/10本\n\n"
        "### 3. フックの型（説明文から読み取れるもの、最大 3）\n- 手間軽減\n"
    )

    def _report(self, analysis: str) -> object:
        out = _out(_video(1, 100, 1))
        out.analysis = analysis
        return build_report(out)

    def test_first_section_becomes_body(self) -> None:
        assert self._report(self._ANALYSIS).body_md.startswith("通底パターンは時短")

    def test_remaining_sections_become_cards(self) -> None:
        titles = [s.title for s in self._report(self._ANALYSIS).sections]
        assert titles == ["伸びている勝ちパターン", "フックの型"]

    def test_prompt_hints_are_stripped_from_titles(self) -> None:
        # 「（最大 4、頻度/再生順）」は生成条件であって読者向けの情報ではない。
        assert "最大" not in " ".join(s.title for s in self._report(self._ANALYSIS).sections)

    def test_content_parentheses_are_kept(self) -> None:
        report = self._report("### 1. サマリ\n本文\n\n### 2. 保存率（重要指標）\n- 中身\n")
        assert report.sections[0].title == "保存率（重要指標）"

    def test_unstructured_analysis_falls_back_to_plain_body(self) -> None:
        # プロンプト改訂で見出しが消えても、本文が失われないこと。
        report = self._report("見出しのない素の分析文。")
        assert report.sections == []
        assert report.body_md == "見出しのない素の分析文。"

    def test_sections_are_rendered_as_cards(self) -> None:
        html = render_report(self._report(self._ANALYSIS))
        assert html.count("class='sec'") == 2
        assert "伸びている勝ちパターン" in html


class TestThumbnails:
    def test_thumb_column_appears_only_when_images_exist(self) -> None:
        out = _out(_video(1, 100, 1, cover_url="https://cdn.example/a.jpg"))
        assert len(build_report(out).tables[0].columns) == 8
        with_thumbs = build_report(out, thumbs={"https://cdn.example/a.jpg": "https://r/1.jpg"})
        assert len(with_thumbs.tables[0].columns) == 9

    def test_missing_thumb_leaves_the_cell_empty(self) -> None:
        # 1本だけ取得できた場合、他の行は画像なしで崩れず並ぶ。
        out = _out(
            _video(1, 100, 1, cover_url="https://cdn.example/a.jpg"),
            _video(2, 100, 1, cover_url="https://cdn.example/b.jpg"),
        )
        report = build_report(out, thumbs={"https://cdn.example/a.jpg": "https://r/1.jpg"})
        assert report.tables[0].rows[0][1].image == "https://r/1.jpg"
        assert report.tables[0].rows[1][1].image is None

    def test_rendered_html_has_one_img_per_available_thumb(self) -> None:
        out = _out(_video(1, 100, 1, cover_url="https://cdn.example/a.jpg"))
        html = render_report(
            build_report(out, thumbs={"https://cdn.example/a.jpg": "https://r/1.jpg"})
        )
        assert html.count("<img class='thumb'") == 1
