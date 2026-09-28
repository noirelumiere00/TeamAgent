"""2 段目の詳しい構成（場面ごと）のテスト用の偽の分析結果。

本番の Gemini の出力の形（scenes に role/telop/speech/intent が付く・付かない古い出力もある）と、
media job が返すコマ（場面の中央の秒・幅 180px の JPEG の data URI）を再現する。
- 1 位: 12 場面・役割あり・テロップと発話あり・ブランドなし（作り方の動画）
- 2 位: 8 場面・役割なし（v1/v2 の既定プロンプトの古い出力）・CTA の秒あり
- 3 位: 10 場面・役割あり・ブランドが主役で 6 秒映る（クライアント）
- 4 位: 14 場面（12 行を超える）・一部の役割が語彙の外（"transition"）
- 5 位: 分析失敗（Gemini 500）
"""

from __future__ import annotations

import base64
from itertools import pairwise
from typing import Any

from teamagent.skills.video_algorithm.frames import scene_timecodes
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    FrameShot,
    VideoMeta,
    VideoVSEOAnalysis,
)

# 1x1 の JPEG 相当の小さい画像（形だけ本物の data URI）。
TINY_JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0frame\xff\xd9").decode()


def _scenes(
    bounds: list[float], roles: list[str | None], **extra: list[Any]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, (start, end) in enumerate(pairwise(bounds)):
        sc: dict[str, Any] = {
            "start_sec": start,
            "end_sec": end,
            "desc": f"画面{'ABCDEFGHIJKLMNOP'[i]}",
        }
        role = roles[i] if i < len(roles) else None
        if role is not None:
            sc["role"] = role
        for key, values in extra.items():
            if i < len(values) and values[i] is not None:
                sc[key] = values[i]
        out.append(sc)
    return out


RICH_ANALYSES: dict[int, dict[str, Any]] = {
    1: {
        "duration_sec": 36.0,
        "hook_type": "number",
        "hook_summary": "4つでいい本格スパイスカレー",
        "hook_has_caption": True,
        "telops": [
            {"sec": 0.5, "text": "スパイスカレーは4つでいい", "kw_match": True},
            {"sec": 4.0, "text": "1. クミン", "kw_match": False},
            {"sec": 8.0, "text": "2. コリアンダー", "kw_match": False},
            {"sec": 33.0, "text": "保存して見返してね", "kw_match": False},
        ],
        "scenes": _scenes(
            [0, 2.5, 4, 8, 11, 14, 17, 20, 24, 28, 31, 33, 36],
            [
                "hook",
                "problem",
                "steps",
                "steps",
                "steps",
                "steps",
                "result",
                "result",
                "proof",
                "result",
                "other",
                "cta",
            ],
            telop=["スパイスカレーは4つでいい", None, "1. クミン", "2. コリアンダー"],
            speech=["これ、4つだけで作れます", "ルーは要りません"],
            intent=["材料の少なさで止める", "悩みを言葉にする"],
        ),
        "cut_count": 18,
        "pacing": "fast",
        "main_message": "スパイス4つで本格カレー",
        "cta_type": ["save"],
        "cta_text": "保存して見返してね",
        "cta_sec": 33.0,
        "has_narration": True,
        "is_trending_sound": "no",
        "spoken_keywords": [
            {
                "keyword": "スパイスカレー",
                "matched": True,
                "layer": "narration",
                "appear_sec": [1.2],
            }
        ],
        "message_coherence": 85,
        "save_share_motivation": "材料4つのメモとして見返す",
    },
    2: {
        "duration_sec": 48.0,
        "hook_type": "question",
        "hook_summary": "ルーなしで作れる？",
        "hook_has_caption": False,
        "telops": [{"sec": 6.0, "text": "ルーなしで作る", "kw_match": False}],
        "scenes": _scenes([0, 3, 9, 15, 21, 27, 33, 40, 48], [None] * 8),
        "cut_count": 8,
        "pacing": "moderate",
        "main_message": "ルーなしで作れる",
        "cta_type": ["follow"],
        "cta_sec": 44.0,
        "has_narration": False,
        "is_trending_sound": "yes",
        "keyword_matches": [
            {"keyword": "スパイスカレー", "matched": True, "layer": "telop", "appear_sec": [12.0]},
            {"keyword": "スパイスカレー", "matched": True, "layer": "caption"},
        ],
        "message_coherence": 62,
        "save_share_motivation": "",
    },
    3: {
        "duration_sec": 30.0,
        "hook_type": "visual",
        "hook_summary": "鍋から立つ湯気",
        "hook_has_caption": True,
        "telops": [{"sec": 0.8, "text": "#PR 〇〇カレー粉", "kw_match": True}],
        "scenes": _scenes(
            [0, 2, 5, 8, 11, 14, 17, 20, 24, 27, 30],
            [
                "hook",
                "steps",
                "steps",
                "result",
                "proof",
                "proof",
                "result",
                "steps",
                "result",
                "cta",
            ],
            intent=["湯気で食欲を起こす"],
        ),
        "brand_detections": [
            {
                "brand_name": "〇〇カレー粉",
                "detection_source": "product_package",
                "appear_sec": [2.0, 14.0],
                "total_screen_time_sec": 6.0,
                "prominence": "hero",
                "brand_relation": "client",
            }
        ],
        "cut_count": 15,
        "pacing": "fast",
        "main_message": "市販のカレー粉で本格",
        "cta_type": ["buy"],
        "cta_text": "プロフィールから",
        "has_narration": True,
        "is_trending_sound": "unknown",
        "message_coherence": 90,
        "save_share_motivation": "買う前に見返す",
    },
    4: {
        "duration_sec": 70.0,
        "hook_type": "other",
        "hook_summary": "",
        "hook_has_caption": False,
        "telops": [],
        "scenes": _scenes(
            [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70],
            ["hook", "transition", "steps"],
        ),
        "cut_count": None,
        "pacing": "slow",
        "main_message": "家族の食卓",
        "cta_type": [],
        "has_narration": False,
        "is_trending_sound": "no",
        "message_coherence": None,
        "save_share_motivation": "",
    },
}
FAILED_RANKS = frozenset({5})


def scene_frames(analysis: VideoVSEOAnalysis, duration_sec: float = 0.0) -> list[FrameShot]:
    """本番（video_algorithm の _extract_frames）と同じ秒の選び方（scene_timecodes）でコマを返す。"""
    return [
        FrameShot(sec=sec, caption=caption, data_uri=TINY_JPEG)
        for sec, caption in scene_timecodes(analysis, duration_sec=duration_sec)
    ]


def rich_videos() -> list[AnalyzedVideo]:
    """5 本（1〜4 位は分析済み・コマと表紙つき、5 位は分析失敗）。"""
    videos: list[AnalyzedVideo] = []
    for rank in (1, 2, 3, 4, 5):
        meta = VideoMeta(
            rank=rank,
            url=f"https://www.tiktok.com/@cook{rank}/video/{7000 + rank}",
            author=f"cook{rank}",
            follower_count=10_000 * rank,
            play_count=100_000 // rank,
            collect_count=2_000 // rank,
            # 検索結果の尺（TikTok は整数の秒）。分析できた本は Gemini の尺と同じにする。
            duration_sec=float(int(RICH_ANALYSES.get(rank, {}).get("duration_sec", 30.0 + rank))),
        )
        if rank in FAILED_RANKS:
            videos.append(AnalyzedVideo(meta=meta, error="分析失敗: RuntimeError"))
            continue
        analysis = VideoVSEOAnalysis.model_validate(RICH_ANALYSES[rank])
        videos.append(
            AnalyzedVideo(
                meta=meta,
                analysis=analysis,
                frames=scene_frames(analysis, meta.duration_sec),
                cover_data_uri=TINY_JPEG,
            )
        )
    return videos
