"""検索上位チェックの直接投稿（Block Kit）のテスト。本番の形のデータ（slack_prod_shape）で組む。

確かめること（A＝1 段目・B＝2 段目の追記）:
(1) 生の ``[名](url)``・``**太字**``・行頭の「- 」が出ない
(2) リンクは ``<url|名前>`` で、自前の URL（投稿・レポート）だけ。順位のリンクは**その順位の投稿**へ
(3) ``<!here>``・``<@U…>``・偽装リンクを含む第三者の文字列（KW・表示名・本文・LLM の文）が無害化される
(4) Block Kit の上限（50 blocks・section 3000 字・header 150 字・合計 12,000 字）を超えない
(5) 最上位の text（スクリーンリーダーは blocks を読まずこれだけを読む）に blocks の全文が入る
あわせて、小俣さんの「前と違う・古い？」への答え（集計の時刻と取り方・フォロワー帯・常連の縦並び・
クライアントの節と照合の範囲）、落としてはいけない注記（注意・取得できなかった媒体・集計外・
分析できず・今月の残り）の文言、断定語（勝ち筋・打ち手）を使わないことを固定する。
"""

from __future__ import annotations

import json

import pytest

from teamagent.skills._shared.slack_blocks import (
    MAX_FALLBACK_TEXT,
    MAX_TOTAL_TEXT,
    block_text,
    text_size,
)
from teamagent.skills.search_surface_check.insights import compute_facts
from teamagent.skills.search_surface_check.schema import (
    ConclusionPoint,
    KwSurface,
    SearchSurfaceCheckInput,
    SurfaceConclusion,
)
from teamagent.skills.search_surface_check.slack_render import (
    ACQUIRED_NOTE,
    ACQUIRED_WITH_IG_NOTE,
    COVER_ONLY_ROW,
    FAILED_ROW,
    FOLLOWUP_CAVEAT,
    LIVE_NOTE,
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


def _url(rank: int) -> str:
    """A の順位の投稿 URL（順位のリンクの宛先の照合用）。"""
    return next(p.url for p in shape.a_posts() if p.rank == rank)


def _assert_text_has_every_block(msg: object, *, skip: int) -> None:
    """最上位の text に、blocks の中身（要点の行に入れた見出し・結論を除く）がすべて入っている。"""
    blocks = msg.blocks  # type: ignore[attr-defined]
    text = msg.text  # type: ignore[attr-defined]
    for block in blocks[skip:]:
        flat = block_text(block)
        if flat and ":mag: *結論" not in flat:
            assert flat in text, flat


# ── A: 1 段目 ─────────────────────────────────────────────────────────


def test_fixture_facts_are_computed_from_the_posts_and_match_the_production_sample() -> None:
    """見本の集計は投稿から計算した値で、samples.md の A（17:00）の集計と同じになる。"""
    facts = shape.a_facts()
    assert [(c.category, c.count, round(c.play_share * 100)) for c in facts.categories] == [
        ("ugc", 12, 14),
        ("creator", 9, 40),
        ("influencer", 5, 34),
        ("media", 4, 12),
    ]
    assert [(h.author, h.ranks) for h in facts.holders] == [
        ("kurashiru.com", [8, 9, 12, 26]),
        ("spice_koki", [2, 11, 22]),
        ("musuicurry", [6, 14, 16]),
    ]
    assert (facts.small_in_top10, facts.top10_n) == (3, 10)
    assert (facts.median_plays, facts.reach_ratio_median) == (66_000, 2.78)
    assert facts.median_save_rate_pct == 0.71
    assert [(s.rank, s.save_rate_pct) for s in facts.save_leaders] == [
        (27, 2.88),
        (11, 2.38),
        (21, 1.62),
    ]
    assert (facts.recent_90d, facts.median_duration_sec, facts.kw_in_text) == (5, 60, 7)
    assert 330 <= (facts.median_age_days or 0) < 360  # 11か月前
    assert [(t.tag, t.count) for t in facts.top_tags[:5]] == [
        ("スパイスカレー", 19),
        ("カレー", 9),
        ("スパイス", 6),
        ("簡単レシピ", 6),
        ("レシピ", 5),
    ]
    assert facts.pr_ranks == [1, 6, 12, 14, 16]
    # 帯は投稿から数えた値（10万人以上＝常連のクラシル 4・ニキ 3＋4・5 位）
    assert [(t.tier, t.count) for t in facts.tiers] == [
        ("10万〜100万人", 9),
        ("1万〜10万人", 15),
        ("1万人未満", 6),
    ]
    # A の出力の集計は、その出力の投稿から skill と同じ計算をした値（手で書いた値ではない）
    out = shape.a_output()
    assert out.surfaces[0].facts == compute_facts(
        out.surfaces[0].posts,
        keyword=shape.KEYWORD,
        client_name=shape.CLIENT,
        now_epoch=shape.A_MEASURED,
    )


def test_a_is_well_formed_block_kit() -> None:
    msg = surface_message(shape.a_output(), shape.a_input())
    assert msg is not None
    assert_well_formed(msg.blocks, _a_links())
    assert header_texts(msg.blocks) == [f"検索上位チェック「{shape.KEYWORD}」"]
    assert text_size(msg.blocks) <= MAX_TOTAL_TEXT


def test_a_shows_the_measured_time_and_what_was_asked_before() -> None:
    """「前と違う・古い？」: 集計の時刻と取り方・フォロワー帯・常連の縦並び。"""
    msg = surface_message(shape.a_output(), shape.a_input())
    assert msg is not None
    assert msg.blocks[1]["elements"][0]["text"] == (
        f"TikTok 上位30本・2026-09-28 17:00 {LIVE_NOTE}"
    )
    tiers = _section_with(msg.blocks, "フォロワー帯")
    assert tiers.splitlines()[1:] == [
        "• 10万〜100万人 9/30本・再生の46%",
        "• 1万〜10万人 15/30本・再生の42%",
        "• 1万人未満 6/30本・再生の12%",
    ]
    holders = _section_with(msg.blocks, "常連")
    assert holders.splitlines()[1:] == [
        "• @kurashiru.com（メディア・41.2万人） 4枠: 8・9・12・26位",
        "• @spice_koki（一般・2,930人） 3枠: 2・11・22位",
        "• @musuicurry（インフルエンサー・13.8万人） 3枠: 6・14・16位",
    ]


@pytest.mark.parametrize(
    ("source", "note"),
    [
        ("direct", LIVE_NOTE),
        ("acquire_job", ACQUIRED_NOTE),  # 以前の取得ジョブの成果物を読んだだけ
        ("", "集計"),  # 取り方が分からない出力では言い切らない
    ],
)
def test_a_measured_note_depends_on_how_tiktok_was_fetched(source: str, note: str) -> None:
    out = shape.a_output().model_copy(update={"tiktok_source": source})
    msg = surface_message(out, shape.a_input())
    assert msg is not None
    context = msg.blocks[1]["elements"][0]["text"]
    assert context == f"TikTok 上位30本・2026-09-28 17:00 {note}"
    assert context in msg.text
    if source != "direct":
        assert "検索し直した値" not in body(msg.blocks) and "検索し直した値" not in msg.text


def test_a_measured_note_for_mixed_and_instagram_only_surfaces() -> None:
    base = shape.a_output()
    ig = base.surfaces[0].model_copy(update={"platform": "instagram"})
    mixed = base.model_copy(
        update={"surfaces": [base.surfaces[0], ig], "tiktok_source": "acquire_job"}
    )
    msg = surface_message(mixed, shape.a_input())
    assert msg is not None
    assert msg.blocks[1]["elements"][0]["text"] == (
        f"TikTok・Instagram・2026-09-28 17:00 {ACQUIRED_WITH_IG_NOTE}"
    )
    # Instagram だけ（毎回 Apify で検索する）なら「検索し直した値」と書ける
    only_ig = base.model_copy(update={"surfaces": [ig], "tiktok_source": ""})
    msg = surface_message(only_ig, shape.a_input())
    assert msg is not None
    assert msg.blocks[1]["elements"][0]["text"] == (
        f"Instagram 上位30本・2026-09-28 17:00 {LIVE_NOTE}"
    )


def test_a_client_section_has_only_client_facts_and_the_matching_scope() -> None:
    msg = surface_message(shape.a_output(), shape.a_input())
    assert msg is not None
    texts = mrkdwn_texts(msg.blocks)
    i = next(i for i, t in enumerate(texts) if t.startswith(":office: *GABAN の現状*"))
    assert texts[i] == ":office: *GABAN の現状*\n• 上位30本に「GABAN」に触れた投稿は無し"
    assert texts[i + 1] == (
        "照合したのは本文・タグにある「GABAN」の表記だけです"
        "（ほかの表記・公式アカウントは照合していません）"
    )


def test_a_client_accounts_are_matched_and_marked() -> None:
    """クライアントのアカウントを渡すと、その投稿の順位（リンク先はその投稿）と印が出る。"""
    out, inp = shape.a_problem_case()
    msg = surface_message(out, inp)
    assert msg is not None
    client_url = out.surfaces[0].posts[2].url
    texts = mrkdwn_texts(msg.blocks)
    i = next(i for i, t in enumerate(texts) if t.startswith(":office: *GABAN の現状*"))
    assert texts[i] == (
        ":office: *GABAN の現状*\n"
        f"• クライアントの投稿: <{client_url}|3位>（上位30本に 1本）\n"
        "• 上位30本に「GABAN」に触れた投稿は無し"
    )
    assert texts[i + 1] == (
        "照合したのは本文・タグにある「GABAN」の表記とアカウント @gaban_official だけです"
        "（ほかの表記は照合していません）"
    )
    top = _section_with(msg.blocks, "*上位5本*").splitlines()
    assert top[3] == (
        f"*3位* <{client_url}|@gaban_official>（クリエイター・4.8万人）［クライアント］"
        " 15万回・保存0.7%・40日前"
    )


def test_a_notes_that_must_not_be_dropped() -> None:
    """注意（warnings）・取得できなかった媒体・2 段目の予告は、文言どおり blocks と text に残る。"""
    out, inp = shape.a_problem_case()
    msg = surface_message(out, inp)
    assert msg is not None
    missing = (
        f"Instagram「{shape.KEYWORD}」はデータを取得できませんでした"
        "（取得できた媒体だけで分析しています）"
    )
    notes = [e["text"] for e in msg.blocks[-3]["elements"]]
    assert notes == [f":hourglass_flowing_sand: {followup_notice_line(5)}", missing]
    last = [e["text"] for e in msg.blocks[-1]["elements"]]
    assert last == [
        "注意: 一部の面は AI の読みを作れず、集計だけの見出しにしています",
        "概算 $0.0186",
    ]
    for note in (*notes, *last):
        assert note in msg.text


def test_a_overall_numbers_with_stage_words_and_links_to_the_right_posts() -> None:
    msg = surface_message(shape.a_output(), shape.a_input())
    assert msg is not None
    overall = _section_with(msg.blocks, "上位30本全体")
    assert overall.splitlines()[1:] == [
        "• フォロワー1万人未満の投稿者: 上位10本中 3本（少数派）",
        f"　例: <{_url(2)}|2位> @spice_koki 2,930人・<{_url(7)}|7位> @sample_small7 5,100人",
        "• 再生÷フォロワーの中央値: 2.78倍",
        # 保存率の上位は集計の値（LLM の丸めた「1.6～2.9%」ではなく）を、その順位の投稿へのリンクで
        f"• 保存率が高い投稿: <{_url(27)}|27位> 2.88%・<{_url(11)}|11位> 2.38%"
        f"・<{_url(21)}|21位> 1.62%",
        f"• 本文かタグに「{shape.KEYWORD}」の語をすべて含む: 7/30本（少数派）",
        "• 直近90日の投稿: 5/30本（少数派）・投稿時期の中央値 11か月前",
        "• 尺の中央値: 1分00秒",
        "• よく付くタグ: #スパイスカレー 19本・#カレー 9本・#スパイス 6本",
        "• PR表記のある投稿（ブランドは問わない）: 1・6・12・14・16位",
    ]


def test_a_no_holders_is_said_in_words() -> None:
    out = shape.a_output()
    assert out.surfaces[0].facts is not None
    out.surfaces[0].facts.holders = []
    msg = surface_message(out, shape.a_input())
    assert msg is not None
    assert ":dart: *常連*\n• なし（30本すべて別のアカウント）" in mrkdwn_texts(msg.blocks)


def test_a_labels_llm_text_and_drops_assertive_labels() -> None:
    msg = surface_message(shape.a_output(), shape.a_input())
    assert msg is not None
    whole = body(msg.blocks)
    assert ":mag: *結論（AI の要約）*" in whole
    angles = _section_with(msg.blocks, "上位に見られる切り口")
    assert angles.splitlines() == [
        ":bulb: *上位に見られる切り口* （AI の分類・該当する順位）",
        "• 基本スパイス4種類の選び方・黄金比（2・11位）",
        "• 初心者向け・簡単・失敗しない（10・17・19位）",
        "• 無水カレー・時短調理（6・13・14位）",
        "• 玉ねぎの炒め方・下ごしらえのコツ（8・9・27位）",
    ]
    for word in ("勝ち筋", "打ち手", "最有力", "空白:"):
        assert word not in whole
    # winning・gap の全文はレポートへ（Slack に二重に書かない）
    assert "入れ替わりが遅い" not in whole and "2,930フォロワーながら" not in whole


def test_a_ai_reading_numbers_are_aligned_to_the_facts_and_evidence_links_are_exact() -> None:
    """AI の文の「1.6～2.9%」（投稿一覧に 1 桁で渡した保存率）は、集計の値にそろえる。"""
    msg = surface_message(shape.a_output(), shape.a_input())
    assert msg is not None
    reading = _section_with(msg.blocks, "AI の読み（次の一手の候補）").splitlines()
    assert reading[0] == (
        ":speech_balloon: *AI の読み（次の一手の候補）* （数字は集計の値にそろえています）"
    )
    assert reading[1].startswith("• 保存率が高い投稿（1.62～2.88%）の共通点は調理工程の詳細化。")
    assert reading[1].endswith(f"（根拠: <{_url(27)}|27位>・<{_url(11)}|11位>・<{_url(21)}|21位>）")
    assert "1.6～2.9%" not in msg.text


def test_a_ai_reading_is_left_as_is_when_the_rounded_value_is_ambiguous() -> None:
    out = shape.a_output()
    c = out.surfaces[0].conclusion
    assert c is not None
    # 0.6% に丸まる投稿は何本もある → どれか決められないので変えない。整数の % も変えない。
    c.actions = [ConclusionPoint(text="保存率0.6%前後が多く、再生の74%は上位2区分", ranks=[])]
    msg = surface_message(out, shape.a_input())
    assert msg is not None
    reading = _section_with(msg.blocks, "AI の読み").splitlines()
    assert reading == [
        ":speech_balloon: *AI の読み（次の一手の候補）*",
        "• 保存率0.6%前後が多く、再生の74%は上位2区分",
    ]


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


def test_a_text_has_everything_in_the_blocks_for_screen_readers() -> None:
    """Slack はスクリーンリーダーに最上位の text だけを読ませる → blocks の全文を text に入れる。"""
    out = shape.a_output()
    msg = surface_message(out, shape.a_input())
    assert msg is not None
    assert len(msg.text) <= MAX_FALLBACK_TEXT
    first = msg.text.splitlines()[0]
    assert first == (
        f"検索上位チェック「{shape.KEYWORD}」TikTok 上位30本: "
        "クリエイターとインフルエンサーが再生の74%を占める。"
        "小規模アカウントも3本入賞し、新規参入の余地あり。"
    )
    assert msg.text.count("クリエイターとインフルエンサーが再生の74%") == 1  # 結論は繰り返さない
    _assert_text_has_every_block(msg, skip=1)
    for needle in (
        "• @kurashiru.com（メディア・41.2万人） 4枠: 8・9・12・26位",
        "• 10万〜100万人 9/30本・再生の46%",
        "7/30本（少数派）",
        followup_notice_line(5),
        f"<{shape.A_REPORT}|レポートを開く>",
        "照合したのは本文・タグにある「GABAN」の表記だけです",
    ):
        assert needle in msg.text, needle
    for post in sorted(out.surfaces[0].posts, key=lambda p: p.rank)[:5]:
        assert f"<{post.url}|" in msg.text  # 会話の履歴から「2位の動画」を辿れる
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


def _compact(n_keywords: int, *, headline: str, client: bool = True) -> tuple[object, object]:
    base = shape.a_output()
    surfaces: list[KwSurface] = []
    for i in range(n_keywords):
        for platform in ("tiktok", "instagram"):
            s = base.surfaces[0].model_copy(deep=True)
            s.keyword = f"キーワード{i}"
            s.platform = platform  # type: ignore[assignment]
            assert s.conclusion is not None
            s.conclusion.headline = f"{headline}{i}{platform}"
            surfaces.append(s)
    base.surfaces = surfaces
    base.keywords = list(dict.fromkeys(s.keyword for s in surfaces))
    inp = SearchSurfaceCheckInput(
        keywords=base.keywords,
        platforms=["tiktok", "instagram"],
        client_name=shape.CLIENT if client else None,
    )
    return base, inp


def test_a_compact_shows_client_lines_per_surface_and_the_scope_once() -> None:
    out, inp = _compact(1, headline="結論")
    msg = surface_message(out, inp)  # type: ignore[arg-type]
    assert msg is not None
    sections = [t for t in mrkdwn_texts(msg.blocks) if t.startswith("*「キーワード0」")]
    assert len(sections) == 2
    for section_text in sections:
        assert "• 上位30本に「GABAN」に触れた投稿は無し" in section_text.splitlines()
    scope = (
        "照合したのは本文・タグにある「GABAN」の表記だけです"
        "（ほかの表記・公式アカウントは照合していません）"
    )
    assert mrkdwn_texts(msg.blocks).count(scope) == 1


def test_a_compact_text_has_every_surface_conclusion() -> None:
    """5 語×2 媒体（本番の長さの結論）でも、text にすべての面の結論が入る（4,000 字以内）。"""
    out, inp = _compact(5, headline="上位はクリエイター中心で小規模も入賞")
    msg = surface_message(out, inp)  # type: ignore[arg-type]
    assert msg is not None
    assert len(msg.text) <= MAX_FALLBACK_TEXT
    for s in out.surfaces:  # type: ignore[attr-defined]
        assert s.conclusion.headline in msg.text, s.conclusion.headline
    assert f"<{shape.A_REPORT}|レポートを開く>" in msg.text
    assert_limits(msg.blocks)


def test_a_compact_many_surfaces_stay_within_limits() -> None:
    """5 語×2 媒体・長い結論でも 50 blocks・3000 字・header 150 字・合計 12,000 字に収まり、
    レポートは残る。text も 4,000 字以内で、どの面も見出しの行は残る。"""
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
    assert text_size(msg.blocks) <= MAX_TOTAL_TEXT  # msg_blocks_too_long の手前
    assert_well_formed(msg.blocks, {p.url for p in surfaces[0].posts} | {shape.A_REPORT})
    assert len(header_texts(msg.blocks)[0]) <= 150
    assert f"<{shape.A_REPORT}|レポートを開く>" in body(msg.blocks)
    assert len(msg.text) <= MAX_FALLBACK_TEXT
    assert f"<{shape.A_REPORT}|レポートを開く>" in msg.text
    for s in surfaces:
        assert f"*「{s.keyword}」{'TikTok' if s.platform == 'tiktok' else 'Instagram'}" in msg.text


def test_a_single_surface_with_huge_llm_lists_is_clipped() -> None:
    out = shape.a_output()
    c = out.surfaces[0].conclusion
    assert c is not None
    c.angles = [ConclusionPoint(text="切り口" * 40, ranks=[1, 2]) for _ in range(200)]
    c.actions = [ConclusionPoint(text="読み" * 400, ranks=[27]) for _ in range(20)]
    msg = surface_message(out, shape.a_input())
    assert msg is not None
    assert_limits(msg.blocks)
    assert text_size(msg.blocks) <= MAX_TOTAL_TEXT
    assert any(t.endswith("…") for t in mrkdwn_texts(msg.blocks))
    assert len(msg.text) <= MAX_FALLBACK_TEXT
    assert f"<{shape.A_REPORT}|レポートを開く>" in msg.text


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
    # 1 段目の集計の時刻（追記を出した時刻ではない）。「実測」とは書かない（取り方は 1 段目の注記）
    assert msg.blocks[1]["elements"][0]["text"] == (
        "TikTok・2026-09-28 15:30 の検索上位チェックの続き・動画を見て分析 4本"
        "・5位はサムネだけの分析のため集計外"
    )


def test_b_partial_run_says_what_was_not_analyzed() -> None:
    """今月の残りで一部だけ・分析できなかった順位は、文言どおりに残す。"""
    msg = followup_message(shape.b_partial_output())
    assert msg is not None
    assert msg.blocks[1]["elements"][0]["text"] == (
        "TikTok・2026-09-28 15:30 の検索上位チェックの続き・動画を見て分析 3本・4位は分析できず"
    )
    notes = [e["text"] for e in msg.blocks[-1]["elements"]]
    assert notes == [
        "今月の動画分析の残りの都合で、5本のうち4本だけ分析しました（リセットは来月1日・JST）",
        FOLLOWUP_CAVEAT,
        "概算 $0.3596",
    ]
    lines = _section_with(msg.blocks, "*1本ずつ*").splitlines()
    assert lines[-1].endswith(f"@itamae_shinya>　{FAILED_ROW}")
    for note in notes:
        assert note in msg.text


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
    assert lines[3] == (
        "*3位* <https://www.tiktok.com/@musuicurry/video/7400000000000000103|@musuicurry>"
        "　フック: POV／発話の検索KW あり／ナレーション あり／1分15秒・19カット"
    )
    assert lines[5].endswith(COVER_ONLY_ROW)
    joined = "\n".join(lines)
    assert "冒頭テロップ" not in joined and "テンポ" not in joined  # 全員に同じ項目は省く
    assert "○" not in joined and "×" not in joined


def test_b_save_block_has_the_counted_common_points_and_the_ai_reading() -> None:
    """保存率の高い 2 本: 集計で決まる共通点と AI の読みの両方（リンクはその順位の投稿へ）。"""
    out = shape.b_output()
    msg = followup_message(out)
    assert msg is not None
    urls = {r.rank: r.url for r in out.videos}
    save = _section_with(msg.blocks, "保存率の高い2本").splitlines()
    assert save[0] == f":floppy_disk: *保存率の高い2本* （<{urls[2]}|2位>・<{urls[3]}|3位>）"
    assert save[1] == (
        "共通点（集計）: 冒頭にテロップ・テロップに KW・発話に KW・テンポがふつう・ナレーションあり"
    )
    assert save[2].startswith("AI の読み: 保存率の高い2位と3位は")
    assert "保存につながっている" not in body(msg.blocks)  # winning（因果の言い切り）は出さない


def test_b_text_has_everything_in_the_blocks_and_notes() -> None:
    out = shape.b_output()
    msg = followup_message(out)
    assert msg is not None
    assert msg.text.splitlines()[0] == (
        f"上位5本の動画の中身「{shape.KEYWORD}」TikTok: "
        "スパイス選びと分量を明確にした、初心者向けの実践的なレシピ動画が上位を占める"
    )
    _assert_text_has_every_block(msg, skip=1)
    assert f"<{shape.B_REPORT}|レポートを開く>" in msg.text
    assert all(f"<{r.url}|" in msg.text for r in out.videos)
    assert "5位はサムネだけの分析のため集計外" in msg.text
    last = msg.blocks[-1]["elements"]
    assert [e["text"] for e in last] == [FOLLOWUP_CAVEAT, "概算 $0.3596"]
    assert FOLLOWUP_CAVEAT in msg.text
    assert_only_own_links([msg.text], _b_links())


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
    assert text_size(msg.blocks) <= MAX_TOTAL_TEXT
    assert len(msg.text) <= MAX_FALLBACK_TEXT


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
