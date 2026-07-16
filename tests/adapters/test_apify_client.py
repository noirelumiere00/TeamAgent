"""apify_client の単体テスト（httpx MockTransport 注入・実課金ゼロ）。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from teamagent.adapters.apify_client import (
    ACTOR_IG_HASHTAG,
    ACTOR_IG_SEARCH,
    ACTOR_X_PERIOD,
    ACTOR_X_SEARCH,
    ACTOR_X_SEARCH_FALLBACK,
    ApifyClient,
    ApifyError,
)
from teamagent.adapters.cost_guard import CostLimitExceededError


class _FakeApify:
    """Apify REST v2 の最小モック。actor別に items / statusMessage を差し替え可能。"""

    def __init__(self) -> None:
        self.items_by_actor: dict[str, list[dict[str, Any]]] = {}
        self.status_message = ""
        self.final_status = "SUCCEEDED"
        self.calls: list[str] = []
        self.aborted: list[str] = []
        self._run_actor: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if request.method == "POST" and path.startswith("/v2/acts/"):
            actor = path.split("/v2/acts/")[1].split("/")[0]
            run_id = f"run-{actor}"
            self._run_actor[run_id] = actor
            return httpx.Response(
                201,
                json={"data": {"id": run_id, "status": "READY", "defaultDatasetId": f"ds-{actor}"}},
            )
        if request.method == "GET" and path.startswith("/v2/actor-runs/"):
            run_id = path.split("/v2/actor-runs/")[1]
            actor = self._run_actor.get(run_id, "")
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": run_id,
                        "status": self.final_status,
                        "statusMessage": self.status_message,
                        "defaultDatasetId": f"ds-{actor}",
                    }
                },
            )
        if request.method == "POST" and "/abort" in path:
            self.aborted.append(path)
            return httpx.Response(200, json={"data": {}})
        if request.method == "GET" and path.startswith("/v2/datasets/"):
            actor = path.split("/v2/datasets/ds-")[1].split("/")[0]
            return httpx.Response(200, json=self.items_by_actor.get(actor, []))
        return httpx.Response(404, json={})

    def client(self) -> ApifyClient:
        http = httpx.Client(transport=httpx.MockTransport(self.handler))
        return ApifyClient("tok", http=http, poll_interval_s=0.0)


_X_ITEM = {
    "id": "111",
    "url": "https://x.com/user_a/status/111",
    "text": "白湯うまい",
    "likeCount": 31,
    "retweetCount": 2,
    "replyCount": 1,
    "createdAt": "2026-07-01",
    "lang": "ja",
    "author": {"userName": "user_a", "name": "Aさん"},
}


def test_search_posts_parses_and_costs() -> None:
    fake = _FakeApify()
    fake.items_by_actor[ACTOR_X_SEARCH] = [_X_ITEM]
    posts, cost = fake.client().search_posts("白湯", count=10, request_id="t")
    assert len(posts) == 1
    p = posts[0]
    assert p.post_id == "111"
    assert p.author_handle == "user_a"
    assert p.like_count == 31
    assert p.source_actor == ACTOR_X_SEARCH
    assert cost > 0


def test_search_posts_falls_back_on_empty() -> None:
    fake = _FakeApify()
    fake.items_by_actor[ACTOR_X_SEARCH] = []
    fake.items_by_actor[ACTOR_X_SEARCH_FALLBACK] = [dict(_X_ITEM, id="222")]
    posts, _ = fake.client().search_posts("白湯", count=10, request_id="t")
    assert [p.post_id for p in posts] == ["222"]
    assert any(ACTOR_X_SEARCH_FALLBACK in c for c in fake.calls)


def test_tier_error_surfaces_on_silent_zero() -> None:
    fake = _FakeApify()
    fake.items_by_actor[ACTOR_X_SEARCH] = []
    fake.status_message = "This feature requires a paid plan"
    with pytest.raises(ApifyError, match="APIFY_TIER"):
        fake.client().run_actor_sync(ACTOR_X_SEARCH, {"query": "x"}, max_items=5, request_id="t")


def test_deadline_aborts_run() -> None:
    fake = _FakeApify()
    fake.final_status = "RUNNING"  # 永遠に終わらない run
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    client = ApifyClient("tok", http=http, poll_interval_s=0.01)
    with pytest.raises(ApifyError, match="APIFY_TIMEOUT"):
        client.run_actor_sync(
            ACTOR_X_SEARCH, {"query": "x"}, max_items=5, deadline_s=0.001, request_id="t"
        )
    assert fake.aborted  # 課金停止の abort が呼ばれている


def test_verify_posts_maps_by_status_id() -> None:
    fake = _FakeApify()
    fake.items_by_actor["xtracto~x-post-detail-scraper"] = [_X_ITEM]
    result, _ = fake.client().verify_posts(
        ["https://x.com/user_a/status/111", "https://x.com/user_b/status/999"],
        request_id="t",
    )
    assert result["https://x.com/user_a/status/111"] is not None
    assert result["https://x.com/user_b/status/999"] is None  # 取得不可＝要再確認


def test_ig_search_parses_nested_top_posts() -> None:
    fake = _FakeApify()
    fake.items_by_actor[ACTOR_IG_SEARCH] = [
        {
            "topPosts": [
                {
                    "shortCode": "abc",
                    "url": "https://www.instagram.com/reel/abc/",
                    "caption": "セブン新作",
                    "ownerUsername": "gourmet_a",
                    "likesCount": 100,
                    "videoViewCount": 5000,
                    "productType": "clips",
                }
            ]
        }
    ]
    posts, _ = fake.client().ig_search("セブン", limit=10, request_id="t")
    assert posts[0].shortcode == "abc"
    assert posts[0].post_type == "reel"


def test_ig_search_hashtag_uses_hashtag_actor() -> None:
    fake = _FakeApify()
    fake.items_by_actor[ACTOR_IG_HASHTAG] = [
        {"shortCode": "xyz", "ownerUsername": "u", "likesCount": 1}
    ]
    posts, _ = fake.client().ig_search("#セブン", limit=10, surface="hashtag", request_id="t")
    assert posts[0].shortcode == "xyz"
    assert any(ACTOR_IG_HASHTAG in c for c in fake.calls)


def test_tiktok_comments_normalized() -> None:
    fake = _FakeApify()
    fake.items_by_actor["clockworks~tiktok-comments-scraper"] = [
        {"text": "美味しそう", "diggCount": 16, "uniqueId": "mayo"},
        {"text": "", "diggCount": 1},  # 空テキストはスキップ
    ]
    comments, _ = fake.client().tiktok_comments(
        "https://www.tiktok.com/@a/video/1", max_comments=10, request_id="t"
    )
    assert comments == [{"text": "美味しそう", "likes": 16, "author": "mayo"}]


class _DenyLedger:
    def check(self, provider: str, user_email: str, **kw: Any) -> list[str]:
        raise CostLimitExceededError("今月の枠を使い切りました")

    def record(self, *a: Any, **kw: Any) -> None:  # pragma: no cover
        raise AssertionError("record は呼ばれないはず")


class _OkLedger:
    def __init__(self) -> None:
        self.checked: list[tuple[str, float]] = []
        self.recorded: list[tuple[str, float, int]] = []

    def check(
        self, provider: str, user_email: str, *, est_cost_usd: float, request_id: str
    ) -> list[str]:
        self.checked.append((provider, est_cost_usd))
        return ["予算の80%を超えています"]

    def record(
        self, provider: str, user_email: str, *, cost_usd: float, units: int, request_id: str
    ) -> None:
        self.recorded.append((provider, cost_usd, units))


class _ReservationLedger:
    def __init__(self) -> None:
        self.reserved: list[float] = []
        self.settled: list[tuple[object, float, int]] = []
        self.token = object()

    def reserve(
        self, provider: str, user_email: str, *, est_cost_usd: float, request_id: str
    ) -> tuple[list[str], object]:
        self.reserved.append(est_cost_usd)
        return [], self.token

    def settle(self, reservation: object, *, cost_usd: float, units: int, request_id: str) -> None:
        self.settled.append((reservation, cost_usd, units))


def test_ledger_deny_blocks_before_run() -> None:
    fake = _FakeApify()
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    client = ApifyClient("tok", ledger=_DenyLedger(), http=http, poll_interval_s=0.0)
    with pytest.raises(CostLimitExceededError):
        client.run_actor_sync(ACTOR_X_SEARCH, {"query": "x"}, max_items=5, request_id="t")
    assert fake.calls == []  # fail-close: actor は起動していない＝課金ゼロ


def test_ledger_check_and_record_flow() -> None:
    fake = _FakeApify()
    fake.items_by_actor[ACTOR_X_SEARCH] = [_X_ITEM]
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    ledger = _OkLedger()
    client = ApifyClient("tok", ledger=ledger, http=http, poll_interval_s=0.0)
    res = client.run_actor_sync(ACTOR_X_SEARCH, {"query": "x"}, max_items=5, request_id="t")
    assert ledger.checked and ledger.checked[0][0] == "apify"
    assert ledger.recorded and ledger.recorded[0][2] == 1  # 実件数で記帳
    assert res.warnings == ["予算の80%を超えています"]


def test_ledger_reservation_is_settled_with_actual_cost() -> None:
    fake = _FakeApify()
    fake.items_by_actor[ACTOR_X_SEARCH] = [_X_ITEM]
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    ledger = _ReservationLedger()
    client = ApifyClient("tok", ledger=ledger, http=http, poll_interval_s=0.0)
    client.run_actor_sync(ACTOR_X_SEARCH, {"query": "x"}, max_items=5, request_id="t")
    assert ledger.reserved
    assert ledger.settled == [(ledger.token, 0.00025, 1)]


def test_unexpected_response_releases_reservation_no_phantom_leak() -> None:
    # Apify GW が data:null（非期待形状）を返しても、予約は必ず settle(解放)される
    # ＝幻の予約が台帳に残って予算を食う事故を防ぐ（self-review HIGH の回帰）。
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.startswith("/v2/acts/"):
            return httpx.Response(201, json={"data": None})  # 想定外形状
        return httpx.Response(404, json={})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    ledger = _ReservationLedger()
    client = ApifyClient("tok-secret", ledger=ledger, http=http, poll_interval_s=0.0)
    with pytest.raises(ApifyError) as ei:
        client.run_actor_sync(ACTOR_X_SEARCH, {"query": "x"}, max_items=5, request_id="t")
    # 予約は解放され（settle が呼ばれ）、幻の予約は残らない
    assert len(ledger.reserved) == 1
    assert len(ledger.settled) == 1 and ledger.settled[0][1] == 0.0  # cost=0 で解放
    # 生例外(URL/トークン含みうる)は漏らさず APIFY_ に丸める
    assert "tok-secret" not in str(ei.value)


def test_from_env_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APIFY_API_TOKEN", raising=False)
    with pytest.raises(ApifyError, match="APIFY_MISCONFIGURED"):
        ApifyClient.from_env()


def test_run_failed_status_raises() -> None:
    fake = _FakeApify()
    fake.final_status = "FAILED"
    with pytest.raises(ApifyError, match="APIFY_RUN_FAILED"):
        fake.client().run_actor_sync(ACTOR_X_SEARCH, {"query": "x"}, max_items=5, request_id="t")


def test_dataset_items_limit_param_sent() -> None:
    fake = _FakeApify()
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v2/datasets/"):
            captured.append(str(request.url.params.get("limit")))
        return fake.handler(request)

    fake.items_by_actor[ACTOR_X_SEARCH] = [_X_ITEM]
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = ApifyClient("tok", http=http, poll_interval_s=0.0)
    client.run_actor_sync(ACTOR_X_SEARCH, {"query": "x"}, max_items=7, request_id="t")
    assert captured == ["7"]  # サーバ側にも件数上限を掛ける


def test_parse_tolerates_alternate_field_names() -> None:
    fake = _FakeApify()
    fake.items_by_actor[ACTOR_X_SEARCH] = [
        {
            "id_str": "333",
            "full_text": "別形式の投稿",
            "favorite_count": "5",
            "user": {"screen_name": "alt_user"},
        }
    ]
    posts, _ = fake.client().search_posts("q", count=5, request_id="t")
    assert posts[0].post_id == "333"
    assert posts[0].author_handle == "alt_user"
    assert posts[0].like_count == 5
    assert posts[0].url == "https://x.com/alt_user/status/333"


def test_parse_scraper_one_real_keys_not_dropped() -> None:
    """第一候補 scraper_one の実キー（postText/screenName/favouriteCount/repostCount/postUrl）を
    取り落とさない＝全件 None ドロップ→毎回フォールバック の根因A回帰を防ぐ。"""
    fake = _FakeApify()
    fake.items_by_actor[ACTOR_X_SEARCH] = [
        {
            "postId": "555",
            "postUrl": "https://x.com/coffee_lover/status/555",
            "postText": "このコーヒー濃厚で最高",
            "favouriteCount": 128,
            "repostCount": 9,
            "replyCount": 3,
            "timestamp": 1750000000000,
            "author": {"screenName": "coffee_lover", "name": "コーヒー好き"},
        }
    ]
    posts, _ = fake.client().search_posts("コーヒー", count=5, request_id="t")
    assert len(posts) == 1  # postText を拾えず None ドロップ、ではない
    p = posts[0]
    assert p.author_handle == "coffee_lover"  # 「不明」にならない
    assert p.author_name == "コーヒー好き"
    assert p.text == "このコーヒー濃厚で最高"
    assert p.like_count == 128
    assert p.retweet_count == 9
    assert p.source_actor == ACTOR_X_SEARCH  # 第一候補で完結（フォールバックへ落ちない）


def test_parse_data_slayer_real_keys() -> None:
    """フォールバック data-slayer の実キー（top-level screen_name / favorites / user_info）で
    handle・いいねが空/0にならない＝author不明＋いいね0＋URL合成不発（根因B/C/D）を防ぐ。"""
    fake = _FakeApify()
    fake.items_by_actor[ACTOR_X_SEARCH] = []  # 第一候補は0件でフォールバックへ
    fake.items_by_actor[ACTOR_X_SEARCH_FALLBACK] = [
        {
            "tweet_id": "666",
            "text": "抹茶ミルク濃くて好き",
            "favorites": 42,
            "retweets": 4,
            "screen_name": "matcha_fan",
            "user_info": {"name": "抹茶ファン", "screen_name": "matcha_fan"},
            "created_at": "2026-07-10",
        }
    ]
    posts, _ = fake.client().search_posts("抹茶", count=5, request_id="t")
    assert len(posts) == 1
    p = posts[0]
    assert p.author_handle == "matcha_fan"  # top-level snake screen_name を拾う
    assert p.like_count == 42  # favorites を拾う
    assert p.url == "https://x.com/matcha_fan/status/666"  # handle+tweet_id からURL合成＝検証可能に


def test_parse_extracts_avatar_media_verified_for_recreation_card() -> None:
    """再現カード用に avatar/添付画像/青バッジ/view/quote を取得できる（P1）。"""
    fake = _FakeApify()
    fake.items_by_actor[ACTOR_X_SEARCH] = [
        {
            "postId": "777",
            "postUrl": "https://x.com/u/status/777",
            "postText": "写真つき投稿",
            "author": {
                "screenName": "u",
                "name": "U",
                "profileImageUrl": "https://pbs.example/av.jpg",
                "isVerified": True,
                "description": "コスメ好き｜淡色｜垢抜け",
            },
            "media": [
                {"mediaUrlHttps": "https://pbs.example/m1.jpg", "type": "photo"},
                {"mediaUrlHttps": "https://pbs.example/m2.jpg", "type": "photo"},
            ],
            "viewCount": 1000,
            "quoteCount": 2,
        }
    ]
    posts, _ = fake.client().search_posts("q", count=5, request_id="t")
    p = posts[0]
    assert p.author_avatar_url == "https://pbs.example/av.jpg"
    assert p.media_urls == ("https://pbs.example/m1.jpg", "https://pbs.example/m2.jpg")
    assert p.media_types == ("photo", "photo")
    assert p.is_verified is True
    assert p.view_count == 1000
    assert p.quote_count == 2
    assert p.author_bio == "コスメ好き｜淡色｜垢抜け"  # 界隈分類(Part4)の主入力


def test_parse_media_from_entities_nested_and_capped() -> None:
    """entities.media 形（apidojo/data-slayer）も拾い、最大4枚で打ち切る。"""
    from teamagent.adapters.apify_client import _extract_x_media

    d = {
        "entities": {
            "media": [{"media_url_https": f"https://pbs/{i}.jpg"} for i in range(6)],
        }
    }
    urls, types = _extract_x_media(d)
    assert len(urls) == 4  # 4枚で打ち切り
    assert urls[0] == "https://pbs/0.jpg"
    assert all(t == "photo" for t in types)  # type 欠落は既定 photo


def test_token_sent_via_header_not_url() -> None:
    # トークンはURLクエリに載せない（httpx例外メッセージ経由の漏えい防止）。
    fake = _FakeApify()
    seen_headers: list[str] = []
    seen_query_has_token: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers.get("Authorization", ""))
        seen_query_has_token.append("token" in dict(request.url.params))
        return fake.handler(request)

    fake.items_by_actor[ACTOR_X_SEARCH] = [_X_ITEM]
    http = httpx.Client(transport=httpx.MockTransport(handler))
    ApifyClient("tok-secret", http=http, poll_interval_s=0.0).search_posts(
        "q", count=5, request_id="t"
    )
    assert all(h == "Bearer tok-secret" for h in seen_headers)
    assert not any(seen_query_has_token)  # URLクエリにトークンは絶対に載らない


def test_timeout_records_partial_cost_and_no_token_in_error() -> None:
    fake = _FakeApify()
    fake.final_status = "RUNNING"
    ledger = _OkLedger()
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    client = ApifyClient("tok-secret", ledger=ledger, http=http, poll_interval_s=0.01)
    with pytest.raises(ApifyError) as ei:
        client.run_actor_sync(
            ACTOR_X_SEARCH,
            {"query": "x"},
            max_items=5,
            deadline_s=0.001,
            request_id="t",
        )
    # timeout でも概算コストを記帳（台帳が実支出から乖離しない）
    assert ledger.recorded and ledger.recorded[0][0] == "apify"
    # 例外文字列にトークンが混じらない
    assert "tok-secret" not in str(ei.value)


def test_search_posts_chain_shares_deadline_budget() -> None:
    # 第一候補が timeout してもフォールバックに残予算しか渡らない（合計が deadline を超えない）。
    fake = _FakeApify()
    budgets: list[int] = []
    orig = ApifyClient.run_actor_sync

    def spy(self: ApifyClient, actor_id: str, run_input: Any, **kw: Any) -> Any:
        budgets.append(kw.get("deadline_s"))
        return orig(self, actor_id, run_input, **kw)

    fake.items_by_actor[ACTOR_X_SEARCH_FALLBACK] = [_X_ITEM]  # 1本目は空→フォールバック
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    client = ApifyClient("tok", http=http, poll_interval_s=0.0)
    import types

    client.run_actor_sync = types.MethodType(spy, client)  # type: ignore[method-assign]
    client.search_posts("q", count=5, deadline_s=120, request_id="t")
    assert budgets[0] <= 120
    assert budgets[1] <= budgets[0]  # フォールバックは残予算のみ


def test_search_posts_period_builds_apidojo_input() -> None:
    fake = _FakeApify()
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.startswith("/v2/acts/"):
            bodies.append(json.loads(request.content.decode("utf-8")))
        return fake.handler(request)

    fake.items_by_actor["apidojo~tweet-scraper"] = [_X_ITEM]
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = ApifyClient("tok", http=http, poll_interval_s=0.0)
    posts, _ = client.search_posts_period(
        ["セブン 新商品"],
        start="2026-07-01",
        end="2026-07-07",
        minimum_favorites=3,
        max_items=50,
        request_id="t",
    )
    assert posts
    assert bodies[0]["searchTerms"] == ["セブン 新商品"]
    assert bodies[0]["start"] == "2026-07-01"
    assert bodies[0]["end"] == "2026-07-07"
    assert bodies[0]["minimumFavorites"] == 3


_APIDOJO_ITEM = {
    "id": "999",
    "url": "https://x.com/foo/status/999",
    "fullText": "apidojo経由のサンマルク投稿",
    "text": "apidojo経由のサンマルク投稿",
    "likeCount": 55,
    "retweetCount": 3,
    "createdAt": "Mon Jul 13 12:00:02 +0000 2026",
    "author": {
        "userName": "foo",
        "name": "Foo",
        "profilePicture": "https://pbs.twimg.com/profile_images/x_normal.jpg",
        "isVerified": True,
        "followers": 1000,
        "description": "ニュースサイト公式",
    },
    "entities": {
        "media": [{"media_url_https": "https://pbs.twimg.com/media/m.jpg", "type": "photo"}]
    },
}


def test_search_posts_uses_apidojo_first_and_parses_rich_fields() -> None:
    """第一候補を apidojo に切替。apidojo が返せば scraper_one/data-slayer は呼ばず、
    再現カード用の avatar/verified/media も取れる（レイテンシ根治＋データ充実）。"""
    fake = _FakeApify()
    fake.items_by_actor[ACTOR_X_PERIOD] = [_APIDOJO_ITEM]
    posts, cost = fake.client().search_posts("サンマルクカフェ", count=8, request_id="t")
    assert len(posts) == 1
    p = posts[0]
    assert p.source_actor == ACTOR_X_PERIOD  # apidojo で完結
    assert p.author_handle == "foo"
    assert p.author_name == "Foo"
    assert p.like_count == 55
    assert p.author_avatar_url == "https://pbs.twimg.com/profile_images/x_normal.jpg"
    assert p.is_verified is True
    assert p.media_urls == ("https://pbs.twimg.com/media/m.jpg",)
    assert cost > 0
    # apidojo が返したのでフォールバック actor は呼ばれない
    assert not any(f"/acts/{ACTOR_X_SEARCH}/" in c or ACTOR_X_SEARCH in c for c in fake.calls)


def test_search_posts_falls_back_to_scraper_one_when_apidojo_empty() -> None:
    """apidojo が 0 件なら scraper_one → data-slayer へ縮退する（後方互換）。"""
    fake = _FakeApify()
    fake.items_by_actor[ACTOR_X_PERIOD] = []
    fake.items_by_actor[ACTOR_X_SEARCH] = [dict(_X_ITEM, id="777")]
    posts, _ = fake.client().search_posts("q", count=8, request_id="t")
    assert [p.post_id for p in posts] == ["777"]
    assert posts[0].source_actor == ACTOR_X_SEARCH
