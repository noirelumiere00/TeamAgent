"""video_algorithm 後段連携（該当秒フレーム）のテスト。重い skill は差し替えて呼ばない。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from teamagent.skills._html.report import Report, render_report
from teamagent.skills.tiktok_search import deep


@dataclass
class _Frame:
    sec: float
    data_uri: str
    caption: str = ""


@dataclass
class _Meta:
    author: str = "yossy"
    url: str = "https://www.tiktok.com/@yossy/video/1"


@dataclass
class _Analysis:
    hook_type: str = "number"
    hook_summary: str = "19時帰宅でも10分"


@dataclass
class _Video:
    meta: _Meta = field(default_factory=_Meta)
    analysis: _Analysis | None = field(default_factory=_Analysis)
    frames: list[_Frame] = field(default_factory=list)


@dataclass
class _Result:
    videos: list[_Video] = field(default_factory=list)


_IMG = "data:image/jpeg;base64,AAAA"


class _Ctx:
    request_id = "r"

    def bind_logger(self, name: str) -> Any:  # pragma: no cover - 使わない
        raise AssertionError


def _install(monkeypatch: pytest.MonkeyPatch, result: _Result | Exception) -> list[Any]:
    calls: list[Any] = []

    class _Fake:
        def run(self, input: Any, ctx: Any) -> Any:
            calls.append(input)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(
        "teamagent.skills.video_algorithm.skill.VideoAlgorithmSkill", lambda *a, **k: _Fake()
    )
    return calls


@pytest.fixture(autouse=True)
def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_HTML_REPORT_FRAMES", "1")


class TestGate:
    def test_flag_off_does_not_run_the_heavy_skill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("USE_HTML_REPORT_FRAMES", raising=False)
        calls = _install(monkeypatch, _Result([_Video(frames=[_Frame(0.0, _IMG)])]))
        assert deep.build_filmstrips("春巻の皮", _Ctx()) == ([], 0.0)
        assert calls == []

    def test_does_not_ask_video_algorithm_for_its_own_outputs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 欲しいのは分析とフレームだけ。あちら側の HTML/slides/pptx を作らせない。
        calls = _install(monkeypatch, _Result([_Video(frames=[_Frame(0.0, _IMG)])]))
        deep.build_filmstrips("春巻の皮", _Ctx())
        assert calls[0].outputs == []
        assert calls[0].max_videos == deep._MAX_VIDEOS


class TestMapping:
    def test_frames_become_a_filmstrip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            _Result([_Video(frames=[_Frame(0.0, _IMG), _Frame(1.5, _IMG, "テロップ登場")])]),
        )
        strips, _cost = deep.build_filmstrips("春巻の皮", _Ctx())
        assert len(strips) == 1
        assert strips[0].title == "@yossy"
        assert strips[0].subtitle == "冒頭フック: 数字提示 — 19時帰宅でも10分"
        assert [f.label for f in strips[0].frames] == ["0.0秒", "1.5秒"]
        assert strips[0].frames[1].caption == "テロップ登場"

    def test_frame_count_is_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(
            monkeypatch,
            _Result([_Video(frames=[_Frame(float(i), _IMG) for i in range(20)])]),
        )
        assert len(deep.build_filmstrips("q", _Ctx())[0][0].frames) == deep._MAX_FRAMES

    def test_video_without_frames_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _Result([_Video(frames=[]), _Video(frames=[_Frame(0.0, _IMG)])]))
        assert len(deep.build_filmstrips("q", _Ctx())[0]) == 1

    def test_empty_data_uri_is_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _Result([_Video(frames=[_Frame(0.0, "")])]))
        assert deep.build_filmstrips("q", _Ctx()) == ([], 0.0)

    def test_missing_analysis_yields_no_subtitle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, _Result([_Video(analysis=None, frames=[_Frame(0.0, _IMG)])]))
        assert deep.build_filmstrips("q", _Ctx())[0][0].subtitle == ""

    def test_failure_is_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, RuntimeError("gemini down"))
        assert deep.build_filmstrips("q", _Ctx()) == ([], 0.0)


class TestRendering:
    def _strips(self, monkeypatch: pytest.MonkeyPatch) -> Report:
        _install(monkeypatch, _Result([_Video(frames=[_Frame(0.0, _IMG), _Frame(2.5, _IMG)])]))
        return Report(title="T", filmstrips=deep.build_filmstrips("q", _Ctx())[0])

    def test_frames_render_with_seconds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        html = render_report(self._strips(monkeypatch))
        assert html.count("<img class='cut'") == 2
        assert "0.0秒" in html and "2.5秒" in html

    def test_frame_keeps_aspect_ratio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        html = render_report(self._strips(monkeypatch))
        assert ".strip figure img{width:104px;height:auto" in html
        assert "height=" not in html

    def test_svg_data_uri_is_rejected(self) -> None:
        # SVG は中でスクリプトを実行し得るので画像として通さない。
        from teamagent.skills._html.report import Filmstrip, Frame

        html = render_report(
            Report(
                title="T",
                filmstrips=[
                    Filmstrip(title="@x", frames=[Frame("0秒", "data:image/svg+xml;base64,AAAA")])
                ],
            )
        )
        assert "svg" not in html
        assert "class='strip'" not in html

    def test_link_to_original_video(self, monkeypatch: pytest.MonkeyPatch) -> None:
        html = render_report(self._strips(monkeypatch))
        assert "https://www.tiktok.com/@yossy/video/1" in html


class TestCostPropagation:
    """入れ子の有料分析コストを捨てない（usage台帳とレポート表示の過少申告を防ぐ）。"""

    def test_cost_is_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = _Result([_Video(frames=[_Frame(0.0, _IMG)])])
        result.total_cost_usd = 0.42  # type: ignore[attr-defined]
        _install(monkeypatch, result)
        _strips, cost = deep.build_filmstrips("q", _Ctx())
        assert cost == 0.42

    def test_cost_is_returned_even_without_frames(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # フレームが1枚も取れなくても課金は発生している。0 と報告してはいけない。
        result = _Result([_Video(frames=[])])
        result.total_cost_usd = 0.31  # type: ignore[attr-defined]
        _install(monkeypatch, result)
        strips, cost = deep.build_filmstrips("q", _Ctx())
        assert strips == [] and cost == 0.31

    def test_failure_reports_zero_cost(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, RuntimeError("down"))
        assert deep.build_filmstrips("q", _Ctx()) == ([], 0.0)
