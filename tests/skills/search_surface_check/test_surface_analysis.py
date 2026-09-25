"""検索上位チェックの分析（集計・結論の照合・Slack 文面・DADS レポート）のテスト。

2026-09-25 に小俣さんが「見づらい・分析が甘い」と指摘した実物（スパイスカレー 作り方）を
再現したデータで、指摘された 3 点（分類の誤り・捨てていた指標・読めない出力）を固定する。
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search_surface_check.conclusion import (
    _numbers,
    build_prompt,
    conclude,
    ground_conclusion,
    tone_down,
)
from teamagent.skills.search_surface_check.display import fmt_age, fmt_count
from teamagent.skills.search_surface_check.insights import (
    compute_facts,
    contains_keyword,
    is_pr_post,
    mentions,
)
from teamagent.skills.search_surface_check.report import render_surface_report
from teamagent.skills.search_surface_check.schema import SearchSurfaceCheckInput, SurfacePost
from teamagent.skills.search_surface_check.skill import SearchSurfaceCheckSkill, _category
from teamagent.skills.search_surface_check.summary import slack_safe
from tests.skills.search_surface_check.fixtures import (
    CLASSIFY_BY_ACCOUNT,
    GROUNDED_CONCLUSION,
    KEYWORD,
    NOW,
    FakeBedrock,
    s3_rows,
)

_JOB_ID = "tk_0123456789ab"


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="req-test", user_id="U1", metadata={"user_email": "a@vectorinc.co.jp"}
    )


class _Source:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def posts(self, n: int | None = None) -> list[dict[str, Any]]:
        return self._rows[:n] if n else self._rows


class _EmptyApify:
    """IG が 0 件で返る（2026-09-25 の本番と同じ。エラーにはならない）。"""

    def ig_search(self, keyword: str, **kw: Any) -> tuple[list[Any], float]:
        return [], 0.0


def _skill(bedrock: FakeBedrock, rows: list[dict[str, Any]] | None = None) -> Any:
    published: dict[str, str] = {}

    def publisher(path: str, *, request_id: str, query: str) -> str:
        with open(path, encoding="utf-8") as f:
            published["html"] = f.read()
        return "https://s3.example/surface"

    skill = SearchSurfaceCheckSkill(
        apify=_EmptyApify(),  # type: ignore[arg-type]
        bedrock=bedrock,
        publisher=publisher,
        tiktok_source_factory=lambda job_id, audit_hash: _Source(
            s3_rows() if rows is None else rows
        ),
        clock=lambda: NOW,
    )
    return skill, published


def _run(bedrock: FakeBedrock, **input_kw: Any) -> tuple[Any, dict[str, str]]:
    skill, published = _skill(bedrock)
    out = skill.run(
        SearchSurfaceCheckInput(
            keywords=[KEYWORD], acquire_job_id=_JOB_ID, client_name="GABAN", **input_kw
        ),
        _ctx(),
    )
    return out, published


def _posts() -> list[SurfacePost]:
    out, _ = _run(FakeBedrock())
    return next(s for s in out.surfaces if s.platform == "tiktok").posts


# ---------------------------------------------------------------------------
# 取得: 捨てていた指標を拾う
# ---------------------------------------------------------------------------


def test_s3_rows_keep_saves_dates_names_tags_and_duration() -> None:
    first = _posts()[0]
    assert first.author == "gonosara" and first.author_name == "ごのさら"
    assert first.save_count == 11_200 and first.share_count == 351_000 // 300
    assert first.posted_at == NOW - 120 * 86_400
    assert first.duration_sec == 58
    assert first.hashtags == ["スパイスカレー", "料理", "簡単レシピ"]
    # 本文は 80 字で切らない（分類と切り口の読み取りに使う）
    long_desc = "あ" * 200
    rows = s3_rows()
    rows[0]["title"] = long_desc
    skill, _ = _skill(FakeBedrock(), rows)
    got = skill._tiktok_from_s3(_JOB_ID, "h", [KEYWORD], 30)[KEYWORD][0]
    assert got.desc == long_desc


def test_s3_rows_with_broken_numbers_do_not_crash() -> None:
    rows = s3_rows()
    rows[0].update(saves=None, create_time="x", duration=-5, hashtags="notalist", followers=True)
    skill, _ = _skill(FakeBedrock(), rows)
    got = skill._tiktok_from_s3(_JOB_ID, "h", [KEYWORD], 30)[KEYWORD][0]
    assert (got.save_count, got.posted_at, got.duration_sec, got.hashtags) == (0, 0, 0, [])
    assert got.author_followers == 0


# ---------------------------------------------------------------------------
# 分類: 実例の誤りを直す
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "followers", "expected"),
    [
        ("ugc", 150_000, "creator"),  # 15 万人の料理家が「一般」になっていた実例
        ("ugc", 28_000, "creator"),  # 改訂後の実機でも 2.8 万人の専門アカウントが ugc に
        ("ugc", 10_000, "creator"),
        ("ugc", 9_999, "ugc"),
        ("ugc", 0, "ugc"),  # フォロワー不明（IG）は直さない
        ("gourmet", 0, "creator"),  # 旧語彙
        ("media", 5_200_000, "media"),
        ("brand_official", 10, "brand_official"),
        ("bogus", 10, "unknown"),
        (None, 10, "unknown"),
    ],
)
def test_category_guard(raw: Any, followers: int, expected: str) -> None:
    assert _category(raw, followers) == expected


def test_classifier_output_is_normalized_in_the_run() -> None:
    by_author = {p.author: p.category for p in _posts()}
    assert by_author["katokenoshokutaku"] == "creator"  # ugc と答えたが 15 万人
    assert by_author["musuicurry"] == "creator"  # gourmet と答えた
    assert by_author["kurashiru.com"] == "media"  # 旧版は「公式」にしていた
    assert set(by_author.values()) <= {"creator", "influencer", "ugc", "media"}


def test_classify_prompt_gets_name_followers_and_tags() -> None:
    bedrock = FakeBedrock()
    _run(bedrock)
    classify = next(p for p in bedrock.prompts if "# 投稿一覧" in p and "検索面の読み" not in p)
    assert '"name": "クラシル"' in classify
    assert '"followers": 5200000' in classify
    assert '"tags": ["クラシル", "料理"]' in classify
    assert "迷ったら ugc" not in classify  # 旧版の「迷ったら一般」を消した


# ---------------------------------------------------------------------------
# 集計（決定的）
# ---------------------------------------------------------------------------


def test_facts_on_the_real_surface() -> None:
    facts = compute_facts(_posts(), keyword=KEYWORD, client_name="GABAN", now_epoch=NOW)
    assert facts.n == 15 and facts.unique_authors == 13
    top = facts.categories[0]
    assert (top.category, top.count, round(top.play_share * 100)) == ("creator", 7, 68)
    # 同数（2 本）のインフルエンサーとメディアは再生の多い方を先に
    assert [c.category for c in facts.categories] == ["creator", "ugc", "influencer", "media"]
    assert [(h.author, h.ranks) for h in facts.holders] == [
        ("spice_koki", [2, 9]),
        ("kurashiru.com", [8, 11]),
    ]
    assert [(t.tier, t.count) for t in facts.tiers] == [
        ("100万人以上", 2),
        ("10万〜100万人", 5),
        ("1万〜10万人", 4),
        ("1万人未満", 4),
    ]
    assert facts.small_in_top10 == 1 and facts.top10_n == 10
    assert facts.most_played_rank == 6
    assert facts.median_save_rate_pct == 3.49
    assert [s.rank for s in facts.save_leaders] == [2, 9, 13]
    assert facts.median_age_days == 150 and facts.recent_90d == 6
    assert facts.median_duration_sec == 58
    assert facts.kw_in_text == 4  # 7・11・13・15 位（11 位はタグで「スパイスカレー」）
    assert facts.top_tags[0].tag == "スパイスカレー" and facts.top_tags[0].count == 10
    assert facts.pr_ranks == [6]
    assert facts.mention_ranks == []


def test_rank_play_agreement_sign() -> None:
    def post(rank: int, plays: int) -> SurfacePost:
        return SurfacePost(platform="tiktok", keyword="k", rank=rank, play_count=plays)

    ordered = [post(i, 1000 - i) for i in range(1, 11)]
    reversed_ = [post(i, 100 + i) for i in range(1, 11)]
    assert compute_facts(ordered, keyword="k", client_name=None, now_epoch=NOW).rank_play_rho == 1.0
    assert (
        compute_facts(reversed_, keyword="k", client_name=None, now_epoch=NOW).rank_play_rho == -1.0
    )
    few = ordered[:7]  # 8 本未満は出さない
    assert compute_facts(few, keyword="k", client_name=None, now_epoch=NOW).rank_play_rho is None


@pytest.mark.parametrize(
    ("desc", "tags", "expected"),
    [
        ("新作 #PR", [], True),
        ("新作 ＃ＰＲ", [], True),  # 全角
        ("新作", ["pr"], True),
        ("新作 #提供", [], True),
        ("新作 #brand_pr", [], False),  # 部分一致は数えない
        ("PRします", [], False),  # タグでない
    ],
)
def test_pr_detection(desc: str, tags: list[str], expected: bool) -> None:
    post = SurfacePost(platform="tiktok", keyword="k", rank=1, desc=desc, hashtags=tags)
    assert is_pr_post(post) is expected


def test_mentions_and_keyword_match() -> None:
    post = SurfacePost(
        platform="tiktok", keyword="k", rank=1, desc="ＧＡＢＡＮのスパイスで", hashtags=["カレー"]
    )
    assert mentions(post, "gaban")  # 全角・大小を寄せる
    assert not mentions(post, "G")  # 1 文字は見ない
    assert contains_keyword(post, "スパイス カレー")  # 本文とタグにまたがってよい
    assert not contains_keyword(post, "スパイス 作り方")


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (8_200, "8,200"),
        (150_000, "15万"),  # 「15.0万」にしない
        (123_456, "12.3万"),
        (1_234_567, "123万"),  # 100 万以上は小数を付けない
        (5_200_000, "520万"),
        (123_000_000, "1.2億"),
    ],
)
def test_count_format(n: int, expected: str) -> None:
    assert fmt_count(n) == expected


def test_fixed_clock_ages() -> None:
    assert fmt_age(0) == "今日"
    assert fmt_age(45) == "45日前"
    assert fmt_age(100) == "3か月前"
    assert fmt_age(400) == "1年1か月前"


# ---------------------------------------------------------------------------
# 結論（LLM）: 入力に無い数字を通さない
# ---------------------------------------------------------------------------


def _allowed() -> tuple[set[str], set[int], Any, list[SurfacePost]]:
    posts = _posts()
    facts = compute_facts(posts, keyword=KEYWORD, client_name="GABAN", now_epoch=NOW)
    _, allowed = build_prompt(
        "{keyword}{platform}{client_name}{facts_json}{posts_json}",
        keyword=KEYWORD,
        platform="tiktok",
        client_name="GABAN",
        facts=facts,
        posts=posts,
        now_epoch=NOW,
    )
    return allowed, {p.rank for p in posts}, facts, posts


def test_grounded_conclusion_is_kept() -> None:
    allowed, ranks, _, _ = _allowed()
    c = ground_conclusion(GROUNDED_CONCLUSION, allowed_numbers=allowed, valid_ranks=ranks)
    assert c is not None
    assert c.headline == GROUNDED_CONCLUSION["headline"]
    assert c.winning is not None and c.winning.ranks == [2, 9]
    assert c.gap is not None and len(c.actions) == 1
    # 実在しない順位だけの切り口は捨てる（2 本以上に出ていないと「共通」ではない）
    assert [a.text for a in c.angles] == ["スパイスを4つに絞る"]


def test_fabricated_numbers_are_dropped() -> None:
    allowed, ranks, _, _ = _allowed()
    assert "83" not in allowed and "37" not in allowed
    raw = dict(GROUNDED_CONCLUSION)
    raw["winning"] = {"text": "クリエイターが再生の83%を占める", "ranks": [1]}
    raw["actions"] = [
        {"text": "37本の投稿で検証する", "ranks": []},
        GROUNDED_CONCLUSION["actions"][0],
    ]
    dropped: list[tuple[str, str]] = []
    c = ground_conclusion(
        raw, allowed_numbers=allowed, valid_ranks=ranks, on_drop=lambda f, r: dropped.append((f, r))
    )
    assert c is not None
    assert c.winning is None
    assert [a.text for a in c.actions] == [GROUNDED_CONCLUSION["actions"][0]["text"]]
    assert ("winning", "number:83") in dropped and ("actions", "number:37") in dropped


def test_fabricated_headline_is_replaced_by_the_rule_headline() -> None:
    _, _, facts, posts = _allowed()
    raw = dict(GROUNDED_CONCLUSION, headline="クリエイターが再生の83%を取る面")
    c, cost = conclude(
        lambda prompt: (json.dumps(raw, ensure_ascii=False), 0.01),
        "{keyword}{platform}{client_name}{facts_json}{posts_json}",
        keyword=KEYWORD,
        platform="tiktok",
        client_name="GABAN",
        facts=facts,
        posts=posts,
        now_epoch=NOW,
    )
    assert c is not None and cost == 0.01
    assert c.headline == "上位15本の最多はクリエイターの7本（再生の68%）。常連は@spice_koki（2枠）"
    assert c.winning is not None  # 見出し以外の正しい項目は残す


@pytest.mark.parametrize("reply", ["すみません、分析できません", "{not json", "[]"])
def test_unparseable_reply_falls_back_to_rule(reply: str) -> None:
    _, _, facts, posts = _allowed()
    c, _ = conclude(
        lambda prompt: (reply, 0.0),
        "{keyword}{platform}{client_name}{facts_json}{posts_json}",
        keyword=KEYWORD,
        platform="tiktok",
        client_name=None,
        facts=facts,
        posts=posts,
        now_epoch=NOW,
    )
    assert c is not None and c.generated_by == "rule"


def test_hype_words_are_toned_down() -> None:
    # 実機の Haiku が「誇張語を使わない」の指示のあとでも書いた文
    assert tone_down("10万人前後のアカウントが検索面を支配。") == (
        "10万人前後のアカウントが検索面の中心。"
    )
    assert tone_down("クリエイターが再生を独占") == "クリエイターが再生の多くを占める"
    allowed, ranks, _, _ = _allowed()
    raw = dict(GROUNDED_CONCLUSION, headline="クリエイターが検索面を支配する面")
    c = ground_conclusion(raw, allowed_numbers=allowed, valid_ranks=ranks)
    assert c is not None and "支配" not in c.headline


def test_non_list_fields_do_not_crash() -> None:
    allowed, ranks, _, _ = _allowed()
    raw = {"headline": "料理系クリエイターの面", "actions": {"text": "x"}, "angles": "x"}
    c = ground_conclusion(raw, allowed_numbers=allowed, valid_ranks=ranks)
    assert c is not None and c.actions == [] and c.angles == []


def test_numbers_normalize_fullwidth_and_commas() -> None:
    assert _numbers("再生の６８％・1,234本・2.50倍") == {"68", "1234", "2.50", "2.5"}


def test_analyze_prompt_carries_facts_posts_and_rules() -> None:
    bedrock = FakeBedrock()
    _run(bedrock)
    analyze = next(p for p in bedrock.prompts if "検索面の読み" in p)
    assert "「スパイスカレー 作り方」のTikTok検索面" in analyze
    assert "クライアント「GABAN」" in analyze
    assert '"再生の割合%": 68' in analyze
    assert '"順位": [2, 9]' in analyze  # 常連
    assert '"rank": 15' in analyze and '"days_ago": 75' in analyze
    assert "数字は「集計」と「投稿一覧」に書かれている値だけを使う" in analyze


# ---------------------------------------------------------------------------
# 実行全体: Slack 文面とレポート
# ---------------------------------------------------------------------------


def test_slack_summary_is_readable_bullets_not_tables() -> None:
    out, _ = _run(FakeBedrock())
    text = out.slack_summary
    assert text.startswith(
        "**検索上位チェック**「スパイスカレー 作り方」TikTok 上位15本・2026-09-25 実測"
    )
    assert "**結論** 料理系クリエイターが上位15本中7本を持ち、再生の68%を取る面" in text
    assert "- 勝ち筋: クリエイター7本で再生の68%" in text and "（2・9位）" in text
    assert "- 空白: 公式は0本" in text
    assert "- 打ち手: フォロワー1万〜10万人の料理クリエイター" in text
    assert "- 上位に共通する切り口: 「スパイスを4つに絞る」1・2・14位" in text
    assert "- 投稿者: クリエイター 7本（再生の68%）／一般 4本（再生の5%）" in text
    assert (
        "- 常連: スパイスこうき（@spice_koki） 2枠（2・9位）／クラシル（@kurashiru.com） 2枠"
        in text
    )
    assert "保存率の中央値 3.49%（最高は2位 @spice_koki 6.82%）" in text
    assert "直近90日の投稿 6本" in text
    assert "- PR表記あり: 6位" in text
    assert "「GABAN」に触れた投稿は無し" in text
    assert "**上位10本**（再生・保存率・投稿時期）" in text
    post_lines = [ln for ln in text.splitlines() if re.match(r"^- \d+位 @", ln)]
    assert len(post_lines) == 10
    assert post_lines[0].startswith(
        "- 1位 @gonosara（クリエイター・12.3万人） 35.1万回・保存3.2%・4か月前"
    )
    assert "［PR表記］" in post_lines[5]
    assert "@kurashiru.com（メディア・520万人）" in post_lines[7]  # 100 万以上は小数を付けない
    # 表・コードブロックを使わない
    assert "```" not in text and "|" not in text
    # IG は取れなかったことを 1 行で（空の列は出さない）
    assert "Instagram「スパイスカレー 作り方」はデータを取得できませんでした" in text
    assert "レポート（全15本の一覧つき・7日有効）: https://s3.example/surface" in text


def test_client_accounts_in_surface() -> None:
    out, _ = _run(FakeBedrock(), client_accounts=["@spice_koki"])
    assert "- クライアント: 2・9位に在圏" in out.slack_summary
    assert out.surfaces[0].client_ranks == [2, 9]


def test_third_party_text_cannot_inject_slack_markup() -> None:
    rows = s3_rows()
    # 題名は 26 字で切るので、リンク記法は先頭に置く
    rows[0]["title"] = "[偽](https://evil.example) <!channel> *太字* `code`"
    rows[0]["account_name"] = "<@U123>"
    skill, _ = _skill(FakeBedrock(), rows)
    out = skill.run(SearchSurfaceCheckInput(keywords=[KEYWORD], acquire_job_id=_JOB_ID), _ctx())
    text = out.slack_summary
    assert "<!channel>" not in text and "<@U123>" not in text
    assert "*太字*" not in text and "`code`" not in text
    assert "［偽］(https" in text  # リンク記法 [..](..) を崩して効かなくする
    assert slack_safe("<!here> a*b* [x](y)") == "＜!here＞ a＊b＊ ［x］(y)"


def test_analyze_failure_degrades_to_rule_headline_with_warning() -> None:
    out, _ = _run(FakeBedrock(analyze_error=RuntimeError("ThrottlingException")))
    c = out.surfaces[0].conclusion
    assert c is not None and c.generated_by == "rule"
    assert "**結論** 上位15本の最多はクリエイターの7本（再生の68%）" in out.slack_summary
    assert any("集計だけの見出し" in w for w in out.warnings)


def test_report_uses_dads_and_hides_empty_instagram() -> None:
    _, published = _run(FakeBedrock(), client_accounts=["@spice_koki"])
    page = published["html"]
    # DADS のトークンと出典
    assert "--color-primitive-blue-900:#0017c1" in page
    assert "'Noto Sans JP'" in page and "font-size:16px;line-height:1.7" in page
    assert "Copyright (c) 2023 デジタル庁" in page
    # 空の IG 列は出さず、取れなかったことを注意書きで
    assert "データなし" not in page
    assert "Instagram「スパイスカレー 作り方」はデータを取得できませんでした。" in page
    # 読む順: 結論 → 数字 → 構成 → 常連 → 一覧
    order = [
        page.index("<p class='label'>結論</p>"),
        page.index("class='kpis'"),
        page.index("<h3>投稿者の構成</h3>"),
        page.index("<h3>常連（複数の枠を持つアカウント）</h3>"),
        page.index("<h3>上位の一覧（全15本"),
    ]
    assert order == sorted(order)
    assert page.count("<tr class='is-client'>") == 2
    assert "文中の数字は集計と照合済み" in page
    # 全件の一覧（15 本）と投稿日・尺・保存率
    assert len(re.findall(r"<td class='num'>\d{4}-\d{2}-\d{2}</td>", page)) == 15
    assert ">6.8%<" in page and ">58秒<" in page


def test_report_escapes_third_party_text() -> None:
    rows = s3_rows()
    rows[0]["title"] = "<script>alert(1)</script>"
    rows[0]["account_name"] = "<img src=x onerror=alert(1)>"
    skill, published = _skill(FakeBedrock(), rows)
    skill.run(SearchSurfaceCheckInput(keywords=[KEYWORD], acquire_job_id=_JOB_ID), _ctx())
    page = published["html"]
    assert "<script>" not in page and "<img" not in page
    assert "&lt;script&gt;" in page


def test_report_multi_keyword_overview_links_sections() -> None:
    posts = _posts()
    facts = compute_facts(posts, keyword=KEYWORD, client_name=None, now_epoch=NOW)
    from teamagent.skills.search_surface_check.schema import KwSurface, SurfaceConclusion

    s1 = KwSurface(
        keyword=KEYWORD,
        platform="tiktok",
        posts=posts,
        facts=facts,
        conclusion=SurfaceConclusion(headline="面A"),
    )
    s2 = s1.model_copy(
        update={"keyword": "カレー 隠し味", "conclusion": SurfaceConclusion(headline="面B")}
    )
    page = render_surface_report(
        keywords=[KEYWORD, "カレー 隠し味"], surfaces=[s1, s2], client_name=None, measured_epoch=NOW
    )
    assert "<h2>KW 別の結論</h2>" in page
    assert "href='#surface-1'" in page and "href='#surface-2'" in page
    assert "id='surface-2'" in page and "面B" in page


def test_every_classifier_account_in_fixture_is_on_the_surface() -> None:
    """代役の分類表がデータとずれていない（ずれると既定の ugc に落ちて試験が甘くなる）。"""
    assert {r["account_id"] for r in s3_rows()} == set(CLASSIFY_BY_ACCOUNT)
