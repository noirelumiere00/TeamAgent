"""search_surface_check の単体テスト（Apify/Bedrock/S3ソースをモック）。"""

from __future__ import annotations

import json
from typing import Any

from teamagent.adapters.apify_client import ApifyError, IgPost
from teamagent.skills.base import SkillContext
from teamagent.skills.search_surface_check.schema import SearchSurfaceCheckInput
from teamagent.skills.search_surface_check.skill import SearchSurfaceCheckSkill

_JOB_ID = "tk_0123456789ab"


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="req-test", user_id="U1", metadata={"user_email": "a@vectorinc.co.jp"}
    )


def _ig(shortcode: str, author: str = "gourmet_a", views: int = 1000) -> IgPost:
    return IgPost(
        shortcode=shortcode,
        url=f"https://www.instagram.com/reel/{shortcode}/",
        caption="新作レビュー",
        author=author,
        like_count=100,
        view_count=views,
        comment_count=5,
        thumb_url="",
        post_type="reel",
        source_actor="apify~instagram-search-scraper",
    )


class _FakeApify:
    def __init__(self, posts: list[IgPost] | None = None, *, fail: bool = False) -> None:
        self._posts = posts or []
        self._fail = fail
        self.calls: list[tuple[str, str]] = []

    def ig_search(self, keyword: str, *, surface: str, **kw: Any) -> tuple[list[IgPost], float]:
        self.calls.append((keyword, surface))
        if self._fail:
            raise ApifyError("APIFY_RUN_FAILED: ig")
        return list(self._posts), 0.1


class _FakeBedrock:
    """全投稿を id順に news/ugc 交互に分類するフェイク。"""

    def converse(self, messages: list[dict[str, Any]], **kw: Any) -> Any:
        text = messages[0]["content"][0]["text"]
        if "カテゴリ構成比" in text:
            body = "TikTokはニュース面、IGはグルメUGC面。"
        else:
            n = text.count('"id"')
            cats = {str(i): ("news" if i % 2 == 0 else "ugc") for i in range(n)}
            body = json.dumps({"categories": cats, "note": ""})
        return type("R", (), {"text": body, "usage": type("U", (), {"cost_usd": 0.001})()})()


class _FakeSource:
    def __init__(self, posts: list[dict[str, Any]]) -> None:
        self._posts = posts

    def posts(self, n: int | None = None) -> list[dict[str, Any]]:
        return self._posts[:n] if n else self._posts


def _publisher(path: str, *, request_id: str, query: str) -> str:
    return "https://s3.example/surface"


def _s3_posts() -> list[dict[str, Any]]:
    return [
        {
            "id": "p0001",
            "kw": "セブン",
            "rank_display": 1,
            "title": "ファミマ試着室",
            "account_id": "tbsnews",
            "account_name": "TBS NEWS",
            "followers": 500000,
            "plays": 212000,
            "likes": 5000,
            "comments": 100,
            "url": "https://www.tiktok.com/@tbsnews/video/1",
        },
        {
            "id": "p0002",
            "kw": "セブン",
            "rank_display": 2,
            "title": "セブン新作食べてみた",
            "account_id": "seven_official",
            "account_name": "セブン公式",
            "followers": 90000,
            "plays": 7000,
            "likes": 300,
            "comments": 10,
            "url": "https://www.tiktok.com/@seven_official/video/2",
        },
    ]


def test_s3_path_with_ig_and_client_marking() -> None:
    # IG: 同一 shortcode が2回出る=出現頻度2で面の定着度が上がる
    apify = _FakeApify([_ig("aaa"), _ig("aaa"), _ig("bbb", author="user_b", views=99)])
    skill = SearchSurfaceCheckSkill(
        apify=apify,  # type: ignore[arg-type]
        bedrock=_FakeBedrock(),
        publisher=_publisher,
        tiktok_source_factory=lambda job_id, audit_hash: _FakeSource(_s3_posts()),
    )
    out = skill.run(
        SearchSurfaceCheckInput(
            keywords=["セブン"],
            client_accounts=["@seven_official"],
            acquire_job_id=_JOB_ID,
        ),
        _ctx(),
    )
    assert out.report_url == "https://s3.example/surface"
    tiktok = next(s for s in out.surfaces if s.platform == "tiktok")
    ig = next(s for s in out.surfaces if s.platform == "instagram")
    # rank_display 順の忠実記録
    assert [p.rank for p in tiktok.posts] == [1, 2]
    # クライアント在圏判定（決定的マッチ）
    assert tiktok.client_ranks == [2]
    assert tiktok.posts[1].is_client
    # IG は出現頻度順（aaa が2回 → 1位）
    assert ig.posts[0].appearances == 2 and ig.posts[0].rank == 1
    # 勢力図が計算される
    assert sum(tiktok.category_ratio.values()) > 0.99
    assert "TikTokはニュース面" in out.comparison_summary
    assert "在圏" in out.slack_summary


def test_many_keywords_without_acquire_job_guides_to_acquire() -> None:
    skill = SearchSurfaceCheckSkill(
        apify=_FakeApify(), bedrock=_FakeBedrock(), publisher=_publisher
    )  # type: ignore[arg-type]
    out = skill.run(SearchSurfaceCheckInput(keywords=["セブン", "ファミマ", "ローソン"]), _ctx())
    assert "tiktok_acquire" in out.slack_summary
    assert out.surfaces == []


def test_direct_path_for_few_keywords() -> None:
    class _V:
        def __init__(self) -> None:
            self.url = "https://www.tiktok.com/@a/video/1"
            self.desc = "即席チェック"
            self.play_count = 100
            self.digg_count = 10
            self.comment_count = 1
            self.author = type("A", (), {"unique_id": "a_user", "follower_count": 10})()

    def fake_search(kw: str, *, max_videos: int, request_id: str) -> Any:
        return type("R", (), {"videos": (_V(),)})()

    skill = SearchSurfaceCheckSkill(
        apify=_FakeApify([_ig("ccc")]),  # type: ignore[arg-type]
        bedrock=_FakeBedrock(),
        publisher=_publisher,
        tiktok_search_fn=fake_search,
    )
    out = skill.run(SearchSurfaceCheckInput(keywords=["セブン"]), _ctx())
    tiktok = next(s for s in out.surfaces if s.platform == "tiktok")
    assert tiktok.posts[0].author == "a_user" and tiktok.posts[0].rank == 1


def test_ig_failure_degrades_with_warning() -> None:
    skill = SearchSurfaceCheckSkill(
        apify=_FakeApify(fail=True),  # type: ignore[arg-type]
        bedrock=_FakeBedrock(),
        publisher=_publisher,
        tiktok_source_factory=lambda job_id, audit_hash: _FakeSource(_s3_posts()),
    )
    out = skill.run(
        SearchSurfaceCheckInput(keywords=["セブン"], acquire_job_id=_JOB_ID),
        _ctx(),
    )
    assert any("IG面" in w for w in out.warnings)
    assert any(s.platform == "tiktok" for s in out.surfaces)  # TikTok側は生きている


def test_rollout_denied(monkeypatch: Any) -> None:
    monkeypatch.setenv("SEARCH_SURFACE_ALLOWED_EMAILS", "other@vectorinc.co.jp")
    skill = SearchSurfaceCheckSkill(apify=_FakeApify(), publisher=_publisher)  # type: ignore[arg-type]
    out = skill.run(SearchSurfaceCheckInput(keywords=["セブン"]), _ctx())
    assert "段階公開中" in out.slack_summary


def test_ig_surface_env_default(monkeypatch: Any) -> None:
    monkeypatch.setenv("IG_SURFACE_DEFAULT", "hashtag")
    apify = _FakeApify([_ig("ddd")])
    skill = SearchSurfaceCheckSkill(
        apify=apify,  # type: ignore[arg-type]
        bedrock=_FakeBedrock(),
        publisher=_publisher,
        tiktok_source_factory=lambda job_id, audit_hash: _FakeSource([]),
    )
    skill.run(
        SearchSurfaceCheckInput(keywords=["セブン"], platforms=["instagram"], acquire_job_id=None),
        _ctx(),
    )
    assert apify.calls == [("セブン", "hashtag")]  # 検証ゲートの切替は env 一発
