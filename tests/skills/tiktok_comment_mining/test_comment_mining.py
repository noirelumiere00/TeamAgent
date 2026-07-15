"""tiktok_comment_mining の単体テスト（chromium/Apify/Bedrock をモック）。"""

from __future__ import annotations

import json
from typing import Any

from teamagent.adapters.apify_client import ApifyError
from teamagent.adapters.tiktok_scraper import TikTokComment, TikTokCommentResult, TikTokScrapeError
from teamagent.skills.base import SkillContext
from teamagent.skills.tiktok_comment_mining.schema import CommentMiningInput
from teamagent.skills.tiktok_comment_mining.skill import TikTokCommentMiningSkill

_URL = "https://www.tiktok.com/@mayo/video/1"


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="req-test", user_id="U1", metadata={"user_email": "a@vectorinc.co.jp"}
    )


def _chromium_ok(url: str, *, max_comments: int, request_id: str) -> TikTokCommentResult:
    return TikTokCommentResult(
        video_url=url,
        comments=(
            TikTokComment(text="メロンパンクッキー美味しすぎ", likes=16, author="a"),
            TikTokComment(text="もう売ってなかった…", likes=8, author="b"),
        ),
    )


def _chromium_fail(url: str, *, max_comments: int, request_id: str) -> TikTokCommentResult:
    raise TikTokScrapeError("TIKTOK_BLOCKED: cloud ip")


class _FakeApify:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def tiktok_comments(self, url: str, **kw: Any) -> tuple[list[dict[str, Any]], float]:
        self.calls += 1
        if self.fail:
            raise ApifyError("APIFY_RUN_FAILED")
        return [{"text": "Apify経由コメント", "likes": 3, "author": "c"}], 0.001


_CLASSIFY_JSON = json.dumps(
    {
        "buckets": [
            {"category": "推薦", "count": 1, "example_ids": ["c0"]},
            {"category": "売切れ嘆き", "count": 1, "example_ids": ["c1"]},
        ],
        "consumer_vocabulary": ["爆食", "ちょい足し"],
        "common_questions": [],
        "pain_points": ["売切れ"],
        "desires": [],
        "purchase_signals": ["買いに行く"],
        "overall_sentiment": "positive",
        "key_themes": ["新作レビュー"],
    }
)


class _FakeBedrock:
    def __init__(self, text: str = _CLASSIFY_JSON) -> None:
        self._text = text

    def converse(self, **kw: Any) -> Any:
        return type("R", (), {"text": self._text, "usage": type("U", (), {"cost_usd": 0.002})()})()


def _publisher(path: str, *, request_id: str, query: str) -> str:
    return "https://s3.example/comments"


def test_chromium_primary_path() -> None:
    apify = _FakeApify()
    skill = TikTokCommentMiningSkill(
        apify=apify,  # type: ignore[arg-type]
        bedrock=_FakeBedrock(),
        publisher=_publisher,
        comments_fn=_chromium_ok,
    )
    out = skill.run(CommentMiningInput(video_urls=[_URL]), _ctx())
    assert out.scraped_comments == 2
    assert apify.calls == 0  # 一次経路が生きていれば Apify は呼ばない
    ins = out.videos[0]
    assert ins.source == "chromium"
    assert [b.category for b in ins.buckets] == ["推薦", "売切れ嘆き"]
    assert ins.buckets[0].examples == ["メロンパンクッキー美味しすぎ"]
    assert "爆食" in out.cross_vocabulary
    assert out.report_url == "https://s3.example/comments"
    assert "コメント欄マイニング" in out.slack_summary


def test_comment_dedup_key_order_independent_and_batch_sensitive() -> None:
    """⑤ の dedup キーは client+動画URL集合。順不同=同一、別バッチ=別（#214-3 対称）。"""
    from teamagent.skills.tiktok_comment_mining.skill import _comment_dedup_key

    a = CommentMiningInput(video_urls=["u1", "u2"], client_name="サンマルク")
    b = CommentMiningInput(video_urls=["u2", "u1"], client_name="サンマルク")  # 順不同=同一
    c = CommentMiningInput(video_urls=["u3"], client_name="サンマルク")  # 別バッチ=別
    assert _comment_dedup_key(a) == _comment_dedup_key(b)
    assert _comment_dedup_key(a) != _comment_dedup_key(c)


def test_comment_persists_with_batch_dedup_key() -> None:
    """永続化に検索定義ハッシュを dedup_key として渡す（同日別バッチを潰さない・#214-3）。"""
    from teamagent.skills.tiktok_comment_mining.skill import _comment_dedup_key

    class _Rec:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def schedule(self, **kw: Any) -> None:
            self.calls.append(kw)

    rec = _Rec()
    skill = TikTokCommentMiningSkill(
        apify=_FakeApify(),  # type: ignore[arg-type]
        bedrock=_FakeBedrock(),
        publisher=_publisher,
        comments_fn=_chromium_ok,
        persister=rec,  # type: ignore[arg-type]
    )
    inp = CommentMiningInput(video_urls=[_URL], client_name="サンマルク")
    skill.run(inp, _ctx())
    assert len(rec.calls) == 1
    assert rec.calls[0]["tool"] == "tiktok_comment"
    assert rec.calls[0]["dedup_key"] == _comment_dedup_key(inp)


def test_apify_fallback_on_chromium_block() -> None:
    apify = _FakeApify()
    skill = TikTokCommentMiningSkill(
        apify=apify,  # type: ignore[arg-type]
        bedrock=_FakeBedrock(),
        publisher=_publisher,
        comments_fn=_chromium_fail,
    )
    out = skill.run(CommentMiningInput(video_urls=[_URL]), _ctx())
    assert apify.calls == 1
    assert out.videos[0].source == "apify"
    assert out.total_cost_usd > 0  # Apify課金が計上される


def test_both_paths_fail_returns_zero_message() -> None:
    skill = TikTokCommentMiningSkill(
        apify=_FakeApify(fail=True),  # type: ignore[arg-type]
        bedrock=_FakeBedrock(),
        publisher=_publisher,
        comments_fn=_chromium_fail,
    )
    out = skill.run(CommentMiningInput(video_urls=[_URL]), _ctx())
    assert out.scraped_comments == 0
    assert "取得できませんでした" in out.slack_summary
    assert out.warnings


def test_classify_failure_degrades_to_counts_only() -> None:
    skill = TikTokCommentMiningSkill(
        apify=_FakeApify(),  # type: ignore[arg-type]
        bedrock=_FakeBedrock("これはJSONではない"),
        publisher=_publisher,
        comments_fn=_chromium_ok,
    )
    out = skill.run(CommentMiningInput(video_urls=[_URL]), _ctx())
    assert out.videos[0].total_comments == 2 and out.videos[0].buckets == []
    assert any("分類" in w for w in out.warnings)


def test_hallucinated_example_id_is_not_returned() -> None:
    payload = json.dumps(
        {
            "buckets": [
                {"category": "推薦", "count": 1, "example_ids": ["c999"]},
            ]
        }
    )
    skill = TikTokCommentMiningSkill(
        apify=_FakeApify(),  # type: ignore[arg-type]
        bedrock=_FakeBedrock(payload),
        publisher=_publisher,
        comments_fn=_chromium_ok,
    )
    out = skill.run(CommentMiningInput(video_urls=[_URL]), _ctx())
    assert out.videos[0].buckets[0].examples == []


def test_classify_false_skips_llm() -> None:
    class _NeverBedrock:
        def converse(self, **kw: Any) -> Any:  # pragma: no cover
            raise AssertionError("呼ばれないはず")

    skill = TikTokCommentMiningSkill(
        apify=_FakeApify(),  # type: ignore[arg-type]
        bedrock=_NeverBedrock(),
        publisher=_publisher,
        comments_fn=_chromium_ok,
    )
    out = skill.run(CommentMiningInput(video_urls=[_URL], classify=False), _ctx())
    assert out.scraped_comments == 2 and out.videos[0].buckets == []


def test_rollout_denied(monkeypatch: Any) -> None:
    monkeypatch.setenv("COMMENT_MINING_ALLOWED_EMAILS", "other@vectorinc.co.jp")
    skill = TikTokCommentMiningSkill(publisher=_publisher)
    out = skill.run(CommentMiningInput(video_urls=[_URL]), _ctx())
    assert "段階公開中" in out.slack_summary


def test_invalid_url_rejected_before_any_fetch() -> None:
    # url_guard で弾かれるURLは、chromium/Apify どちらにも渡さず即エラーで返す
    # （縮退経路で未検証URLがApifyに漏れる穴を塞ぐ）。
    apify = _FakeApify()

    def _never(url: str, *, max_comments: int, request_id: str) -> Any:  # pragma: no cover
        raise AssertionError("不正URLで取得を呼んではいけない")

    skill = TikTokCommentMiningSkill(
        apify=apify,  # type: ignore[arg-type]
        bedrock=_FakeBedrock(),
        publisher=_publisher,
        comments_fn=_never,
    )
    out = skill.run(CommentMiningInput(video_urls=["https://evil.example/x?u=tiktok.com"]), _ctx())
    assert "不正なURL" in out.slack_summary
    assert apify.calls == 0 and out.scraped_comments == 0
