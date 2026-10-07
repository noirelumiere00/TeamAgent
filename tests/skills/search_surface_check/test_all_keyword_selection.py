"""全 KW から動画を選ぶ・取得の前の確認文・競合の順位（10-05 小俣さん裁定）。

再現する本番の失敗: 「HIS」「JTB」など複数 KW の検索上位チェックで、動画の中身を見るのが
1 語目の TikTok 面の上位 5 本だけだった（2 語目以降の個別分析が無い）。
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.skills.search_surface_check import confirm
from teamagent.skills.search_surface_check.schema import (
    KwSurface,
    SearchSurfaceCheckInput,
    SearchSurfaceCheckOutput,
    SurfacePost,
)
from teamagent.skills.search_surface_check.skill import SearchSurfaceCheckSkill
from teamagent.skills.search_surface_check.summary import competitor_parts
from teamagent.skills.search_surface_check.video_digest import (
    followup_label,
    select_followup_videos,
)


def _post(kw: str, rank: int, vid: int, *, author: str = "a", duration: int = 30) -> SurfacePost:
    return SurfacePost(
        platform="tiktok",
        keyword=kw,
        rank=rank,
        url=f"https://www.tiktok.com/@{author}/video/{vid}",
        author=author,
        duration_sec=duration,
    )


def _out(*surfaces: tuple[str, list[SurfacePost]]) -> SearchSurfaceCheckOutput:
    return SearchSurfaceCheckOutput(
        keywords=[k for k, _ in surfaces],
        surfaces=[KwSurface(keyword=k, platform="tiktok", posts=p) for k, p in surfaces],
    )


def test_picks_from_every_keyword_with_multi_keyword_videos_first() -> None:
    out = _out(
        ("HIS", [_post("HIS", r, 100 + r) for r in range(1, 8)]),
        # 2 語目の 3 位は 1 語目の 6 位と同じ動画（複数語に出る＝先に選ぶ）
        ("JTB", [_post("JTB", 1, 201), _post("JTB", 2, 202), _post("JTB", 3, 106)]),
    )
    picked = select_followup_videos(out, 5)
    # 赤（1 語目だけを見る旧実装）なら JTB の動画は 1 本も選ばれない
    assert any("JTB" in " ".join(p.kw_ranks) and "HIS" not in " ".join(p.kw_ranks) for p in picked)
    first = picked[0]
    assert first.url.endswith("/video/106")
    assert first.kw_ranks == ["HIS 6位", "JTB 3位"]
    # 残りは表示順位の良い順（同順位は KW の順）・総合順位を 1..N に振り直す
    assert [p.url.rsplit("/", 1)[1] for p in picked[1:]] == ["101", "201", "102", "202"]
    assert [p.rank for p in picked] == [1, 2, 3, 4, 5]
    assert followup_label(out, picked) == "HIS・JTB"


def test_ten_videos_and_unanalyzable_posts_are_skipped() -> None:
    out = _out(
        ("A", [_post("A", r, 300 + r, duration=0 if r == 1 else 20) for r in range(1, 8)]),
        ("B", [_post("B", r, 400 + r) for r in range(1, 8)]),
    )
    picked = select_followup_videos(out, 10)
    assert len(picked) == 10
    assert all(not p.url.endswith("/video/301") for p in picked)  # カルーセル（尺 0）は除く


def test_single_keyword_keeps_display_ranks() -> None:
    out = _out(("A", [_post("A", r, 500 + r, duration=0 if r == 2 else 20) for r in range(1, 9)]))
    picked = select_followup_videos(out, 5)
    assert [p.rank for p in picked] == [1, 3, 4, 5, 6]
    assert all(not p.kw_ranks for p in picked)
    assert followup_label(out, picked) == "A"


def test_chapter_goes_after_the_last_tiktok_surface_when_videos_span_keywords() -> None:
    out = _out(
        ("A", [_post("A", 1, 1), _post("A", 2, 2)]),
        ("B", [_post("B", 1, 3), _post("B", 2, 1)]),
    )
    picked = select_followup_videos(out, 2)
    assert SearchSurfaceCheckSkill._chapter_anchor(out, picked) == ("B", "tiktok")
    single = select_followup_videos(_out(("A", [_post("A", 1, 1)])), 1)
    assert SearchSurfaceCheckSkill._chapter_anchor(out, single) == ("A", "tiktok")


# ── 確認文 ─────────────────────────────────────────────────────


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(confirm.FOLLOWUP_ENABLED_ENV, "1")
    monkeypatch.setenv(confirm.ONE_SHOT_ENV, "1")
    monkeypatch.setenv(confirm.FOLLOWUP_ALLOWED_EMAILS_ENV, "s-komata@vectorinc.co.jp")


def _meta(channel: str = "D0123", verified: bool = True) -> dict[str, Any]:
    return {
        "user_email": "s-komata@vectorinc.co.jp",
        "identity_verified": verified,
        "channel_id": channel,
    }


def test_confirm_required_only_for_the_pilot_in_dm(on: None, monkeypatch: Any) -> None:
    inp = SearchSurfaceCheckInput(keywords=["A"])
    assert confirm.confirm_required(inp, _meta())
    assert not confirm.confirm_required(inp.model_copy(update={"confirmed": True}), _meta())
    # 取得ジョブを持ってきた呼び出しは確認の後（confirmed の付け忘れで確認を繰り返さない）
    assert not confirm.confirm_required(
        inp.model_copy(update={"acquire_job_id": "tk_0123456789ab"}), _meta()
    )
    assert not confirm.confirm_required(inp, _meta(channel="C0123"))
    assert not confirm.confirm_required(inp, _meta(verified=False))
    assert not confirm.confirm_required(inp, {**_meta(), "user_email": "x@vectorinc.co.jp"})
    monkeypatch.setenv(confirm.ONE_SHOT_ENV, "0")
    assert not confirm.confirm_required(inp, _meta())


def test_wildcard_opens_confirm_and_one_shot_to_everyone(on: None, monkeypatch: Any) -> None:
    """本番の値「*」（10-06 夜の全員開放）で、確認と 1 通化が本人確認済みの全員に効く。

    以前は完全一致の照合で「*」を誰とも一致させず、小俣さんを含む全員で確認も 1 通化も止まっていた。
    """
    monkeypatch.setenv(confirm.FOLLOWUP_ALLOWED_EMAILS_ENV, "*")
    inp = SearchSurfaceCheckInput(keywords=["A"])
    assert confirm.one_shot_enabled("someone@vectorinc.co.jp")
    assert confirm.confirm_required(inp, {**_meta(), "user_email": "someone@vectorinc.co.jp"})
    # 本人を解決できていない（email が無い・形がおかしい）なら「*」でも開かない
    assert not confirm.one_shot_enabled("")
    assert not confirm.one_shot_enabled("not-an-email")
    # チャンネル・本人確認なしでは従来どおり確認を返さない
    assert not confirm.confirm_required(inp, _meta(channel="C0123"))
    assert not confirm.confirm_required(inp, _meta(verified=False))


def test_env_names_match_the_gateway() -> None:
    from teamagent.mcp_gateway import surface_video_followup as g

    assert confirm.FOLLOWUP_ENABLED_ENV == g.ENABLED_ENV
    assert confirm.FOLLOWUP_ALLOWED_EMAILS_ENV == g.ALLOWED_EMAILS_ENV
    assert confirm.ONE_SHOT_ENV == g.ONE_SHOT_ENV


def test_confirm_message_shows_defaults_and_given_values() -> None:
    msg = confirm.build_confirm_message(
        SearchSurfaceCheckInput(keywords=["HIS", "JTB"], platforms=["tiktok"])
    )
    assert "キーワード: HIS／JTB（TikTok）" in msg
    assert "上位から 5本" in msg and "約10分" in msg
    assert "比較する競合: なし" in msg
    msg10 = confirm.build_confirm_message(
        SearchSurfaceCheckInput(
            keywords=["HIS"], max_videos=10, competitor_accounts=["jtb_official", "@his_jp"]
        )
    )
    assert "上位から 10本" in msg10 and "約20分" in msg10
    assert "比較する競合: @jtb_official・@his_jp" in msg10


# ── 競合 ─────────────────────────────────────────────────────


def test_competitor_parts_follow_the_given_order() -> None:
    posts = [
        _post("A", 1, 1, author="jtb_official").model_copy(update={"is_competitor": True}),
        _post("A", 2, 2, author="x"),
        _post("A", 5, 3, author="JTB_Official").model_copy(update={"is_competitor": True}),
    ]
    surface = KwSurface(keyword="A", platform="tiktok", posts=posts)
    assert competitor_parts(surface, ["@his_jp", "jtb_official"]) == [
        ("his_jp", []),
        ("jtb_official", [1, 5]),
    ]
    assert competitor_parts(surface, []) == []


def test_competitor_ranks_reach_the_summary_and_blocks() -> None:
    from teamagent.skills.search_surface_check.slack_render import surface_message
    from tests.skills.search_surface_check.fixtures import KEYWORD
    from tests.skills.search_surface_check.test_video_followup import _JOB_ID, _ctx, _skill

    skill, *_ = _skill()
    inp = SearchSurfaceCheckInput(
        keywords=[KEYWORD],
        platforms=["tiktok"],
        acquire_job_id=_JOB_ID,
        competitor_accounts=["@GONOSARA", "nobody_here"],
    )
    out = skill.run(inp, _ctx())
    surface = out.surfaces[0]
    assert surface.competitor_ranks and surface.competitor_ranks[0] == 1
    assert "- 競合: @GONOSARA 1位" in out.slack_summary
    assert "@nobody_here 上位に無し" in out.slack_summary
    rich = surface_message(out, inp)
    assert rich is not None
    body = "\n".join(b.get("text", {}).get("text", "") for b in rich.blocks)
    assert "• 競合: @GONOSARA <" in body and "@nobody_here 上位に無し" in body


def test_one_shot_message_fits_slack_limits_with_ten_videos() -> None:
    from teamagent.skills._shared.slack_blocks import MAX_BLOCKS, MAX_TOTAL_TEXT, text_size
    from teamagent.skills.search_surface_check.slack_render import one_shot_message
    from tests.skills.search_surface_check.test_video_followup import (
        _first_stage,
        _followup,
        _input,
        _skill,
    )

    skill, *_ = _skill()
    out = _first_stage(skill)
    result = _followup(skill, out, max_videos=10)
    assert result.status == "ok" and len(result.videos) == 10
    rich = one_shot_message(out, _input(), result)
    assert rich is not None
    assert len(rich.blocks) <= MAX_BLOCKS and text_size(rich.blocks) <= MAX_TOTAL_TEXT
    body = "\n".join(b.get("text", {}).get("text", "") for b in rich.blocks if "text" in b)
    assert "上位10本の動画の中身" in body
    assert result.report_url and result.report_url in rich.text  # 最新のレポートだけ
    assert out.report_url not in rich.text
    assert "検索上位チェックの続き" not in body


def test_across_keywords_note_says_ranks_are_combined() -> None:
    from teamagent.skills.search_surface_check.slack_render import followup_message
    from tests.skills.search_surface_check.test_video_followup import (
        _first_stage,
        _followup,
        _skill,
    )

    skill, *_ = _skill()
    result = _followup(skill, _first_stage(skill))
    plain = followup_message(result)
    combined = followup_message(result.model_copy(update={"across_keywords": True}))
    assert plain is not None and combined is not None
    assert "全キーワードの総合" not in plain.text
    assert "全キーワードの総合" in combined.text


def test_videos_are_analyzed_with_their_own_keyword() -> None:
    """全 KW から選んだら、テロップ・発話の KW 判定の基準（query）を動画ごとの KW にする。

    変異: query を「HIS・JTB」の 1 回呼びに戻すと、呼び出しが 1 回になり赤。
    """
    from teamagent.skills.base import SkillContext
    from teamagent.skills.video_algorithm.schema import AnalyzedVideo

    class _Engine:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[int]]] = []

        @staticmethod
        def reserve_video_quota(ctx: SkillContext, count: int) -> int:
            return count

        def analyze_videos(self, metas: list[Any], *, query: str, **kw: Any) -> list[Any]:
            self.calls.append((query, [m.rank for m in metas]))
            return [AnalyzedVideo(meta=m, error="analysis failed") for m in metas]

    out = _out(
        ("HIS", [_post("HIS", 1, 1), _post("HIS", 2, 2)]),
        ("JTB", [_post("JTB", 1, 3), _post("JTB", 2, 1)]),
    )
    videos = select_followup_videos(out, 3)
    engine = _Engine()
    skill = SearchSurfaceCheckSkill(video_engine=engine, publisher=lambda *a, **k: None)
    result = skill.run_video_followup(
        out,
        SearchSurfaceCheckInput(keywords=["HIS", "JTB"], platforms=["tiktok"]),
        SkillContext(request_id="r", metadata={"user_email": "s-komata@vectorinc.co.jp"}),
        videos=videos,
    )
    # 総合 1 位（両 KW に出る video/1・一番上は HIS 1 位）と 2 位（video/3・JTB 1 位）、3 位（video/2）
    assert engine.calls == [("HIS", [1, 3]), ("JTB", [2])]
    assert result.keyword == "HIS・JTB"


def test_one_shot_message_keeps_every_surface_and_the_report_with_four_surfaces() -> None:
    """2 KW × TikTok/IG（4 面）＋10 本でも、面の節とレポートのリンクは残る（削るのは後ろから）。"""
    from teamagent.skills._shared.slack_blocks import MAX_BLOCKS, MAX_TOTAL_TEXT, text_size
    from teamagent.skills.search_surface_check.slack_render import one_shot_message
    from tests.skills.search_surface_check.test_video_followup import (
        _first_stage,
        _followup,
        _input,
        _skill,
    )

    skill, *_ = _skill()
    out = _first_stage(skill)
    base = out.surfaces[0]
    surfaces = [
        base.model_copy(update={"keyword": kw, "platform": pf})
        for kw in ("HIS 海外旅行", "JTB 国内旅行")
        for pf in ("tiktok", "instagram")
    ]
    four = out.model_copy(
        update={"surfaces": surfaces, "keywords": ["HIS 海外旅行", "JTB 国内旅行"]}
    )
    result = _followup(skill, out, max_videos=10)
    rich = one_shot_message(four, _input(), result)
    assert rich is not None
    assert len(rich.blocks) <= MAX_BLOCKS and text_size(rich.blocks) <= MAX_TOTAL_TEXT
    body = "\n".join(b.get("text", {}).get("text", "") for b in rich.blocks if "text" in b)
    for kw in ("HIS 海外旅行", "JTB 国内旅行"):
        assert body.count(f"「{kw}」") >= 2  # TikTok と Instagram の節
    assert f"<{result.report_url}|レポートを開く>" in body
