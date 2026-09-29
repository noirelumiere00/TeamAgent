"""照合（evidence.py）: 引用 refs・名簿・段階・コマ、と JPEG の寸法（仕様 v3 §2-4・§5 T6）。"""

from __future__ import annotations

import pytest

from teamagent.skills.video_algorithm import facts as vf
from teamagent.skills.video_algorithm.evidence import (
    TIER_CASE,
    TIER_MAJORITY,
    TIER_OBSERVED,
    TIER_REQUIRED,
    Ref,
    Roster,
    VerifiedRef,
    analysis_terms,
    norm,
    query_terms,
    quote_frame,
    ref_frame,
    refs_tier,
    tier,
    tier_text,
    verify_ref,
)
from teamagent.skills.video_algorithm.schema import FrameShot
from tests.skills.video_algorithm.prod_shape import QUERY, jpeg, prod_videos


def _ctx(rank: int) -> tuple[vf.VideoFacts, object]:
    v = next(v for v in prod_videos() if v.meta.rank == rank)
    return vf.video_facts(v, QUERY), v.analysis


def _verify(rank: int, sec: float | None, quote: str) -> VerifiedRef | None:
    facts, analysis = _ctx(rank)
    return verify_ref(Ref(rank, sec, quote), facts, analysis)  # type: ignore[arg-type]


# ── T6 refs の照合 ───────────────────────────────────────────────────────────


def test_ref_to_a_real_telop_passes_and_a_made_up_picture_fails() -> None:
    """壊し方: 許容を ±10 秒にする／照合を外す → 作り話の引用が通って赤。"""
    ok = _verify(4, 25.0, "大さじ8杯")
    assert ok is not None and (ok.source, ok.found_sec) == ("telop", 25.0)
    assert _verify(1, 0.0, "スプーンで引き上げる") is None  # どこにも無い画の描写
    assert _verify(1, 0.0, "とにかく痩せたいから") is None  # 別の動画（#4）のテロップ


@pytest.mark.parametrize(
    ("sec", "passes"), [(23.0, True), (27.0, True), (22.9, False), (27.1, False)]
)
def test_ref_seconds_tolerance_is_two_seconds(sec: float, passes: bool) -> None:
    """テロップの秒 ±2.0 は合格、±2.1 は不合格。"""
    assert (_verify(4, sec, "大さじ8杯") is not None) is passes


def test_ref_quote_is_normalized() -> None:
    assert _verify(4, 25.0, "大さじ８杯") is not None  # 全角数字（NFKC）
    assert _verify(2, 10.0, "スパイスカレーは これで作れます") is not None  # 空白を無視
    assert _verify(4, 25.0, "杯") is None  # 1 文字は照合しない


def test_ref_sources_scene_hook_brand_and_caption() -> None:
    scene = _verify(2, 20.0, "場面4")  # 12〜42 秒の場面の説明
    assert scene is not None and scene.source == "scene"
    assert _verify(2, 44.1, "場面4") is None  # 場面の終わり＋2 秒より後
    hook = _verify(5, 1.0, "スプーンですくう")
    assert hook is not None and hook.source == "hook"
    assert _verify(5, 3.5, "スプーンですくう") is None  # フックは 3 秒まで
    brand = _verify(4, 23.5, "ハーブ専科")
    assert brand is not None and brand.source in ("telop", "brand")
    caption = _verify(3, None, "作り方は下にまとめました")
    assert caption is not None and caption.source == "caption"
    assert _verify(3, None, "カレールーはもう卒業") is None  # テロップの文言はキャプションに無い


def test_brand_name_passes_only_near_its_seconds() -> None:
    """ブランド名は、映る秒（#1 SPICIA は 24〜30 秒）の ±2 秒のときだけ合格（近くのテロップに無い語）。

    壊し方: ブランドの秒を見ない → 40 秒の引用が通って赤。
    """
    near = _verify(1, 27.0, "SPICIA")
    assert near is not None and (near.source, near.found_sec) == ("brand", 26.0)
    assert _verify(1, 32.0, "SPICIA") is not None  # 30 秒の ±2
    assert _verify(1, 40.0, "SPICIA") is None
    assert _verify(1, 20.0, "SPICIA") is None


@pytest.mark.parametrize(
    ("rank", "sec", "quote"),
    [
        (4, 22.0, "ハーブ専科で1週間で3kg痩せた"),
        (1, 27.0, "SPICIAだけで本格的な味になる"),
        (2, 12.0, "ティーケー食品のスパイスは全部100円"),
    ],
)
def test_made_up_quote_containing_a_brand_name_fails(rank: int, sec: float, quote: str) -> None:
    """R2-1（critical）: ブランド名を含むだけの作り話の引用を「照合済み」にしない。

    ブランド名で合格させるのは、引用がブランド名（かその一部）のときだけ。壊し方: `name in q`
    （引用がブランド名を含む）で合格させる → 3 つとも通って赤。
    """
    assert _verify(rank, sec, quote) is None


def test_refs_tier_counts_distinct_ranks() -> None:
    refs = [r for r in (_verify(4, 25.0, "大さじ8杯"), _verify(3, 13.0, "大さじ3")) if r]
    refs.append(VerifiedRef(4, 28.5, "醤油大さじ5杯", "telop", 28.5))
    refs.append(VerifiedRef(5, 25.0, "大さじ1", "telop", 25.0))
    assert refs_tier(refs, 5) == (TIER_MAJORITY, (3, 4, 5))


@pytest.mark.parametrize(
    ("c", "n", "name"),
    [
        (5, 5, TIER_REQUIRED),
        (3, 5, TIER_MAJORITY),
        (2, 5, TIER_CASE),
        (3, 3, TIER_REQUIRED),
        (1, 1, TIER_OBSERVED),
        (2, 2, TIER_OBSERVED),
        (1, 2, TIER_OBSERVED),
    ],
)
def test_tier_names_need_three_videos(c: int, n: int, name: str) -> None:
    """R2-10: 1〜2 本だけの観測を「必須条件」「多数派」と呼ばない（「観測 1/1」）。

    壊し方: 本数の下限（MIN_TIER_N）を外す → 1/1 が必須条件になって赤。
    """
    assert tier(c, n) == name
    assert tier_text([1], 1) == "観測 1/1（#1）"


def test_quote_frame_is_right_after_the_quote_second_only() -> None:
    """R2-5: 引用の横のコマは、引用の秒の 0.5 秒前〜2 秒後だけ（±3 秒の別のテロップのコマを出さない）。

    壊し方: 窓を ±3 秒に戻す → 12 秒の引用に 15.5 秒のコマが付いて赤。
    """
    frames = [FrameShot(sec=s, data_uri=f"data:image/jpeg;base64,{s}") for s in (12.0, 15.5)]
    picked = quote_frame(frames, 12.0)
    assert picked is not None and picked.sec == 12.0
    assert quote_frame(frames, 13.0) is None  # 12.0 は 0.5 秒より前・15.5 は 2 秒より後
    assert quote_frame(frames, 14.0) is not None  # 15.5 は 2 秒以内の後
    assert quote_frame(frames, 16.1) is None  # 15.5 は 0.5 秒より前
    assert quote_frame(frames, None) is None


def test_ref_frame_uses_an_existing_frame_within_three_seconds() -> None:
    frames = [FrameShot(sec=s, data_uri=f"data:image/jpeg;base64,{s}") for s in (0.8, 16.0, 24.5)]
    picked = ref_frame(frames, 25.0)
    assert picked is not None and picked.sec == 24.5
    assert ref_frame(frames, 20.0) is None  # 3 秒より離れていれば出さない
    assert ref_frame(frames, None) is None
    assert ref_frame([FrameShot(sec=25.0)], 25.0) is None  # 画像の無いコマは使わない


# ── 名簿・語 ──────────────────────────────────────────────────────────────────


def test_roster_matches_aliases_ignoring_width_case_and_spaces() -> None:
    roster = Roster.of("SPICIA", ["T&K|ティーケー食品", " ハーブ専科 "])
    assert roster.relation("spicia") == "client"
    assert roster.relation("Ｔ＆Ｋ") == "competitor"
    assert roster.relation("ティーケー 食品") == "competitor"
    assert roster.relation("ハーブ専科") == "competitor"
    assert roster.relation("T&K食品") == "other"  # 完全一致だけ（部分一致で競合にしない）
    assert roster.relation("unidentified_logo") == "other"
    assert Roster.of(None, []).relation("SPICIA") == "unspecified"
    assert Roster.of("  ", [" "]).specified is False


def test_query_terms_and_analysis_terms() -> None:
    assert query_terms("スパイスカレー　作り方, 作り方") == ["スパイスカレー", "作り方"]
    a = prod_videos()[0].analysis
    assert a is not None and analysis_terms(a) == ["スパイスカレー", "作り方"]
    assert norm("Ｓ＆Ｂ  赤缶") == "s&b赤缶"


# ── 縦横（JPEG の SOF）───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("size", "orientation"),
    [((320, 568), "portrait"), ((320, 180), "landscape"), ((320, 320), "square")],
)
def test_orientation_from_sof(size: tuple[int, int], orientation: str) -> None:
    import base64

    uri = "data:image/jpeg;base64," + base64.b64encode(jpeg(*size)).decode()
    assert vf.jpeg_size(jpeg(*size)) == size
    assert vf.orientation_of([FrameShot(sec=0.8, data_uri=uri)]) == orientation


def test_orientation_falls_back_to_the_cover_then_unknown() -> None:
    import base64

    broken = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0frame\xff\xd9").decode()
    cover = "data:image/jpeg;base64," + base64.b64encode(jpeg(180, 320)).decode()
    assert vf.orientation_of([FrameShot(sec=0.8, data_uri=broken)], cover) == "portrait"
    assert vf.orientation_of([FrameShot(sec=0.8, data_uri=broken)]) == "unknown"
    assert vf.jpeg_size(b"not a jpeg") is None
