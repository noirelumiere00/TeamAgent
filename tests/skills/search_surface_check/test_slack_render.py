"""検索上位チェックの直接投稿（Block Kit）のテスト。本番の形のデータ（slack_prod_shape）で組む。

確かめること（A＝1 段目・B＝2 段目の追記）:
(1) 生の ``[名](url)``・``**太字**``・行頭の「- 」が出ない
(2) リンクは ``<url|名前>`` で、自前の URL（投稿・レポート）だけ
(3) ``<!here>``・``<@U…>``・偽装リンクを含む第三者の文字列（KW・表示名・本文・LLM の文）が無害化される
(4) Block Kit の上限（50 blocks・section 3000 字・header 150 字）を超えない（大きな入力でも）
(5) 通知文（text）に結論とレポートの URL が入る
あわせて、小俣さんの「前と違う・古い？」への答え（実測の時刻・フォロワー帯・常連の縦並び・
クライアントの節と照合の範囲）と、断定語（勝ち筋・打ち手）を使わないことを固定する。
"""

from __future__ import annotations

import json

import pytest

from teamagent.skills.search_surface_check.schema import (
    ConclusionPoint,
    KwSurface,
    SearchSurfaceCheckInput,
    SurfaceConclusion,
)
from teamagent.skills.search_surface_check.slack_render import (
    COVER_ONLY_ROW,
    FOLLOWUP_CAVEAT,
    REUSED_NOTE,
    followup_message,
    followup_rows,
    surface_message,
)
from teamagent.skills.search_surface_check.summary import followup_notice_line
from tests.skills._shared.slack_blocks_checks import (
    assert_limits,
    assert_only_own_links,
    assert_well_formed,
    body,
    header_texts,
    mrkdwn_texts,
)
from tests.skills.search_surface_check import slack_prod_shape as shape


def _a_links() -> set[str]:
    out = shape.a_output()
    return {p.url for p in out.surfaces[0].posts} | {shape.A_REPORT}


def _b_links() -> set[str]:
    return {r.url for r in shape.b_output().videos} | {shape.B_REPORT}


def _section_with(blocks: list[dict[str, object]], needle: str) -> str:
    return next(t for t in mrkdwn_texts(blocks) if needle in t)  # type: ignore[arg-type]


# ── A: 1 段目 ─────────────────────────────────────────────────────────


def test_a_is_well_formed_block_kit() -> None:
    msg = surface_message(shape.a_output(), shape.a_input())
    assert msg is not None
    assert_well_formed(msg.blocks, _a_links())
    assert header_texts(msg.blocks) == [f"検索上位チェック「{shape.KEYWORD}」"]


def test_a_shows_the_measured_time_and_what_was_asked_before() -> None:
    """「前と違う・古い？」: 実測の時刻（再検索で顔ぶれが変わる）・フォロワー帯・常連の縦並び。"""
    msg = surface_message(shape.a_output(), shape.a_input())
    assert msg is not None
    whole = body(msg.blocks)
    assert "2026-09-28 17:00 実測（依頼のたびに検索し直した値）" in whole
    tiers = _section_with(msg.blocks, "フォロワー帯")
    assert "• 10万〜100万人 6/30本・再生の66%" in tiers
    holders = _section_with(msg.blocks, "常連")
    assert holders.splitlines()[1:] == [
        "• @kurashiru.com（メディア・41.2万人） 4枠: 8・9・12・26位",
        "• @spice_koki（一般・2,930人） 3枠: 2・11・22位",
        "• @musuicurry（インフルエンサー・13.8万人） 3枠: 6・14・16位",
    ]


def test_a_client_section_has_only_client_facts_and_the_matching_scope() -> None:
    msg = surface_message(shape.a_output(), shape.a_input())
    assert msg is not None
    texts = mrkdwn_texts(msg.blocks)
    i = next(i for i, t in enumerate(texts) if t.startswith(":office: *GABAN の現状*"))
    assert texts[i] == ":office: *GABAN の現状*\n• 上位30本に「GABAN」に触れた投稿は無し"
    assert texts[i + 1].startswith("照合したのは本文・タグにある「GABAN」の表記だけです")
    assert "公式アカウント" in texts[i + 1]
    # 上位全体の数字はクライアントの節ではなく別の節に、段階の語つきで
    overall = _section_with(msg.blocks, "上位30本全体")
    assert f"• 本文かタグに「{shape.KEYWORD}」の語をすべて含む: 7/30本（少数派）" in overall
    assert "• 直近90日の投稿: 5/30本（少数派）・投稿時期の中央値 11か月前" in overall
    assert "• PR表記のある投稿（ブランドは問わない）: 1・6・12・14・16位" in overall
    # 保存率の上位は集計の値（LLM の丸めた「1.6～2.9%」ではなく）を順位のリンクつきで
    assert "2.88%" in overall and "2.38%" in overall and "1.62%" in overall


def test_a_labels_llm_text_and_drops_assertive_labels() -> None:
    msg = surface_message(shape.a_output(), shape.a_input())
    assert msg is not None
    whole = body(msg.blocks)
    assert ":mag: *結論（AI の要約）*" in whole
    assert "*上位に見られる切り口* （AI の分類・該当する順位）" in whole
    reading = _section_with(msg.blocks, "AI の読み（次の一手の候補）")
    assert "（根拠: <https://www.tiktok.com/@user8013099312681/" in reading
    for word in ("勝ち筋", "打ち手", "最有力", "空白:"):
        assert word not in whole
    # winning・gap の全文はレポートへ（Slack に二重に書かない）
    assert "入れ替わりが遅い" not in whole and "2,930フォロワーながら" not in whole


def test_a_top_posts_are_one_line_each_with_links_and_no_captions() -> None:
    out = shape.a_output()
    msg = surface_message(out, shape.a_input())
    assert msg is not None
    top = _section_with(msg.blocks, "*上位5本*")
    lines = top.splitlines()[1:]
    assert len(lines) == 5
    assert lines[0] == (
        "*1位* <https://www.tiktok.com/@gonosara/video/7165798692291644674|@gonosara>"
        "（クリエイター・5.2万人）［PR表記］ 35.1万回・保存0.8%・3年10か月前"
    )
    for post in out.surfaces[0].posts:
        assert post.desc not in body(msg.blocks)  # キャプション（第三者の文字列）は出さない


def test_a_followup_notice_and_report_link_are_at_the_end() -> None:
    msg = surface_message(shape.a_output(), shape.a_input())
    assert msg is not None
    texts = mrkdwn_texts(msg.blocks)
    assert f":hourglass_flowing_sand: {followup_notice_line(5)}" in texts
    assert (
        f":page_facing_up: <{shape.A_REPORT}|レポートを開く> （全30本の一覧つき・7日有効）" in texts
    )
    assert texts[-1] == "概算 $0.0186"


def test_a_fallback_text_has_conclusion_client_report_and_post_urls() -> None:
    out = shape.a_output()
    msg = surface_message(out, shape.a_input())
    assert msg is not None
    first = msg.text.splitlines()[0]
    assert first.startswith(
        f"検索上位チェック「{shape.KEYWORD}」TikTok 上位30本（2026-09-28 17:00 実測）: "
    )
    assert "クリエイターとインフルエンサーが再生の74%を占める" in first
    assert "GABAN: 上位30本に「GABAN」に触れた投稿は無し" in msg.text
    assert f"<{shape.A_REPORT}>" in msg.text
    for post in sorted(out.surfaces[0].posts, key=lambda p: p.rank)[:5]:
        assert f"{post.rank}位 <{post.url}>" in msg.text  # 会話の履歴から「2位の動画」を辿れる
    assert_only_own_links([msg.text], _a_links())


def test_a_hostile_third_party_strings_are_neutralized() -> None:
    out, inp = shape.hostile_a()
    msg = surface_message(out, inp)
    assert msg is not None
    allowed = {p.url for p in out.surfaces[0].posts if "evil" not in p.url and "|" not in p.url}
    allowed |= {shape.A_REPORT}
    assert_well_formed(msg.blocks, allowed)
    assert_only_own_links([msg.text], allowed)
    raw = json.dumps(msg.payload(), ensure_ascii=False)
    for bad in ("<!here>", "<!channel>", "<@U0EVIL", "<https://evil", "evil.example/@x", "|偽名>"):
        assert bad not in raw, bad
    assert "&lt;!here&gt;" in raw and "A&amp;B" in raw  # 表示は元の字のまま（実体参照）


def test_a_compact_many_surfaces_stay_within_limits() -> None:
    """5 語×2 媒体・長い結論でも 50 blocks・3000 字・header 150 字に収まり、レポートは残る。"""
    base = shape.a_output()
    surfaces: list[KwSurface] = []
    for i in range(5):
        for platform in ("tiktok", "instagram"):
            s = base.surfaces[0].model_copy(deep=True)
            s.keyword = f"とても長い検索キーワード{i}" * 8
            s.platform = platform  # type: ignore[assignment]
            assert s.conclusion is not None
            s.conclusion.headline = "長い結論" * 500
            surfaces.append(s)
    base.surfaces = surfaces
    base.keywords = list(dict.fromkeys(s.keyword for s in surfaces))
    inp = SearchSurfaceCheckInput(
        keywords=base.keywords, platforms=["tiktok", "instagram"], client_name=shape.CLIENT
    )
    msg = surface_message(base, inp)
    assert msg is not None
    assert_limits(msg.blocks)
    assert_well_formed(msg.blocks, {p.url for p in surfaces[0].posts} | {shape.A_REPORT})
    assert len(header_texts(msg.blocks)[0]) <= 150
    assert f"<{shape.A_REPORT}|レポートを開く>" in body(msg.blocks)
    assert len(msg.text) <= 3000


def test_a_single_surface_with_huge_llm_lists_is_clipped() -> None:
    out = shape.a_output()
    c = out.surfaces[0].conclusion
    assert c is not None
    c.angles = [ConclusionPoint(text="切り口" * 40, ranks=[1, 2]) for _ in range(200)]
    c.actions = [ConclusionPoint(text="読み" * 400, ranks=[27]) for _ in range(20)]
    msg = surface_message(out, shape.a_input())
    assert msg is not None
    assert_limits(msg.blocks)
    assert any(t.endswith("…") for t in mrkdwn_texts(msg.blocks))


def test_a_rule_conclusion_is_not_labeled_as_ai() -> None:
    out = shape.a_output()
    out.surfaces[0].conclusion = SurfaceConclusion(
        headline="上位30本の最多は一般の12本", generated_by="rule"
    )
    msg = surface_message(out, shape.a_input())
    assert msg is not None
    assert ":mag: *結論（集計から）*\n上位30本の最多は一般の12本" in mrkdwn_texts(msg.blocks)
    assert "AI の" not in body(msg.blocks)


def test_a_without_surfaces_is_none() -> None:
    out = shape.a_output()
    out.surfaces = []
    assert surface_message(out, shape.a_input()) is None


def test_a_unlinkable_report_url_raises_so_the_caller_falls_back_to_text() -> None:
    out = shape.a_output()
    out.report_url = "https://connect.newstv.co.jp/r/全角"
    with pytest.raises(ValueError):
        surface_message(out, shape.a_input())


# ── B: 2 段目の追記 ──────────────────────────────────────────────────


def test_b_is_well_formed_block_kit() -> None:
    msg = followup_message(shape.b_output())
    assert msg is not None
    assert_well_formed(msg.blocks, _b_links())
    assert header_texts(msg.blocks) == [f"上位5本の動画の中身「{shape.KEYWORD}」"]
    assert "2026-09-28 15:42 実測の検索上位チェックの続き" in body(msg.blocks)


def test_b_stage_table_uses_denominators_and_hides_cta_types() -> None:
    msg = followup_message(shape.b_output())
    assert msg is not None
    table = next(b for b in msg.blocks if b.get("fields"))
    fields = [f["text"] for f in table["fields"]]
    assert fields == [
        "*全員に共通（4/4本）*",
        "冒頭にテロップ・テロップに検索KW・CTA あり・テンポ ふつう",
        "*多数派*",
        "発話に検索KW 3/4本・ナレーション 3/4本",
        "*半数（2/4本）*",
        "フックが問題提起（2・4位）",
        "*少数派*",
        "フックがビジュアル 1/4本（1位）・フックがPOV 1/4本（3位）",
        "*0/4本*",
        "流行の音源",
    ]
    # CTA の種類（レシピ動画で「来店」が多数派に見える分類）は検証まで出さない
    assert "来店" not in body(msg.blocks)


def test_b_video_lines_omit_common_items_and_use_words_not_marks() -> None:
    msg = followup_message(shape.b_output())
    assert msg is not None
    lines = _section_with(msg.blocks, "*1本ずつ*").splitlines()
    assert lines[0] == ":clipboard: *1本ずつ* （4本すべてに共通の項目は省略）"
    assert lines[1] == (
        "*1位* <https://www.tiktok.com/@gonosara/video/7400000000000000101|@gonosara>"
        "　フック: ビジュアル／発話の検索KW なし／ナレーション なし／59秒"
    )
    assert lines[5].endswith(COVER_ONLY_ROW)
    joined = "\n".join(lines)
    assert "冒頭テロップ" not in joined and "テンポ" not in joined  # 全員に同じ項目は省く
    assert "○" not in joined and "×" not in joined


def test_b_save_reason_is_labeled_as_ai_and_winning_goes_to_the_report() -> None:
    msg = followup_message(shape.b_output())
    assert msg is not None
    save = _section_with(msg.blocks, "保存率の高い2本")
    assert save.splitlines()[1].startswith("AI の読み: 保存率の高い2位と3位は")
    assert "保存につながっている" not in body(msg.blocks)  # winning（因果の言い切り）は出さない


def test_b_fallback_text_and_notes() -> None:
    out = shape.b_output()
    msg = followup_message(out)
    assert msg is not None
    assert msg.text.splitlines()[0] == (
        f"上位5本の動画の中身「{shape.KEYWORD}」TikTok: "
        "スパイス選びと分量を明確にした、初心者向けの実践的なレシピ動画が上位を占める"
    )
    assert f"<{shape.B_REPORT}>" in msg.text
    assert all(f"{r.rank}位 <{r.url}>" in msg.text for r in out.videos)
    last = msg.blocks[-1]["elements"]
    assert [e["text"] for e in last] == [FOLLOWUP_CAVEAT, "概算 $0.3596"]


def test_b_reused_says_so_and_costs_nothing() -> None:
    msg = followup_message(shape.b_output(), reused=True)
    assert msg is not None
    assert msg.blocks[1]["elements"][0]["text"] == REUSED_NOTE
    assert msg.blocks[-1]["elements"][-1]["text"] == "概算 $0.0000（前回の分析を使い回しました）"
    assert msg.text.startswith("（24 時間以内の同じ分析の結果です）\n")


def test_b_hostile_third_party_strings_are_neutralized() -> None:
    out = shape.hostile_b()
    msg = followup_message(out)
    assert msg is not None
    allowed = {r.url for r in out.videos if "evil" not in r.url} | {shape.B_REPORT}
    assert_well_formed(msg.blocks, allowed)
    assert_only_own_links([msg.text], allowed)
    raw = json.dumps(msg.payload(), ensure_ascii=False)
    for bad in ("<!here>", "<!channel>", "<@U0EVIL", "<https://evil", "evil.example/@x"):
        assert bad not in raw, bad


def test_b_only_for_analyzed_results() -> None:
    out = shape.b_output()
    for status in ("quota_exhausted", "all_failed", "no_videos"):
        assert followup_message(out.model_copy(update={"status": status})) is None
    assert followup_message(out.model_copy(update={"videos": []})) is None
    assert followup_message(out.model_copy(update={"digest": None})) is None


def test_b_report_failure_is_said_in_words() -> None:
    msg = followup_message(shape.b_output().model_copy(update={"report_url": None}))
    assert msg is not None
    assert (
        ":page_facing_up: レポートの発行に失敗しました（上の要約は分析結果どおりです）"
        in mrkdwn_texts(msg.blocks)
    )


def test_b_ten_videos_stay_within_limits() -> None:
    out = shape.b_output()
    rows = [r.model_copy(update={"rank": i + 1}) for i, r in enumerate(out.videos * 2)]
    digest = out.digest
    assert digest is not None
    out = out.model_copy(
        update={"videos": rows, "digest": digest.model_copy(update={"watched": 8, "requested": 10})}
    )
    msg = followup_message(out)
    assert msg is not None
    assert_limits(msg.blocks)


# ── 1 本 1 行の値（分析結果から）──────────────────────────────────────


def test_followup_rows_from_analyzed_videos() -> None:
    from teamagent.skills.video_algorithm.schema import (
        AnalyzedVideo,
        KeywordMatch,
        TelopItem,
        VideoMeta,
        VideoVSEOAnalysis,
    )

    def meta(rank: int) -> VideoMeta:
        return VideoMeta(rank=rank, author=f"a{rank}", url=shape.tiktok_url(f"a{rank}", str(rank)))

    watched = AnalyzedVideo(
        meta=meta(1),
        analysis=VideoVSEOAnalysis(
            duration_sec=59.4,
            hook_type="problem",
            telops=[TelopItem(sec=0.5, text="KW", kw_match=True)],
            spoken_keywords=[KeywordMatch(keyword="KW", matched=True)],
            cut_count=12,
            pacing="moderate",
            cta_type=["visit"],
            has_narration=True,
        ),
    )
    cover = AnalyzedVideo(
        meta=meta(2), analysis=VideoVSEOAnalysis(), error="動画取得失敗・サムネのみ軽量分析"
    )
    failed = AnalyzedVideo(meta=meta(3), analysis=None, error="取得失敗")
    rows = followup_rows([watched, cover, failed])
    assert [r.state for r in rows] == ["watched", "cover_only", "failed"]
    first = rows[0]
    assert (first.hook, first.opening_telop, first.telop_kw, first.spoken_kw) == (
        "問題提起",
        True,
        True,
        True,
    )
    assert (first.duration_sec, first.cut_count, first.pacing, first.has_cta, first.narration) == (
        59,
        12,
        "ふつう",
        True,
        True,
    )
    assert rows[1].hook == "" and rows[2].url == shape.tiktok_url("a3", "3")
