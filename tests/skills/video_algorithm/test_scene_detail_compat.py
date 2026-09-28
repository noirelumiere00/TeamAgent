"""場面の任意の欄（role/telop/speech/intent）と、2 段目用の分析オプションが video_algorithm を変えないこと。

- 欄が無い（v1/v2 の既定プロンプトの）出力は、model_dump が従来と同じ形（新しいキーが出ない）。
- 欄がある出力は読めて、語彙の外の role は None（コードが推定し直す）。
- run（動画分析ツール）が Gemini に渡す system は v2 のまま。追記は analyze_videos の
  system_addendum を渡したときだけ付く。
- run はフレームを従来どおり pick_timecodes・幅 320・プレビュー動画ありで作る。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gemini_client import GeminiResponse
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import SkillContext
from teamagent.skills.video_algorithm import frames as frames_mod
from teamagent.skills.video_algorithm.schema import (
    SCENE_DETAIL_FIELDS,
    Scene,
    VideoAlgorithmInput,
    VideoMeta,
    VideoVSEOAnalysis,
)
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill, parse_analysis

OLD_ANALYSIS: dict[str, Any] = {
    "duration_sec": 20.0,
    "hook_type": "question",
    "scenes": [
        {"start_sec": 0.0, "end_sec": 3.0, "desc": "導入"},
        {"start_sec": 3.0, "end_sec": 20.0, "desc": "本編"},
    ],
    "cta_type": ["save"],
    "cta_sec": 18.0,
}


@pytest.fixture(autouse=True)
def _local_media(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")


def _resp(analysis: dict[str, Any]) -> GeminiResponse:
    return GeminiResponse(
        text="### 所見\nx\n\n```json\n" + json.dumps(analysis, ensure_ascii=False) + "\n```",
        input_tokens=6000,
        output_tokens=400,
        cost_usd=0.0014,
        model_id="gemini-3.5-flash",
        latency_ms=1000,
    )


def test_old_outputs_dump_exactly_as_before() -> None:
    a = parse_analysis(_resp(OLD_ANALYSIS).text)
    assert a is not None
    dumped = a.model_dump()
    assert dumped["scenes"] == OLD_ANALYSIS["scenes"]
    assert all(not (set(SCENE_DETAIL_FIELDS) & set(sc)) for sc in dumped["scenes"])
    assert '"role"' not in a.model_dump_json()
    assert Scene().model_dump() == {"start_sec": 0.0, "end_sec": 0.0, "desc": ""}


def test_scene_detail_is_read_and_unknown_roles_become_none() -> None:
    data = {
        "scenes": [
            {"start_sec": 0, "end_sec": 2, "role": "HOOK", "telop": " 4つ  でいい ", "speech": 3},
            {"start_sec": 2, "end_sec": 5, "role": "transition", "intent": "", "telop": None},
        ]
    }
    a = VideoVSEOAnalysis.model_validate(data)
    first, second = a.scenes
    assert (first.role, first.telop, first.speech) == ("hook", "4つ でいい", None)
    assert (second.role, second.intent, second.telop) == (None, None, None)
    assert a.model_dump()["scenes"][0] == {
        "start_sec": 0.0,
        "end_sec": 2.0,
        "desc": "",
        "role": "hook",
        "telop": "4つ でいい",
    }
    again = VideoVSEOAnalysis.model_validate(a.model_dump())
    assert again == a  # 結果キャッシュの往復でも変わらない


def _skill(gemini: MagicMock) -> VideoAlgorithmSkill:
    return VideoAlgorithmSkill(
        gemini=gemini,
        downloader=lambda url: (b"vid", "video/mp4"),
        proxy=lambda d, m: (d, m),
        max_workers=1,
    )


def test_analyze_videos_appends_the_addendum_only_when_given() -> None:
    gemini = MagicMock()
    gemini.analyze_video_bytes.return_value = _resp(OLD_ANALYSIS)
    skill = _skill(gemini)
    metas = [VideoMeta(rank=1, url="https://www.tiktok.com/@a/video/1")]
    base = load_prompt("video_algorithm", "v2", "system")

    skill.analyze_videos(metas, query="q", client_name=None, request_id="r")
    assert gemini.analyze_video_bytes.call_args.kwargs["system"] == base

    skill.analyze_videos(metas, query="q", client_name=None, request_id="r", system_addendum="追記")
    assert gemini.analyze_video_bytes.call_args.kwargs["system"] == base.rstrip() + "\n\n追記\n"


def test_run_keeps_the_v2_system_and_its_media_extras(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """動画分析ツール（run）の Gemini への指示・コマの取り方・プレビュー動画は変わらない。"""
    from teamagent.adapters import video_proxy

    gemini = MagicMock()
    gemini.analyze_video_bytes.return_value = _resp(OLD_ANALYSIS)
    frame_calls: list[tuple[list[float], int]] = []
    previews: list[bytes] = []

    def _frames(data: bytes, mime: str, secs: list[float], **k: Any) -> list[tuple[float, str]]:
        frame_calls.append((list(secs), k["width"]))
        return []

    def _preview(data: bytes, mime: str, **k: Any) -> str:
        previews.append(data)
        return ""

    monkeypatch.setattr(frames_mod, "extract_frames", _frames)
    monkeypatch.setattr(video_proxy, "make_web_preview", _preview)
    metas = [VideoMeta(rank=1, url="https://t/1", desc="d", play_count=10, collect_count=1)]
    skill = VideoAlgorithmSkill(
        gemini=gemini,
        searcher=lambda q, n, r: metas,
        downloader=lambda url: (b"vid", "video/mp4"),
        proxy=lambda d, m: (d, m),
        report_dir=str(tmp_path),
    )
    out = skill.run(
        VideoAlgorithmInput(query="q", max_videos=1, outputs=["report"]), SkillContext()
    )
    assert gemini.analyze_video_bytes.call_args.kwargs["system"] == load_prompt(
        "video_algorithm", "v2", "system"
    )
    a = VideoVSEOAnalysis.model_validate(OLD_ANALYSIS)
    assert frame_calls == [([s for s, _ in frames_mod.pick_timecodes(a, max_frames=6)], 320)]
    assert len(previews) == 1
    assert out.videos[0].analysis is not None
    assert out.videos[0].model_dump()["analysis"]["scenes"] == OLD_ANALYSIS["scenes"]
