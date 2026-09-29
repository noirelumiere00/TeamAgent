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

from teamagent.adapters.video_algorithm_cache import VideoAlgorithmResultCache
from teamagent.skills.base import SkillContext
from teamagent.skills.video_algorithm.analysis import cross_analyze
from teamagent.skills.video_algorithm.schema import (
    VideoAlgorithmInput,
    VideoAlgorithmOutput,
    VideoMeta,
)
from teamagent.skills.video_algorithm.skill import CLIENT_MISSING_NOTE, VideoAlgorithmSkill
from teamagent.skills.video_algorithm.slides import render_slides
from tests.skills.video_algorithm.prod_shape import (
    CLIENT,
    COMPETITORS,
    QUERY,
    prod_board,
    prod_videos,
)
from tests.skills.video_algorithm.test_cost_guards import ME, _FakeGemini, _FakeS3

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
    # R2-9: 共通点は語ごとの特徴から（「検索KW 5/5」とまとめない）・保存率はスライドと同じ中央値
    assert "検索KW" not in summary
    assert "保存率中央値 0.82%" in summary and "保存率 0.8" not in summary


def test_slack_gives_no_shared_points_for_one_or_two_videos() -> None:
    """R2-10: 1〜2 本の観測を「必須条件 1/1」として Slack に出さない。"""
    out = _out()
    for v in out.videos[1:]:
        v.error = "動画取得失敗・サムネのみ軽量分析"
    out.cross = cross_analyze(out.videos, QUERY, board=out.board)
    summary = VideoAlgorithmSkill._slack_summary(object.__new__(VideoAlgorithmSkill), out)
    assert "共通点（コードの集計）" not in summary and "必須条件" not in summary
    assert "（分析できた本数が少ないため段階は付けない）" in summary


def test_slack_asks_for_the_client_only_when_missing() -> None:
    missing = _summary()
    assert missing.splitlines()[-1] == CLIENT_MISSING_NOTE
    named = _summary(client_name=CLIENT, competitors=COMPETITORS)
    assert CLIENT_MISSING_NOTE not in named


def _cached_skill(tmp_path: Path, gemini: Any, cache: Any) -> VideoAlgorithmSkill:
    metas = [VideoMeta(rank=1, url="https://example.invalid/1", desc="新宿 ランチ")]
    return VideoAlgorithmSkill(
        gemini=gemini,
        searcher=lambda query, limit, request_id: metas[:limit],
        downloader=lambda url: (b"video", "video/mp4"),
        proxy=lambda data, mime: (data, mime),
        report_dir=str(tmp_path),
        result_cache=cache,
    )


def _run_input(client: str | None = None) -> VideoAlgorithmInput:
    return VideoAlgorithmInput(
        query="新宿 ランチ", max_videos=1, board_size=5, outputs=["report"], client_name=client
    )


@pytest.fixture
def _local_media(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_QUOTA_ENABLED", raising=False)
    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")


@pytest.mark.usefixtures("_local_media")
def test_client_note_matches_the_cache_behaviour(tmp_path: Path) -> None:
    """R1-2: 案内の 1 行は今の挙動と合わせる。

    結果キャッシュのキーに client_name が入り、名前だけ違う依頼を再描画に回す経路（PR-5）が
    まだ無いので、名前を足して依頼し直すと動画の分析からやり直しになる（Gemini の 1 本ずつの
    分析がもう一度呼ばれる）。だから「動画の再分析なし」と約束しない。PR-5 を入れたらこのテストと
    文言を一緒に直す。壊し方: 文言を「（動画の再分析なし）」に戻す → 赤。
    """
    gemini = _FakeGemini()
    cache = VideoAlgorithmResultCache(bucket="b", client=_FakeS3(), ttl_seconds=60)
    skill = _cached_skill(tmp_path, gemini, cache)
    first = skill.run(_run_input(), SkillContext(request_id="r1", metadata={"user_email": ME}))
    assert first.slack_summary.splitlines()[-1] == CLIENT_MISSING_NOTE
    skill.run(_run_input(CLIENT), SkillContext(request_id="r2", metadata={"user_email": ME}))
    assert gemini.video_calls == 2  # 名前を足した依頼は動画の分析からやり直し
    assert "再分析なし" not in CLIENT_MISSING_NOTE and "やり直す" in CLIENT_MISSING_NOTE


@pytest.mark.usefixtures("_local_media")
def test_cached_result_keeps_its_generated_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-5: キャッシュを再利用したときは、検索したときの取得日時のまま返す（今の時刻にしない）。

    順位は取得した時点の値なので、フッタの「順位は…時点」に今の時刻を付けると事実と違う。
    壊し方: キャッシュの再利用で generated_at を今の時刻にする → 赤。
    """
    from teamagent.skills.video_algorithm import skill as skill_mod

    monkeypatch.setattr(skill_mod, "_now_jst_iso", lambda: STAMP)
    gemini = _FakeGemini()
    cache = VideoAlgorithmResultCache(bucket="b", client=_FakeS3(), ttl_seconds=60)
    skill = _cached_skill(tmp_path, gemini, cache)
    first = skill.run(_run_input(), SkillContext(request_id="r1", metadata={"user_email": ME}))
    monkeypatch.setattr(skill_mod, "_now_jst_iso", lambda: "2026-09-30T09:00:00+09:00")
    again = skill.run(_run_input(), SkillContext(request_id="r2", metadata={"user_email": ME}))
    assert gemini.video_calls == 1  # 2 回目はキャッシュ
    assert first.generated_at == again.generated_at == STAMP
    html = render_slides(again, generated_at=again.generated_at or "")
    assert f"順位は{SHOWN}時点" in html and "2026-09-30" not in html


def test_tool_description_has_no_winning_words() -> None:
    """R1-7（M17）: MCP のツール説明（Aico の LLM が読む）と入力の説明に「勝ち筋」を使わない。

    壊し方: 説明を旧文言（「5本横断で勝ち筋を読み解き…」）に戻す → 赤。
    """
    import json

    texts = [
        VideoAlgorithmSkill.description,
        json.dumps(VideoAlgorithmInput.model_json_schema(), ensure_ascii=False),
    ]
    for text in texts:
        assert "勝ち筋" not in text and "勝ちパターン" not in text


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
