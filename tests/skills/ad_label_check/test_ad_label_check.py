"""ad_label_check: 媒体の振り分け・ラベル判定・#PR 照合・Slack 表示。

Apify は run_actor_sync を差し替え、actor ごとに本番で起きる形（項目の欠落・error 行・
短縮 URL で ID が無い）を返す。
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.adapters.apify_client import (
    ACTOR_IG_POSTS,
    ACTOR_TIKTOK_VIDEO,
    ACTOR_YT_VIDEOS,
    ApifyClient,
    ApifyError,
    ApifyRunResult,
    ad_label_platform,
)
from teamagent.skills.ad_label_check.schema import AdLabelCheckInput
from teamagent.skills.ad_label_check.skill import AdLabelCheckSkill, find_disclosures
from teamagent.skills.base import SkillContext

TT = "https://www.tiktok.com/@shop/video/7400000000000000001"
TT_PLAIN = "https://www.tiktok.com/@cafe/video/7400000000000000002"
IG = "https://www.instagram.com/reel/DX7lzTOJ1p6/"
YT = "https://youtu.be/dQw4w9WgXcQ"
YT_GONE = "https://www.youtube.com/watch?v=AAAAAAAAAAA"


class FakeApify(ApifyClient):
    def __init__(self, items: dict[str, list[dict[str, Any]]], fail: set[str] = frozenset()):
        super().__init__("test-token")
        self.items = items
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run_actor_sync(self, actor_id: str, run_input: dict[str, Any], **_: Any) -> ApifyRunResult:
        self.calls.append((actor_id, run_input))
        if actor_id in self.fail:
            raise ApifyError("APIFY_RUN_FAILED: boom")
        return ApifyRunResult(
            items=self.items.get(actor_id, []),
            actor_id=actor_id,
            run_id="r1",
            status="SUCCEEDED",
            estimated_cost_usd=0.01,
        )


ITEMS = {
    ACTOR_TIKTOK_VIDEO: [
        # 順序は入力と逆・入力 URL 欄なし＝ID で突合する。
        {"id": "7400000000000000002", "isAd": False, "isSponsored": False,
         "text": "おいしい #カフェ", "hashtags": [{"name": "カフェ"}]},
        {"id": "7400000000000000001", "isAd": False, "isSponsored": True,
         "text": "新作 #PR", "hashtags": [{"name": "PR"}]},
    ],
    ACTOR_IG_POSTS: [
        {"inputUrl": IG, "shortCode": "DX7lzTOJ1p6", "paidPartnership": True,
         "isSponsored": False, "sponsors": [{"id": "1", "username": "sephora"}],
         "caption": "summer look", "hashtags": []},
    ],
    ACTOR_YT_VIDEOS: [
        {"id": "dQw4w9WgXcQ", "title": "【PR】レビュー", "text": "", "hashtags": []},
        {"url": YT_GONE, "error": "VIDEO_UNAVAILABLE"},
    ],
}  # fmt: skip


def _run(skill: AdLabelCheckSkill, urls: list[str]):
    return skill.run(AdLabelCheckInput(urls=urls), SkillContext(metadata={"user_email": "u@x.jp"}))


def test_platform_detection_is_https_only() -> None:
    assert ad_label_platform("https://vt.tiktok.com/ZSabc/") == "tiktok"
    assert ad_label_platform("https://m.youtube.com/shorts/dQw4w9WgXcQ") == "youtube"
    assert ad_label_platform("https://instagram.com/p/ABC/") == "instagram"
    assert ad_label_platform("http://www.tiktok.com/@a/video/1") is None
    assert ad_label_platform("https://evil-tiktok.com/x") is None
    assert ad_label_platform("https://x.com/a/status/1") is None


@pytest.mark.parametrize(
    ("text", "tags", "expected"),
    [
        ("新作 #PR", [], ["#PR"]),
        ("#PRADA の新作", [], []),
        ("全角 ＃ｐｒ です", [], ["#pr"]),
        ("【PR】レビュー", [], ["【PR】"]),
        ("", ["ad", "travel"], ["#ad"]),
        ("#タイアップ #提供 #タイアップ", [], ["#タイアップ", "#提供"]),
    ],
)
def test_find_disclosures(text: str, tags: list[str], expected: list[str]) -> None:
    assert find_disclosures(text, tags) == expected


def test_mixed_urls_are_grouped_by_platform_with_labels_and_tags() -> None:
    fake = FakeApify(ITEMS)
    pasted = "\n".join([TT, IG, YT, "https://example.com/a", TT_PLAIN, YT_GONE])
    out = _run(AdLabelCheckSkill(apify=fake), [pasted])

    # 媒体ごとに 1 run・ダウンロード系は止める。
    assert sorted(a for a, _ in fake.calls) == sorted(
        [ACTOR_TIKTOK_VIDEO, ACTOR_IG_POSTS, ACTOR_YT_VIDEOS]
    )
    tt_input = dict(fake.calls)[ACTOR_TIKTOK_VIDEO]
    assert tt_input["postURLs"] == [TT, TT_PLAIN] and tt_input["shouldDownloadVideos"] is False
    assert dict(fake.calls)[ACTOR_IG_POSTS]["resultsType"] == "posts"

    by_url = {r.url: r for r in out.results}
    assert by_url[TT].label is True and by_url[TT].disclosure_tags == ["#PR"]
    assert by_url[TT_PLAIN].label is False and by_url[TT_PLAIN].needs_review
    assert by_url[IG].label is True and by_url[IG].partners == ["sephora"]
    # YouTube は isPaidContent 欄が無い＝「取れず」（なし と書かない）・【PR】は拾う。
    assert by_url[YT].label is None and by_url[YT].disclosure_tags == ["【PR】"]
    assert by_url[YT_GONE].found is False and not by_url[YT_GONE].needs_review

    assert out.slack_summary == "\n\n".join(
        [
            "「TT」\n"
            f"{TT}　ラベルOK　ハッシュタグ#PR OK\n"
            f"{TT_PLAIN}　ラベルなし　ハッシュタグなし　⚠️要確認",
            f"「IG」\n{IG}　ラベルOK（@sephora）　ハッシュタグなし",
            "「YT」\n"
            f"{YT}　ラベル取れず　ハッシュタグ【PR】 OK\n"
            f"{YT_GONE}　取得できず（削除・非公開・URL違いの可能性）",
            "「対象外」\nhttps://example.com/a",
        ]
    )
    assert out.total_cost_usd == pytest.approx(0.03)


def test_short_url_without_id_is_matched_only_when_unambiguous() -> None:
    short = "https://vt.tiktok.com/ZSabc/"
    one = FakeApify({ACTOR_TIKTOK_VIDEO: [{"id": "1", "isSponsored": True, "text": ""}]})
    assert _run(AdLabelCheckSkill(apify=one), [short]).results[0].label is True

    two = FakeApify({ACTOR_TIKTOK_VIDEO: [{"id": "1", "isSponsored": True}, {"id": "2"}]})
    res = _run(AdLabelCheckSkill(apify=two), [short, "https://vt.tiktok.com/ZSdef/"]).results
    assert [r.found for r in res] == [False, False]


def test_one_platform_failure_keeps_others_and_never_says_no_label() -> None:
    fake = FakeApify(ITEMS, fail={ACTOR_IG_POSTS})
    out = _run(AdLabelCheckSkill(apify=fake), [TT, IG])
    by_url = {r.url: r for r in out.results}
    assert by_url[TT].label is True
    assert by_url[IG].found is False and by_url[IG].label is None
    assert "取得できず" in out.slack_summary and "instagram の取得に失敗" in out.slack_summary


def test_rollout_gate_blocks_before_any_apify_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AD_LABEL_CHECK_ALLOWED_EMAILS", "other@x.jp")
    fake = FakeApify(ITEMS)
    out = _run(AdLabelCheckSkill(apify=fake), [TT])
    assert fake.calls == [] and "段階公開中" in out.slack_summary


def test_input_upgrades_slack_http_and_schemeless_links_to_https() -> None:
    # Slack は「tiktok.com/@a/video/1」と貼られた URL を <http://…|表示名> で届ける（2026-10-30 本番の DM）。
    inp = AdLabelCheckInput(
        urls=[
            "<http://tiktok.com/@trend/video/7699768122319949076|tiktok.com/@trend/video/7699768122319949076>",
            "instagram.com/p/De1ETJtGXpD",
            "http://youtube.com/shorts/hzfcNYdR1JM",
        ]
    )
    assert inp.urls == [
        "https://tiktok.com/@trend/video/7699768122319949076",
        "https://instagram.com/p/De1ETJtGXpD",
        "https://youtube.com/shorts/hzfcNYdR1JM",
    ]
    assert [ad_label_platform(u) for u in inp.urls] == ["tiktok", "instagram", "youtube"]


def test_input_splits_lines_dedupes_and_caps() -> None:
    urls = [f"{TT}\n{TT}\n{IG}"] + [f"https://youtu.be/{i:011d}" for i in range(30)]
    inp = AdLabelCheckInput(urls=urls)
    assert inp.urls[:2] == [TT, IG] and len(inp.urls) == 20
