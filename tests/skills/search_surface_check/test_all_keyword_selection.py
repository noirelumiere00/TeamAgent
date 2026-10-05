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
    assert not confirm.confirm_required(inp, _meta(channel="C0123"))
    assert not confirm.confirm_required(inp, _meta(verified=False))
    assert not confirm.confirm_required(inp, {**_meta(), "user_email": "x@vectorinc.co.jp"})
    monkeypatch.setenv(confirm.ONE_SHOT_ENV, "0")
    assert not confirm.confirm_required(inp, _meta())


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
