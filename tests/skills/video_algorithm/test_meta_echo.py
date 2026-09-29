"""取得済みの欄を捨てない（投稿日時・ハッシュタグ・音源名）と、入力の echo・取得日時。

仕様 v3 §2-4 skill.py: _search と _posts_to_metas が TikTokVideo / 正規化 post の
create_time・hashtags・music_title を VideoMeta に写す。VideoAlgorithmOutput に
client_name・competitors・avoid_terms・generated_at を返す。区分の名簿は横断の入力まで届く。
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gemini_client import GeminiResponse
from teamagent.adapters.tiktok_scraper import TikTokAuthor, TikTokSearchResult, TikTokVideo
from teamagent.skills.base import SkillContext
from teamagent.skills.video_algorithm.facts import posted_date
from teamagent.skills.video_algorithm.schema import VideoAlgorithmInput, VideoMeta
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill


@pytest.fixture(autouse=True)
def _explicit_local_media_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")


def test_posts_to_metas_keeps_post_date_hashtags_and_music() -> None:
    """壊し方: _posts_to_metas で写さない → create_time 0・hashtags 空で赤。"""
    posts: list[dict[str, Any]] = [
        {
            "rank_display": 1,
            "url": "https://www.tiktok.com/@a/video/1",
            "title": "本文",
            "create_time": 1753412400,
            "hashtags": ["スパイスカレー", 3, ""],
            "music_title": "オリジナル楽曲",
            "duration": 75,
        },
        {"rank_display": 2, "create_time": "broken", "hashtags": "not-a-list"},
    ]
    metas = VideoAlgorithmSkill()._posts_to_metas(posts)
    assert (metas[0].create_time, metas[0].hashtags, metas[0].music_title) == (
        1753412400,
        ["スパイスカレー"],
        "オリジナル楽曲",
    )
    assert (metas[1].create_time, metas[1].hashtags, metas[1].music_title) == (0, [], "")


def test_search_keeps_post_date_hashtags_and_music(monkeypatch: pytest.MonkeyPatch) -> None:
    """壊し方: _search で写さない → 赤。"""
    video = TikTokVideo(
        id="7",
        url="https://www.tiktok.com/@a/video/7",
        desc="本文 #スパイスカレー",
        create_time=1753412400,
        duration=75,
        cover_url="https://p16.example/c.jpg",
        author=TikTokAuthor(unique_id="a", nickname="A", follower_count=10),
        play_count=100,
        digg_count=1,
        comment_count=1,
        share_count=1,
        collect_count=1,
        hashtags=("スパイスカレー", "無水カレー"),
        music_title="オリジナル楽曲 - a",
    )

    def fake_search(query: str, **kwargs: Any) -> TikTokSearchResult:
        return TikTokSearchResult(query=query, search_type="keyword", videos=(video,))

    monkeypatch.setattr("teamagent.adapters.tiktok_scraper.search_tiktok", fake_search)
    meta = VideoAlgorithmSkill()._search("スパイスカレー", 1, "req")[0]
    assert meta.create_time == 1753412400
    assert meta.hashtags == ["スパイスカレー", "無水カレー"]
    assert meta.music_title == "オリジナル楽曲 - a"
    assert str(posted_date(meta)[0]) == "2025-07-25"


def _gemini() -> MagicMock:
    block = (
        "```json\n"
        '{"duration_sec":18,"hook_type":"question","telops":[{"sec":1.0,"text":"新宿の名店"}],'
        '"brand_detections":[{"brand_name":"ユニクロ","appear_sec":[12.0],'
        '"total_screen_time_sec":2.0,"prominence":"prominent","brand_relation":"client"}],'
        '"scenes":[{"start_sec":0,"end_sec":3,"desc":"導入"}],"cta_type":["save"]}\n```'
    )
    gemini = MagicMock()
    gemini.analyze_video_bytes.return_value = GeminiResponse(
        text=block,
        input_tokens=10,
        output_tokens=10,
        cost_usd=0.001,
        model_id="gemini-3.5-flash",
        latency_ms=10,
    )
    gemini.generate_text.return_value = GeminiResponse(
        text="所見のみ",
        input_tokens=10,
        output_tokens=10,
        cost_usd=0.001,
        model_id="gemini-3.5-flash",
        latency_ms=10,
    )
    return gemini


def _run(tmp_path: object, **input_kw: Any) -> tuple[Any, MagicMock]:
    metas = [
        VideoMeta(
            rank=i,
            url=f"https://t/{i}",
            desc="新宿 ランチ",
            play_count=1000,
            collect_count=10,
            duration_sec=18.0,
        )
        for i in (1, 2)
    ]
    gemini = _gemini()
    skill = VideoAlgorithmSkill(
        gemini=gemini,
        searcher=lambda q, n, r: metas,
        downloader=lambda url: (b"vid", "video/mp4"),
        proxy=lambda d, m: (d, m),
        report_dir=str(tmp_path),
    )
    out = skill.run(
        VideoAlgorithmInput(query="新宿 ランチ", max_videos=2, outputs=["report"], **input_kw),
        ctx=SkillContext(),
    )
    return out, gemini


def test_output_echoes_roster_and_generated_at(tmp_path: object) -> None:
    out, gemini = _run(
        tmp_path,
        client_name=" ユニクロ ",
        competitors=["しまむら|シマムラ", " "],
        avoid_terms=["ルー卒業", ""],
    )
    assert out.client_name == "ユニクロ"
    assert out.competitors == ["しまむら|シマムラ"]
    assert out.avoid_terms == ["ルー卒業"]
    assert out.generated_at is not None
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00", out.generated_at)
    # 名簿は横断シンセシスの入力まで届く（区分はコード。Gemini の client ではなく名簿の client）
    prompt = gemini.generate_text.call_args.args[0]
    assert '"名前": "ユニクロ", "区分": "クライアント", "目立ち方": "目立つ"' in prompt
    assert "- クライアント: ユニクロ" in prompt and "- 避けたい訴求: ルー卒業" in prompt


def test_without_roster_the_ai_client_guess_does_not_reach_the_llm(tmp_path: object) -> None:
    """壊し方: 個票で Gemini の brand_relation を渡す → 「client」が入り赤。"""
    out, gemini = _run(tmp_path)
    assert out.client_name is None and out.competitors == [] and out.avoid_terms == []
    prompt = gemini.generate_text.call_args.args[0]
    assert '"名前": "ユニクロ", "区分": "未指定", "目立ち方": "目立つ"' in prompt
    assert "client" not in prompt and "- クライアント: 未指定" in prompt
