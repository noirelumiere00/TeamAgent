"""skill から描画への取得日時の受け渡し（T20）と、Slack の要約の文言（T27）。

- generated_at（JST）は skill が render_slides・render_report・render_pptx に渡す。描画側は
  out.generated_at を自分では読まないので、skill の引数を外すと日時が消える（＝このテストが赤）。
- Slack: 共通点を「勝ち筋」と呼ばない。段階の名前はコードの集計。クライアント未指定なら最後に
  作り直しの案内を 1 行。PPTX は「画像のパワポ（文字の修正はHTML版で）」。URL は行末（#463）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.schema import VideoAlgorithmInput, VideoAlgorithmOutput
from teamagent.skills.video_algorithm.skill import CLIENT_MISSING_NOTE, VideoAlgorithmSkill
from tests.skills.video_algorithm.prod_shape import (
    CLIENT,
    COMPETITORS,
    QUERY,
    prod_board,
    prod_videos,
)

STAMP = "2026-09-28T10:15:00+09:00"
SHOWN = "2026-09-28 10:15（JST）"


def _out(**kw: Any) -> VideoAlgorithmOutput:
    videos, board = prod_videos(), prod_board()
    return VideoAlgorithmOutput(
        query=QUERY,
        videos=videos,
        board=board,
        cross=cross_analyze(videos, QUERY, board=board),
        generated_at=STAMP,
        **kw,
    )


def test_skill_passes_generated_at_to_the_report(tmp_path: Path) -> None:
    skill = VideoAlgorithmSkill(report_dir=str(tmp_path))
    path = skill._write_report(_out(), "req1", str(tmp_path))
    assert path is not None
    html = Path(path).read_text(encoding="utf-8")
    assert f"取得 {SHOWN}" in html


def test_skill_passes_generated_at_to_the_slides(tmp_path: Path) -> None:
    written: list[str] = []

    def fake_pub(path: str, *, request_id: str, query: str) -> str | None:
        written.append(Path(path).read_text(encoding="utf-8"))
        return "https://signed.example/slides"

    skill = VideoAlgorithmSkill(publisher=fake_pub, report_dir=str(tmp_path))
    out = _out()
    skill._build_proposal_outputs(
        out, VideoAlgorithmInput(query=QUERY, outputs=["slides"]), "req1", str(tmp_path)
    )
    assert out.slides_url == "https://signed.example/slides"
    assert SHOWN in written[0]
    assert f"順位は{SHOWN}時点" in written[0]  # フッタ


def test_skill_passes_generated_at_to_the_pptx_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """media worker 経路（HTML を渡す）とローカル経路（render_pptx）の両方。"""
    from teamagent.adapters import media_job
    from teamagent.skills.video_algorithm import pptx_export

    seen: dict[str, Any] = {}

    class FakeClient:
        @staticmethod
        def is_configured() -> bool:
            return True

        def slides_to_pptx(self, html: str, **kw: Any) -> bytes:
            seen["html"] = html
            return b"PK"

    monkeypatch.setattr(media_job, "MediaJobClient", FakeClient)
    skill = VideoAlgorithmSkill(
        publisher=lambda path, *, request_id, query: "https://signed.example/p",
        report_dir=str(tmp_path),
    )
    assert skill._build_pptx(_out(), str(tmp_path), "kw", "req1") == "https://signed.example/p"
    assert SHOWN in seen["html"]

    class LocalClient:
        @staticmethod
        def is_configured() -> bool:
            return False

        @staticmethod
        def local_runtime_enabled() -> bool:
            return True

    def fake_render_pptx(out: VideoAlgorithmOutput, path: str, **kw: Any) -> str:
        seen["kw"] = kw
        Path(path).write_bytes(b"PK")
        return path

    monkeypatch.setattr(media_job, "MediaJobClient", LocalClient)
    monkeypatch.setattr(pptx_export, "render_pptx", fake_render_pptx)
    assert skill._build_pptx(_out(), str(tmp_path), "kw", "req2") == "https://signed.example/p"
    assert seen["kw"] == {"generated_at": STAMP}


def _summary(**kw: Any) -> str:
    out = _out(**kw)
    out.cross.video_count = 5
    return VideoAlgorithmSkill._slack_summary(object.__new__(VideoAlgorithmSkill), out)


def test_slack_never_calls_the_shared_trait_a_winning_path() -> None:
    """T27: 壊し方: 旧文言（最有力の勝ち筋）に戻す → 赤。"""
    summary = _summary(report_url="https://signed.example/r/eyJ.abc")
    assert "勝ち筋" not in summary and "勝ちパターン" not in summary
    assert "必須条件『最初のテロップが0秒台』（5/5本）" in summary
    # 横断の要約の 1 行も段階の名前つき（analysis.cross_analyze の summary）
    assert re.search(r"最も多い共通点は『[^』]+』（(必須条件|多数派) \d/5本）", summary)
    assert re.search(r"多数派『[^』]+』（[34]/5本）", summary)


def test_slack_asks_for_the_client_only_when_missing() -> None:
    missing = _summary()
    assert missing.splitlines()[-1] == CLIENT_MISSING_NOTE
    assert "動画の再分析なし" in CLIENT_MISSING_NOTE
    named = _summary(client_name=CLIENT, competitors=COMPETITORS)
    assert CLIENT_MISSING_NOTE not in named


def test_slack_pptx_wording_and_urls_at_line_end() -> None:
    summary = _summary(
        report_url="https://signed.example/r/eyJ.abc",
        slides_url="https://signed.example/r/eyJ.def",
        pptx_url="https://signed.example/r/eyJ.ghi",
    )
    assert "📊 画像のパワポ（文字の修正はHTML版で・7日有効）: https://signed.example/r/eyJ.ghi" in (
        summary
    )
    for line in summary.splitlines():
        if "https://" in line:
            assert re.search(r"https://[\x21-\x7e]+$", line), line
