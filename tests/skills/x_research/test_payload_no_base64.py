"""MCP応答に base64 画像を載せない回帰テスト（2026-07-15 実機事故の再発防止）。

事故の機序（CloudWatch/S3実測で確定）:
    XPostCard.avatar_data/media_data に base64 data URI が載ったまま応答へ出て、ツール結果が
    4,093,882 bytes に膨張 → openclaw が **約64KB(実測 maxMessageTextChars=63962)** で打ち切り
    → 98.4% が黙って捨てられ posts[1:] の url が LLM の文脈から消滅 → LLM は handle だけ知って
    status ID を知らない状態になり `https://x.com/ogu_gourmet/status/[該当投稿]` を捏造して
    営業に提示した（実在しないURL）。

    画像は S3 のレポートHTML側に焼かれているので、応答から落としても成果物の見た目は不変。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from teamagent.adapters.apify_client import XPost
from teamagent.skills.base import SkillContext
from teamagent.skills.x_research.schema import XNeedsMiningInput, XVoiceSearchInput
from teamagent.skills.x_research.skill import XNeedsMiningSkill, XVoiceSearchSkill

_AVATAR = "data:image/jpeg;base64," + "A" * 3000
_MEDIA = "data:image/jpeg;base64," + "B" * 300000  # 実測 156KB〜330KB 級


def _post(pid: str, likes: int = 10) -> XPost:
    return XPost(
        post_id=pid,
        url=f"https://x.com/u{pid}/status/{pid}",
        author_handle=f"u{pid}",
        author_name="",
        text="白湯うまい",
        like_count=likes,
        retweet_count=0,
        reply_count=0,
        created_at="2026-07-01",
        lang="ja",
        source_actor="scraper_one~x-posts-search",
    )


class _FakeApify:
    def __init__(self, posts: list[XPost]) -> None:
        self.posts = posts

    def search_posts(self, query: str, **kw: Any) -> tuple[list[XPost], float]:
        return list(self.posts), 0.01

    def verify_posts(self, urls: list[str], **kw: Any) -> tuple[dict[str, XPost | None], float]:
        out: dict[str, XPost | None] = {}
        for u in urls:
            pid = u.rsplit("/", 1)[-1]
            out[u] = next((p for p in self.posts if p.post_id == pid), None)
        return out, 0.005


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text
        self.usage = type("U", (), {"cost_usd": 0.001})()


class _FakeBedrock:
    def converse(self, **kw: Any) -> _FakeResp:
        return _FakeResp(json.dumps({"keep": ["1", "2", "3"], "noise_note": ""}))


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="req-test", user_id="U1", metadata={"user_email": "a@vectorinc.co.jp"}
    )


@pytest.fixture
def _skill_with_images(monkeypatch: pytest.MonkeyPatch) -> tuple[XVoiceSearchSkill, dict[str, str]]:
    """全カードに巨大 base64 を埋め込み、生成HTMLを捕捉する skill を返す。"""
    captured: dict[str, str] = {}

    def _fake_embed(self: Any, cards: list[Any], selected: list[Any], **kw: Any) -> None:
        for c in cards:
            c.avatar_data = _AVATAR
            c.media_data = [_MEDIA]

    monkeypatch.setattr(XVoiceSearchSkill, "_embed_card_images", _fake_embed, raising=True)

    def _publisher(path: str, *, request_id: str, query: str) -> str:
        with open(path, encoding="utf-8") as f:
            captured["html"] = f.read()
        return "https://s3.example/signed"

    skill = XVoiceSearchSkill(
        apify=_FakeApify([_post("1", 30), _post("2", 20), _post("3", 10)]),  # type: ignore[arg-type]
        bedrock=_FakeBedrock(),
        publisher=_publisher,
    )
    return skill, captured


def test_response_carries_no_base64(
    _skill_with_images: tuple[XVoiceSearchSkill, dict[str, str]],
) -> None:
    """本命: 応答(posts)に data URI が1つも残らない＝openclaw の切り詰めを誘発しない。"""
    skill, _ = _skill_with_images
    out = skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯"]), _ctx())
    assert out.posts, "前提: カードが返っていること"
    for c in out.posts:
        assert c.avatar_data == ""
        assert c.media_data == []
    dumped = json.dumps(out.model_dump(), ensure_ascii=False, default=str)
    assert "data:image" not in dumped  # server.py が返す実際の形（json.dumps(model_dump)）で確認


def test_response_stays_small_enough_to_survive_openclaw_cap(
    _skill_with_images: tuple[XVoiceSearchSkill, dict[str, str]],
) -> None:
    """応答が openclaw の実測キャップ(約64KB)に収まる＝posts[1:].url が切り捨てられない。

    事故時は 4,093,882 bytes で 63,962 字に切られ 98.4% が消えた。
    """
    skill, _ = _skill_with_images
    out = skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯"]), _ctx())
    dumped = json.dumps(out.model_dump(), ensure_ascii=False, default=str)
    assert len(dumped) < 63962, f"応答が openclaw のキャップを超える: {len(dumped)}字"


def test_all_post_urls_reach_the_response(
    _skill_with_images: tuple[XVoiceSearchSkill, dict[str, str]],
) -> None:
    """全投稿の url が応答に届く（1位だけ届いて2位以降が消える＝捏造の温床、を防ぐ）。"""
    skill, _ = _skill_with_images
    out = skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯"]), _ctx())
    urls = [c.url for c in out.posts]
    assert urls == [
        "https://x.com/u1/status/1",
        "https://x.com/u2/status/2",
        "https://x.com/u3/status/3",
    ]
    assert all(u for u in urls), "url が空のカードがある＝LLMが status ID を知り得ない"


def test_html_report_still_embeds_images(
    _skill_with_images: tuple[XVoiceSearchSkill, dict[str, str]],
) -> None:
    """成果物(HTML)には画像が焼かれたまま＝営業が見る見た目は不変（strip は応答のみ）。"""
    skill, captured = _skill_with_images
    skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯"]), _ctx())
    assert "data:image" in captured["html"], "HTML から画像が消えている＝strip が早すぎる"


def test_slack_summary_includes_post_urls(
    _skill_with_images: tuple[XVoiceSearchSkill, dict[str, str]],
) -> None:
    """slack_summary の上位3件に実URLが載る（handleだけだとLLMがstatus IDを捏造する）。"""
    skill, _ = _skill_with_images
    out = skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯"]), _ctx())
    for u in ("https://x.com/u1/status/1", "https://x.com/u2/status/2"):
        assert u in out.slack_summary


# ---- ② needs も同じ事故が起きる（voice だけ直すと非対称に穴が残る） --------------


class _FakeNeedsBedrock:
    """needs の分類LLM応答（クラスタ）を返す。"""

    def converse(self, **kw: Any) -> _FakeResp:
        return _FakeResp(
            json.dumps(
                {
                    "clusters": [
                        {
                            "label": "手間",
                            "need": "簡単に飲みたい",
                            "post_ids": ["1", "2", "3"],
                            "evidence": "白湯めんどくさい",
                        }
                    ],
                    "hypothesis_summary": "手間が障壁",
                }
            )
        )


@pytest.fixture
def _needs_skill(monkeypatch: pytest.MonkeyPatch) -> tuple[XNeedsMiningSkill, dict[str, str]]:
    captured: dict[str, str] = {}

    def _fake_embed(self: Any, cards: list[Any], selected: list[Any], **kw: Any) -> None:
        for c in cards:
            c.avatar_data = _AVATAR
            c.media_data = [_MEDIA]

    monkeypatch.setattr(XNeedsMiningSkill, "_embed_card_images", _fake_embed, raising=True)

    def _publisher(path: str, *, request_id: str, query: str) -> str:
        with open(path, encoding="utf-8") as f:
            captured["html"] = f.read()
        return "https://s3.example/signed"

    skill = XNeedsMiningSkill(
        apify=_FakeApify([_post("1", 30), _post("2", 20), _post("3", 10)]),  # type: ignore[arg-type]
        bedrock=_FakeNeedsBedrock(),
        analysis_bedrock=_FakeNeedsBedrock(),
        publisher=_publisher,
    )
    return skill, captured


def test_needs_response_carries_no_base64(
    _needs_skill: tuple[XNeedsMiningSkill, dict[str, str]],
) -> None:
    """needs も応答に base64 を載せない。

    needs は voice と同じ _verify_selected 経由で画像が載るため、strip を落とすと 3件でも
    約910KB・max_selected=30 なら約9MB＝voice と同一の切り詰め事故が丸ごと再現する。
    voice 側テストだけでは needs の strip 削除を検出できないため、ここで独立に固定する。
    """
    skill, _ = _needs_skill
    out = skill.run(XNeedsMiningInput(theme="白湯"), _ctx())
    assert out.posts, "前提: カードが返っていること"
    for c in out.posts:
        assert c.avatar_data == ""
        assert c.media_data == []
    dumped = json.dumps(out.model_dump(), ensure_ascii=False, default=str)
    assert "data:image" not in dumped
    assert len(dumped) < 63962, f"応答が openclaw のキャップを超える: {len(dumped)}字"


def test_needs_html_still_embeds_images(
    _needs_skill: tuple[XNeedsMiningSkill, dict[str, str]],
) -> None:
    """canary: needs でも HTML には画像が焼かれている（strip が早すぎないこと）。"""
    skill, captured = _needs_skill
    skill.run(XNeedsMiningInput(theme="白湯"), _ctx())
    assert "data:image" in captured["html"]


def test_needs_slack_summary_includes_post_url(
    _needs_skill: tuple[XNeedsMiningSkill, dict[str, str]],
) -> None:
    """needs の要約にも実URLを載せる（voice だけ二重防御で needs が素、という非対称を作らない）。"""
    skill, _ = _needs_skill
    out = skill.run(XNeedsMiningInput(theme="白湯"), _ctx())
    assert "https://x.com/u1/status/1" in out.slack_summary


# ---- 外部データ由来URLの防御（safe_href を通す） -------------------------------


def test_slack_summary_drops_non_sns_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apify actor が返した非SNSホストURLは要約に出さない（偽ログイン誘導の配信を防ぐ）。

    url は actor の生JSON由来（apify_client が素通し）。納品HTMLは safe_href で弾いているのに
    Slack だけ素通しだと、**自動リンク化される分だけ配信力が強い面**でガードが外れる。
    """

    def _fake_embed(self: Any, cards: list[Any], selected: list[Any], **kw: Any) -> None:
        return None

    monkeypatch.setattr(XVoiceSearchSkill, "_embed_card_images", _fake_embed, raising=True)
    evil = XPost(
        post_id="1",
        url="https://x-com-login.evil.example/verify",  # x.com に似せた別ホスト
        author_handle="u1",
        author_name="",
        text="白湯うまい",
        like_count=999,
        retweet_count=0,
        reply_count=0,
        created_at="2026-07-01",
        lang="ja",
        source_actor="scraper_one~x-posts-search",
    )
    skill = XVoiceSearchSkill(
        apify=_FakeApify([evil]),  # type: ignore[arg-type]
        bedrock=_FakeBedrock(),
        publisher=lambda path, *, request_id, query: "https://s3.example/signed",
    )
    out = skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯"]), _ctx())
    assert "evil.example" not in out.slack_summary  # 危険ホストは要約に出さない
    assert "@u1" in out.slack_summary  # 投稿自体は落とさない（黙って消さない）
