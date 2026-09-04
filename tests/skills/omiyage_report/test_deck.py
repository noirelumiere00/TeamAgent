"""計測JSON（DeckPlan）の contract 準拠・U1構成・分母明記・固定文言の検証。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from teamagent.skills.omiyage_report.compose import build_analysis_note, build_summary_lines
from teamagent.skills.omiyage_report.contract import (
    CTA_TEXT,
    EG_RATE_FOOTNOTE,
    VOICE_UNMEASURED_NOTE,
    DeckPlan,
    SlideDataA,
    SlideDataB,
    SlideDataC,
    SlideDataD,
    SlideDataE,
    SlideDataH,
)
from teamagent.skills.omiyage_report.deck_plan import (
    DeckPlanBuildError,
    build_audit,
    build_deck_plan,
    top5_candidates,
)
from teamagent.skills.omiyage_report.metrics import (
    AxisData,
    OmiyageMeasurement,
    PostRecord,
    measure,
)
from teamagent.skills.omiyage_report.video_analysis import (
    VideoAnalysisFailure,
    VideoAnalysisReport,
    VideoAnalysisSuccess,
)

from .fmt_fixtures import make_png_bytes

_THUMB = make_png_bytes()

_VOCAB = (
    "正直レビュー/検証系",
    "成分オタク系",
    "ベスコス/まとめ系",
    "メンズ美容系",
    "専門家/医師系",
    "PR/タイアップ明記",
)


def _post(
    video_id: str,
    caption: str,
    *,
    author: str = "someone",
    rank: int = 1,
    plays: int = 10_000,
    likes: int = 400,
    comments: int = 50,
    shares: int = 30,
    saves: int = 20,
    followers: int = 5_000,
    cover: bool = True,
) -> PostRecord:
    return PostRecord(
        video_id=video_id,
        url=f"https://www.tiktok.com/@{author}/video/{video_id}",
        author=author,
        caption=caption,
        hashtags=(),
        rank=rank,
        plays=plays,
        likes=likes,
        comments=comments,
        shares=shares,
        saves=saves,
        followers=followers,
        nickname=f"{author}名",
        cover_url=f"https://p16-sign.tiktokcdn.com/{video_id}.jpeg" if cover else "",
        duration_sec=21,
    )


def _measurement(*, with_failure: bool = False) -> OmiyageMeasurement:
    axes = [
        AxisData(
            role="general",
            label="一般KW「ヘアケア」検索",
            query="ヘアケア",
            requested=120,
            posts=(
                _post("1", "エムキュアのヘアケア #PR", rank=1, plays=50_000, followers=800_000),
                _post("2", "ラサーナ愛用", rank=2, plays=30_000, followers=60_000),
                _post("3", "ヘアケア雑談", rank=3, plays=100, followers=900),
                _post("4", "公式です", author="mqure_official", rank=4, plays=7_000),
            ),
        ),
        AxisData(
            role="brand",
            label="ブランド名「エムキュア」検索",
            query="エムキュア",
            requested=120,
            posts=(
                _post("5", "エムキュアでヘアケア", author="mqure_official", rank=1, plays=9_000),
                _post("6", "エムキュア口コミ #PR", rank=2, plays=200_000, followers=120_000),
            ),
        ),
        AxisData(
            role="competitor",
            label="競合「ラサーナ」検索",
            query="ラサーナ",
            requested=120,
            posts=() if with_failure else (_post("7", "ラサーナのヘアケア #PR", rank=1),),
            failed=with_failure,
            failure_code="MEDIA_TIKTOK_BOT_WALL" if with_failure else "",
        ),
    ]
    return measure(
        axes,
        brand="エムキュア",
        competitors=["ラサーナ"],
        keywords=["ヘアケア"],
    )


def _analysis() -> VideoAnalysisReport:
    return VideoAnalysisReport(
        results=(
            VideoAnalysisSuccess(
                video_id="5",
                url="https://www.tiktok.com/@mqure_official/video/5",
                cluster="正直レビュー/検証系",
                telop_text="ヘアケアの正解",
                frames_used=12,
                cost_usd=0.01,
            ),
            VideoAnalysisSuccess(
                video_id="6",
                url="https://www.tiktok.com/@someone/video/6",
                cluster="成分オタク系",
                telop_text="成分表を見る",
                frames_used=12,
                cost_usd=0.01,
            ),
            VideoAnalysisSuccess(
                video_id="7",
                url="https://www.tiktok.com/@someone/video/7",
                cluster="正直レビュー/検証系",
                telop_text="",
                frames_used=12,
                cost_usd=0.01,
            ),
            # Q5/表紙サムネ用（一般KW軸 TOP候補 1,2,3,4 の実フレーム・telopは空）
            *(
                VideoAnalysisSuccess(
                    video_id=video_id,
                    url=f"https://www.tiktok.com/@someone/video/{video_id}",
                    cluster="正直レビュー/検証系",
                    telop_text="",
                    frames_used=12,
                    cost_usd=0.01,
                    thumb_bytes=_THUMB,
                )
                for video_id in ("1", "2", "3", "4")
            ),
        ),
        failures=(
            VideoAnalysisFailure(
                video_id="9",
                url="https://www.tiktok.com/@x/video/9",
                stage="acquire",
                code="MEDIA_TIKTOK_BOT_WALL",
            ),
        ),
        skipped_video_ids=("10",),
        skip_reason="cost_cap",
        requested=5,
        cost_cap_usd=1.0,
        cost_usd_estimate=0.04,
        model_id="jp.anthropic.claude-haiku-4-5-20251001-v1:0",
        sampling_note="1秒1コマ",
        vocabulary=_VOCAB,
    )


def _plan(**kwargs: Any) -> DeckPlan:
    return build_deck_plan(
        _measurement(**kwargs),
        _analysis(),
        generated_on="2026-08-24",
        search_depth=120,
    )


# ---------------------------------------------------------------------------
# contract 準拠（スキーマ検証）
# ---------------------------------------------------------------------------


def test_deck_plan_roundtrips_through_contract_schema() -> None:
    plan = _plan()
    dumped = plan.model_dump_json()
    restored = DeckPlan.model_validate_json(dumped)
    assert restored == plan
    # deck_meta + slide_plan だけが契約（余計なトップレベルキーを増やさない）
    payload = json.loads(dumped)
    assert set(payload) == {
        "spec_name",
        "spec_version",
        "generated_on",
        "deck_meta",
        "slide_plan",
    }


def test_contract_rejects_wrong_payload_type_and_broken_rows() -> None:
    plan = _plan()
    payload = json.loads(plan.model_dump_json())
    # D類型スライドに C ペイロードを入れると弾く
    broken = json.loads(json.dumps(payload))
    d_index = next(i for i, s in enumerate(broken["slide_plan"]) if s["type"] == "D")
    broken["slide_plan"][d_index]["data"] = {
        "groups": [{"label": "x", "value_a": 1, "value_b": 2, "unit": ""}],
        "example": None,
    }
    with pytest.raises(ValidationError):
        DeckPlan.model_validate(broken)
    # D行の列数不一致を弾く
    broken2 = json.loads(json.dumps(payload))
    d_slide = broken2["slide_plan"][d_index]
    d_slide["data"]["rows"][0] = d_slide["data"]["rows"][0][:-1]
    with pytest.raises(ValidationError):
        DeckPlan.model_validate(broken2)


def test_contract_enforces_ben1_fixed_texts() -> None:
    plan = _plan()
    payload = json.loads(plan.model_dump_json())
    # 制約行（voice未計測）の完全一致が崩れたら弾く（誠実性ゲート）
    broken = json.loads(json.dumps(payload))
    broken["deck_meta"]["method_target_constraints"][2] = "音声も計測済み"
    with pytest.raises(ValidationError):
        DeckPlan.model_validate(broken)
    # 便1の H は CTA 必須
    broken2 = json.loads(json.dumps(payload))
    broken2["slide_plan"][-1]["data"]["cta"] = False
    with pytest.raises(ValidationError):
        DeckPlan.model_validate(broken2)


# ---------------------------------------------------------------------------
# U1裁定のデッキ構成
# ---------------------------------------------------------------------------


def test_composition_follows_u1_ruling_order() -> None:
    plan = _plan()
    types = [slide.type for slide in plan.slide_plan]
    q_numbers = [slide.q_number for slide in plan.slide_plan]
    assert types == ["A", "B", "D", "D", "C", "C", "D", "E", "H"]
    assert q_numbers == ["", "", "", "Q1", "Q2", "Q3", "Q4", "Q5", ""]
    # 章扉の Q一覧は収録された Q だけ（露出シェア導入は Q番号なしの「現状」枠）
    b_slide = plan.slide_plan[1]
    assert isinstance(b_slide.data, SlideDataB)
    assert [item.q_number for item in b_slide.data.q_list] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    # 表紙の段差サムネ2枚 = Q5 実フレーム上位2枚の再掲（新規取得なし）
    cover = plan.slide_plan[0]
    assert isinstance(cover.data, SlideDataA)
    q5 = plan.slide_plan[7]
    assert isinstance(q5.data, SlideDataE)
    assert [card.source_url for card in cover.data.thumbnail_pair] == [
        card.source_url for card in q5.data.cards[:2]
    ]
    # H は SUMMARY 扱い（PART ラベル無し）
    assert plan.slide_plan[-1].part is None


def test_cover_carries_method_target_and_fixed_constraint_line() -> None:
    plan = _plan()
    lines = plan.deck_meta.method_target_constraints
    assert len(lines) == 3
    assert "120本" in lines[0]
    assert "0回の動画も含む" in lines[1]
    assert lines[2] == VOICE_UNMEASURED_NOTE
    assert plan.deck_meta.brand_a.name == "エムキュア"
    assert plan.deck_meta.brand_b.name == "ラサーナ"


def test_h_slide_has_cta_and_summary_rows_from_findings() -> None:
    plan = _plan()
    h_slide = plan.slide_plan[-1]
    assert isinstance(h_slide.data, SlideDataH)
    assert h_slide.data.cta is True
    assert CTA_TEXT == (
        "上位10本の冒頭・価格・商品説明まで詳しく比較した事例が必要な方はご連絡ください。"
    )
    assert len(h_slide.data.summary_rows) == 6  # 現状 + Q1〜Q5
    assert [row.number for row in h_slide.data.summary_rows] == [1, 2, 3, 4, 5, 6]


def test_eg_rate_footnote_appears_once_at_first_eg_slide() -> None:
    plan = _plan()
    footnotes = [slide.footnote for slide in plan.slide_plan if slide.footnote]
    assert footnotes == [EG_RATE_FOOTNOTE]
    q1 = next(slide for slide in plan.slide_plan if slide.q_number == "Q1")
    assert q1.footnote == EG_RATE_FOOTNOTE


# ---------------------------------------------------------------------------
# 階層比較（Q1）: 取得失敗軸を「0本/N/A」の実測値として描かない（省略+開示）
# ---------------------------------------------------------------------------


def test_tier_slide_omitted_when_competitor_axis_failed_and_disclosed() -> None:
    # 競合軸が取得失敗 → Q1 は失敗側を「0本/N/A」で埋めず Q2 と同じく省略+開示
    measurement = _measurement(with_failure=True)
    analysis = _analysis()
    plan = build_deck_plan(measurement, analysis, generated_on="2026-08-24", search_depth=120)
    q_numbers = [slide.q_number for slide in plan.slide_plan if slide.q_number]
    assert "Q1" not in q_numbers
    assert "Q2" not in q_numbers
    audit = build_audit(measurement, analysis, plan, generated_on="2026-08-24", search_depth=120)
    assert "Q1" in audit["omitted_q_numbers"]
    assert "Q2" in audit["omitted_q_numbers"]
    failed_axes = [axis for axis in audit["axes"] if axis["failed"]]
    assert [axis["role"] for axis in failed_axes] == ["competitor"]
    # EG率脚注は Q1 固定でなく「EG率が初出するスライド」へ移り、初出1回を保つ
    footnoted = [slide for slide in plan.slide_plan if slide.footnote]
    assert [slide.footnote for slide in footnoted] == [EG_RATE_FOOTNOTE]
    assert footnoted[0].q_number == "Q3"


def test_tier_slide_omitted_when_brand_axis_failed_without_zero_fabrication() -> None:
    # ブランド軸が取得失敗 → 「〜が0本で最多」という虚偽タグを作文しない
    axes = [
        AxisData(
            role="general",
            label="一般KW「ヘアケア」検索",
            query="ヘアケア",
            requested=120,
            posts=(_post("1", "エムキュアのヘアケア #PR", rank=1),),
        ),
        AxisData(
            role="brand",
            label="ブランド名「エムキュア」検索",
            query="エムキュア",
            requested=120,
            posts=(),
            failed=True,
            failure_code="MEDIA_TIKTOK_BOT_WALL",
        ),
        AxisData(
            role="competitor",
            label="競合「ラサーナ」検索",
            query="ラサーナ",
            requested=120,
            posts=(_post("7", "ラサーナのヘアケア #PR", rank=1),),
        ),
    ]
    measurement = measure(axes, brand="エムキュア", competitors=["ラサーナ"], keywords=["ヘアケア"])
    plan = build_deck_plan(measurement, None, generated_on="2026-08-24", search_depth=120)
    q_numbers = [slide.q_number for slide in plan.slide_plan if slide.q_number]
    assert "Q1" not in q_numbers
    for slide in plan.slide_plan:
        if slide.tag is not None:
            assert "0本で最多" not in slide.tag.text
    audit = build_audit(measurement, None, plan, generated_on="2026-08-24", search_depth=120)
    assert "Q1" in audit["omitted_q_numbers"]


# ---------------------------------------------------------------------------
# クラスタ（Q3）: 分母の明記・未解析の不混入・省略時の開示
# ---------------------------------------------------------------------------


def test_cluster_slide_discloses_analyzed_denominator_and_failures() -> None:
    plan = _plan()
    q3 = next(slide for slide in plan.slide_plan if slide.q_number == "Q3")
    assert isinstance(q3.data, SlideDataC)
    # ブランド軸2本(5,6)・競合軸1本(7)が解析済み → 分母を明記
    assert "エムキュア 2本" in q3.lead
    assert "ラサーナ 1本" in q3.lead
    assert "分母=解析できた本数" in q3.lead
    assert q3.tag is not None
    assert "推定" in q3.tag.text
    # 失敗1 + スキップ1 = 2本は分母に混ぜず監査へ回す旨を開示
    assert "2本は分母に混ぜず" in q3.tag.text
    # 語彙全件が行になり、件数はラベルに明記される
    labels = [group.label for group in q3.data.groups]
    assert len(labels) == len(_VOCAB)
    assert any("正直レビュー/検証系（エムキュア 1本 / ラサーナ 1本）" in label for label in labels)


def test_cluster_and_top5_omitted_without_analysis_and_disclosed() -> None:
    # 動画解析なし → Q3（クラスタ）と Q5（実フレームが無い）を黙って埋めず省略+開示
    measurement = _measurement()
    plan = build_deck_plan(measurement, None, generated_on="2026-08-24", search_depth=120)
    q_numbers = [slide.q_number for slide in plan.slide_plan if slide.q_number]
    assert q_numbers == ["Q1", "Q2", "Q4"]
    assert plan.slide_plan[0].data is None  # 表紙サムネも捏造しない
    audit = build_audit(measurement, None, plan, generated_on="2026-08-24", search_depth=120)
    assert audit["omitted_q_numbers"] == ["Q3", "Q5"]
    assert audit["thumbnails"]["embedded"] == 0
    # TOP5候補（1,2,4,3 = 再生数順）が全て実フレーム無しで脱落したことを開示
    assert audit["thumbnails"]["dropped_video_ids"] == ["1", "2", "4", "3"]
    note = build_analysis_note(None)
    assert "未収録" in note
    assert "0件ではありません" in note


# ---------------------------------------------------------------------------
# Q4: 3経路と telop 分母・未計測表示
# ---------------------------------------------------------------------------


def test_keyword_slide_reports_three_routes_with_telop_denominator() -> None:
    plan = _plan()
    q4 = next(slide for slide in plan.slide_plan if slide.q_number == "Q4")
    assert isinstance(q4.data, SlideDataD)
    assert q4.data.columns[2:5] == [
        "キャプション 登場率/平均",
        "ハッシュタグ 登場率/平均",
        "テロップ 登場率/平均",
    ]
    rows = {row[0]: row for row in q4.data.rows}
    brand_row = rows["ブランド名「エムキュア」検索"]
    # telop: 解析2本中「ヘアケア」を含むのは1本 → 50% / 分母2本
    assert brand_row[4] == "50.0% / 0.5回（分母2本）"
    # 一般KW軸も解析済み（Q5サムネ前倒し解析の副産物）: 分母4本・言及0でも0%を明示
    general_row = rows["一般KW「ヘアケア」検索"]
    assert general_row[4] == "0.0% / 0.0回（分母4本）"
    assert "音声は未計測" in q4.lead


def test_keyword_slide_marks_unanalyzed_axis_as_unmeasured() -> None:
    # 解析が一切無い軸のテロップ経路は「未計測」（0%と偽らない）
    plan = build_deck_plan(_measurement(), None, generated_on="2026-08-24", search_depth=120)
    q4 = next(slide for slide in plan.slide_plan if slide.q_number == "Q4")
    assert isinstance(q4.data, SlideDataD)
    for row in q4.data.rows:
        assert row[4] == "未計測"


# ---------------------------------------------------------------------------
# Q5 / PR比較の実数
# ---------------------------------------------------------------------------


def test_top5_cards_use_verified_urls_and_embedded_real_frames() -> None:
    plan = _plan()
    q5 = next(slide for slide in plan.slide_plan if slide.q_number == "Q5")
    assert isinstance(q5.data, SlideDataE)
    cards = q5.data.cards
    assert len(cards) == 4  # 一般KW軸の取得は4本
    assert cards[0].metrics.plays == 50_000  # 再生数降順
    for card in cards:
        assert card.source_url.startswith("https://www.tiktok.com/")
        # 画像は解析フレームの data URI 埋め込み（media worker の network 参照拒否と両立）
        assert card.image.image_kind == "real_frame"
        assert card.image.data_uri.startswith("data:image/png;base64,")
    assert q5.tag is not None
    assert "実フレーム" in q5.tag.text


def test_top5_candidates_definition_is_shared_with_analysis_targets() -> None:
    # skill._analysis_targets と組成の Q5 選定は同一定義（ここが割れると画像が出ない）
    candidates = top5_candidates(_measurement())
    assert [post.video_id for post in candidates] == ["1", "2", "4", "3"]  # 再生数順


def test_top5_drops_oversized_thumbs_by_budget_and_discloses() -> None:
    # per-image budget（120KB）超の実フレームはカード化せず監査に開示する
    measurement = _measurement()
    analysis = _analysis()
    oversized = make_png_bytes() + b"\x00" * (200 * 1024)
    results = tuple(
        VideoAnalysisSuccess(
            video_id=result.video_id,
            url=result.url,
            cluster=result.cluster,
            telop_text=result.telop_text,
            frames_used=result.frames_used,
            cost_usd=result.cost_usd,
            thumb_bytes=oversized if result.video_id == "1" else result.thumb_bytes,
        )
        for result in analysis.results
    )
    analysis = VideoAnalysisReport(
        results=results,
        failures=analysis.failures,
        skipped_video_ids=analysis.skipped_video_ids,
        skip_reason=analysis.skip_reason,
        requested=analysis.requested,
        cost_cap_usd=analysis.cost_cap_usd,
        cost_usd_estimate=analysis.cost_usd_estimate,
        model_id=analysis.model_id,
        sampling_note=analysis.sampling_note,
        vocabulary=analysis.vocabulary,
    )
    plan = build_deck_plan(measurement, analysis, generated_on="2026-08-24", search_depth=120)
    q5 = next(slide for slide in plan.slide_plan if slide.q_number == "Q5")
    assert isinstance(q5.data, SlideDataE)
    assert len(q5.data.cards) == 3  # video 1 が budget 超で脱落
    audit = build_audit(measurement, analysis, plan, generated_on="2026-08-24", search_depth=120)
    assert audit["thumbnails"] == {"embedded": 3, "dropped_video_ids": ["1"]}


def test_pr_slide_reports_both_groups_with_same_metrics() -> None:
    plan = _plan()
    q2 = next(slide for slide in plan.slide_plan if slide.q_number == "Q2")
    assert isinstance(q2.data, SlideDataC)
    labels = [group.label for group in q2.data.groups]
    assert labels == [
        "#PR表記あり 本数",
        "#PR表記なし 本数",
        "#PR表記あり 平均EG率",
        "#PR表記なし 平均EG率",
    ]
    counts = {group.label: (group.value_a, group.value_b) for group in q2.data.groups}
    assert counts["#PR表記あり 本数"] == (1.0, 1.0)
    assert counts["#PR表記なし 本数"] == (1.0, 0.0)
    assert q2.tag is not None
    assert "オーガニック" in q2.tag.text  # 「断定しない」の注記側でのみ使用


def test_exposure_intro_slide_uses_measured_share_numbers() -> None:
    plan = _plan()
    intro = plan.slide_plan[2]
    assert intro.q_number == ""
    assert isinstance(intro.data, SlideDataD)
    own_row = next(row for row in intro.data.rows if row[1] == "エムキュア")
    assert own_row[2] == "1本"  # caption言及のみ（公式ハンドル未入力なので author 判定なし）
    assert own_row[3] == "4本中 25.0%"
    assert intro.tag is not None
    assert "エムキュア関連は1本（25.0%）" in intro.tag.text


# ---------------------------------------------------------------------------
# 要点3行・部分結果・全滅
# ---------------------------------------------------------------------------


def test_summary_lines_are_three_lines_from_measured_numbers() -> None:
    lines = build_summary_lines(_measurement())
    assert len(lines) == 3
    assert "上位4本中" in lines[0]
    assert any("#PR表記あり" in line for line in lines)


def test_build_deck_plan_requires_some_material() -> None:
    axes = [
        AxisData(
            role="general",
            label="一般KW「x」検索",
            query="x",
            requested=120,
            posts=(),
            failed=True,
            failure_code="MEDIA_TIKTOK_BOT_WALL",
        )
    ]
    measurement = measure(axes, brand="A", competitors=["B"], keywords=["x"])
    with pytest.raises(DeckPlanBuildError):
        build_deck_plan(measurement, None, generated_on="2026-08-24", search_depth=120)


# ---------------------------------------------------------------------------
# レンダラ境界（FMT 正レンダラでのエスケープは tests/skills/omiyage_report/test_fmt_html.py、
# エンジン出力 → レンダラ入力の端から端は test_integration.py が固定する）
# ---------------------------------------------------------------------------


def test_engine_plan_passes_fmt_renderer_contract() -> None:
    """エンジンの計測JSONが、レンダラ入力契約（fmt.validate_deck_content）をそのまま通る。"""
    from teamagent.skills.omiyage_report.fmt.contract import validate_deck_content
    from teamagent.skills.omiyage_report.fmt.spec import load_fmt_spec

    plan = _plan()
    content = validate_deck_content(json.loads(plan.model_dump_json()), load_fmt_spec())
    assert [slide.type for slide in content.slides] == [s.type for s in plan.slide_plan]
    # 「Q番号なし」は空文字（エンジン）→ None（レンダラ）へ正規化される
    assert content.slides[2].q_number is None
    # 入れ子 metrics（plays）→ flat（views）へ正規化される
    q5 = content.slides[7]
    engine_q5 = plan.slide_plan[7]
    assert isinstance(engine_q5.data, SlideDataE)
    assert [card.views for card in q5.data.cards] == [  # type: ignore[union-attr]
        card.metrics.plays for card in engine_q5.data.cards
    ]
    # U2脚注（EG率定義）がレンダラ側 Slide にも到達する
    assert any(slide.footnote == EG_RATE_FOOTNOTE for slide in content.slides)


# ---------------------------------------------------------------------------
# 階層比較（Q1）: 平均再生は Q5 と同じ 3 桁区切りの整数で出す
# ---------------------------------------------------------------------------


def _tier_format_measurement() -> OmiyageMeasurement:
    """ナノ帯の平均再生が割り切れない（14011.666…）実測を組む。"""
    nano = tuple(
        _post(video_id, "エムキュアの検証", rank=rank, plays=plays, followers=5_000)
        for video_id, rank, plays in (("11", 1, 14_000), ("12", 2, 14_012), ("13", 3, 14_023))
    )
    mega = (_post("14", "エムキュア紹介", rank=4, plays=1_300_000, followers=800_000),)
    axes = [
        AxisData(
            role="brand",
            label="ブランド名「エムキュア」検索",
            query="エムキュア",
            requested=120,
            posts=(*nano, *mega),
        ),
        AxisData(
            role="competitor",
            label="競合「ラサーナ」検索",
            query="ラサーナ",
            requested=120,
            posts=(_post("15", "ラサーナ検証", rank=1, plays=88_722, followers=5_000),),
        ),
    ]
    return measure(axes, brand="エムキュア", competitors=["ラサーナ"], keywords=["ヘアケア"])


def test_tier_slide_avg_plays_use_thousands_separator_and_integer_rounding() -> None:
    plan = build_deck_plan(
        _tier_format_measurement(),
        None,
        generated_on="2026-08-24",
        search_depth=120,
    )
    q1 = next(slide for slide in plan.slide_plan if slide.q_number == "Q1")
    assert isinstance(q1.data, SlideDataD)
    by_tier = {row[0]: row for row in q1.data.rows}
    # 自社側の平均再生（列2）: 14011.666… は「14,012」、1300000.0 は「1,300,000」
    assert by_tier["ナノ（〜1万）"][2] == "14,012"
    assert by_tier["メガ（50万〜）"][2] == "1,300,000"
    # 競合側の平均再生（列5）も同じ整形
    assert by_tier["ナノ（〜1万）"][5] == "88,722"
    # 0本の帯は 0 本 / N/A（捏造しない）
    assert by_tier["マイクロ（1〜10万）"][1:4] == ["0本", "N/A", "N/A"]
    # 平均EG率は従来どおり小数付きの % 表記を維持する
    assert by_tier["ナノ（〜1万）"][3].endswith("%")
    assert "." in by_tier["ナノ（〜1万）"][3]
    # 素の float（14011.6 / 1300000.0）が表へ出ない
    flat = [cell for row in q1.data.rows for cell in row]
    assert not any(cell.replace(",", "").endswith(".0") for cell in flat)
    assert "14011.7" not in flat
