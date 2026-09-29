"""スライドの版面を試す「負荷形」の合成データ（仕様 v3 §5 T22）。

本番形（prod_shape）より重い条件を 1 つにまとめる:
- 深掘り n=10（スライドは 20 枚・S5 の比較は 10 列・S6 の構成比較は 10 行）
- 長い文（テロップ・キャプション・作り手の名前・ブランド名・検索語 3 語）
- 横長の動画（3・7 位）と、表紙の無い動画（全部。cover_data_uri を空にする）
- 場面が多い（12 場面）・ブランドが多い（1 本 4 つ）
- synthesis v3 の欄を上限の字数と件数で埋めたもの（検査済みの印 version="v3" を付ける。
  文の中身ではなく、長さと件数で版面が崩れないかを見るため、finalize は通さない）

文言・アカウント・ブランドはすべて作り話。
"""

from __future__ import annotations

from itertools import pairwise

from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    AvoidItem,
    BoardAngle,
    CrossSynthesis,
    Directive,
    FrameShot,
    HypothesisV3,
    PerVideoNote,
    PostingPlan,
    Storyboard,
    StoryboardCut,
    SummaryLines,
    SynthRef,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
)
from tests.skills.video_algorithm.prod_shape import jpeg_uri

LOAD_QUERY = "スパイスカレー 作り方 簡単"
LOAD_N = 10
_LANDSCAPE = {3, 7}
_LONG = "とても長いテロップの文言でスライドの枠からはみ出さないかを確かめるための文"
_BRAND = "ロングネームブランドカレースパイスシリーズ"


def _long(prefix: str, n: int) -> str:
    return (prefix + _LONG * 3)[:n]


def _analysis(rank: int, duration: float) -> VideoVSEOAnalysis:
    telops = [
        {
            "sec": float(s),
            "text": _long(f"{s}秒 大さじ{rank}杯 スパイスカレー", 60),
            "position": "bottom",
        }
        for s in range(0, int(duration), 3)
    ]
    bounds = [round(duration * i / 12, 1) for i in range(13)]
    scenes = [
        {"start_sec": s, "end_sec": e, "desc": _long("場面", 100)} for s, e in pairwise(bounds)
    ]
    brands = [
        {
            "brand_name": f"{_BRAND}{k}",
            "appear_sec": [5.0 + k, 30.0 + k, duration - 2],
            "total_screen_time_sec": 12.0,
            "prominence": "hero" if k == 0 else "prominent",
            "is_intentional": "likely_sponsored" if k == 0 else "organic_mention",
        }
        for k in range(4)
    ]
    return VideoVSEOAnalysis.model_validate(
        {
            "duration_sec": duration,
            "hook_type": ["problem", "visual", "shock", "question"][rank % 4],
            "hook_summary": _long("冒頭の画", 120),
            "telops": telops,
            "scenes": scenes,
            "brand_detections": brands,
            "cta_type": ["save"],
            "cta_text": _long("保存して何度も作ってみてください", 60),
            "cta_sec": duration - 3,
            "has_narration": True,
            "spoken_keywords": [
                {
                    "keyword": "スパイスカレー",
                    "matched": True,
                    "match_type": "exact",
                    "layer": "narration",
                    "appear_sec": [0.5, 10.0, 20.0, 30.0],
                }
            ],
            "message_coherence": 90,
        }
    )


def load_videos() -> list[AnalyzedVideo]:
    out: list[AnalyzedVideo] = []
    for rank in range(1, LOAD_N + 1):
        duration = 60.0 + rank * 9  # 69〜150 秒
        land = rank in _LANDSCAPE
        uri = jpeg_uri(320, 180) if land else jpeg_uri(320, 568)
        a = _analysis(rank, duration)
        frames = [FrameShot(sec=0.8, caption="フック", data_uri=uri)] + [
            FrameShot(
                sec=round((sc.start_sec + sc.end_sec) / 2, 1),
                caption=f"{_BRAND}0 {sc.start_sec:.0f}s",
                data_uri=uri,
            )
            for sc in a.scenes
        ]
        out.append(
            AnalyzedVideo(
                meta=VideoMeta(
                    rank=rank,
                    url=f"https://www.tiktok.com/@u{rank}/video/{7000000000000000000 + rank}",
                    author="W" * 24,
                    follower_count=1_234_567 * rank,
                    desc=_long("#PR スパイスカレー 作り方 簡単 材料 鶏もも肉 300g ", 900),
                    play_count=9_876_543 // rank,
                    collect_count=98_765 // rank,
                    share_count=12_345 // rank,
                    duration_sec=duration,
                    create_time=1_700_000_000 + rank * 86_400,
                    hashtags=["スパイスカレー", "作り方"],
                    music_title=_long("オリジナル楽曲", 80),
                ),
                analysis=a,
                frames=frames,
                cover_data_uri="",  # 表紙なし
            )
        )
    return out


def load_board() -> list[VideoMeta]:
    board = [v.meta for v in load_videos()]
    for rank in range(LOAD_N + 1, 31):
        board.append(
            VideoMeta(
                rank=rank,
                url=f"https://www.tiktok.com/@c{rank % 4}/video/{7100000000000000000 + rank}",
                author=f"creator_{rank % 4}_" + "x" * 12,
                desc=_long(f"無水 4つ ダイソー #PR スパイスカレー{rank} ", 300),
                play_count=10_000 * rank,
                collect_count=100 * rank,
                duration_sec=60.0,
                create_time=1_500_000_000 + rank * 20_000_000,
            )
        )
    return board


def _ref(rank: int, sec: float, quote: str) -> SynthRef:
    return SynthRef(rank=rank, sec=sec, quote=quote, source="telop", found_sec=sec)


def load_synthesis() -> CrossSynthesis:
    """v3 の欄を上限の字数・件数で埋めたもの（版面の負荷試験用）。"""
    refs = [_ref(r, 3.0, _long("3秒 大さじ", 40)) for r in (1, 2)]
    cuts = [
        StoryboardCut(
            cut=k,
            show=_long("画の説明", 40),
            telop=_long("テロップ案", 24),
            aim=_long("狙い", 30),
            refs=[_ref(k, 3.0, _long("参考の引用", 40))],
            start_sec=float(k * 10),
            end_sec=float(k * 10 + 10),
            stage="10秒〜残り10秒",
        )
        for k in range(1, 7)
    ]
    return CrossSynthesis(
        version="v3",
        summary_lines=SummaryLines(
            type_line=_long("見出し", 40),
            feature_ids=["first_telop_0s"],
            best_reason=_long("最も見られた理由", 50),
            client_move=_long("（クライアント商品）の次の一手", 60),
        ),
        per_video=[
            PerVideoNote(
                rank=r,
                win_line=_long("勝ち方", 30),
                why_fact=_long("事実", 50),
                why_guess="推測:" + _long("推測", 50),
                steal=[_long("盗める点1", 40), _long("盗める点2", 40)],
                not_to_copy=_long("真似しない", 40),
            )
            for r in range(1, LOAD_N + 1)
        ],
        directives=[
            Directive(
                text=_long(f"指示{k}", 60),
                kind="テロップ",
                refs=refs,
                origin="llm",
                tier="事例",
                ranks=[1, 2],
            )
            for k in range(6)
        ],
        avoid=[
            AvoidItem(text=_long(f"やらないこと{k}", 50), refs=refs, ranks=[1, 2]) for k in range(4)
        ],
        storyboards=[
            Storyboard(
                name=_long("絵コンテ", 16),
                basis_ranks=[1, 2, 3],
                cuts=cuts,
                target_sec=96.0,
                basis_note="#1・#2・#3にもとづく案（タイアップ投稿 #1・#2・#3を含む）",
            )
            for _ in range(2)
        ],
        board_angles=[
            BoardAngle(
                label=_long("切り口", 8), match_terms=["無水", "4つ"], ranks=list(range(11, 31))
            )
            for _ in range(5)
        ],
        hypotheses=[
            HypothesisV3(
                text=_long(f"仮説{k}", 60),
                match_terms=["大さじ"],
                test=_long("A/Bの組み方", 60),
                ranks=[1, 2, 3],
                tier="多数派",
            )
            for k in range(3)
        ],
        posting=PostingPlan(caption_plan=_long("キャプション案", 80), ab_plan=_long("A/B", 80)),
    )


def load_output() -> VideoAlgorithmOutput:
    from teamagent.skills.video_algorithm.analysis import cross_analyze

    videos = load_videos()
    board = load_board()
    cross = cross_analyze(videos, LOAD_QUERY, board=board)
    cross.synthesis = load_synthesis()
    return VideoAlgorithmOutput(
        query=LOAD_QUERY,
        videos=videos,
        board=board,
        cross=cross,
        client_name=_long("クライアント名", 40),
        competitors=[_long(f"競合{k}|別名", 30) for k in range(4)],
        avoid_terms=[_long("避けたい訴求", 20)],
        generated_at="2026-09-28T10:15:00+09:00",
    )


__all__ = ["LOAD_N", "LOAD_QUERY", "load_board", "load_output", "load_synthesis", "load_videos"]
