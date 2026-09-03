"""apify_client の TikTok 動画実体取得（clockworks/tiktok-scraper + video download add-on）。

httpx MockTransport で Apify REST v2（run 起動 → ポーリング → dataset → KVS keys → KVS record）を
再現し、実課金ゼロで 0本/1本/N本/例外/上限超過/allowlist を固定する。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from teamagent.adapters.apify_client import (
    ACTOR_TIKTOK_VIDEO,
    ApifyClient,
    ApifyError,
    tiktok_post_url_allowed,
)

# ISO BMFF: 先頭 box の type が ftyp（実測 2026-09-02: ftypisom）
_MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 40
_URL1 = "https://www.tiktok.com/@shinjuku_g/video/7634863996553776404"
_URL2 = "https://www.tiktok.com/@other/video/7000000000000000002"
_URL3 = "https://www.tiktok.com/@other/video/7000000000000000003"
_KEY1 = "video-shinjuku_g-20260501101145-7634863996553776404.mp4"
_KEY2 = "video-other-20260501101146-7000000000000000002.mp4"


class _FakeTikTokApify:
    """clockworks/tiktok-scraper の run + 既定 KVS を最小再現する。"""

    def __init__(self, records: dict[str, bytes], items: list[dict[str, Any]] | None = None):
        self.records = records
        self.items = items if items is not None else []
        self.final_status = "SUCCEEDED"
        self.calls: list[str] = []
        self.post_bodies: list[dict[str, Any]] = []
        self.record_auth: list[str] = []
        self.record_query_has_token: list[bool] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        run = {
            "id": "run-1",
            "status": self.final_status,
            "statusMessage": "",
            "defaultDatasetId": "ds1",
            "defaultKeyValueStoreId": "kvs1",
        }
        if request.method == "POST" and path.startswith("/v2/acts/"):
            self.post_bodies.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(201, json={"data": {**run, "status": "READY"}})
        if request.method == "GET" and path.startswith("/v2/actor-runs/"):
            return httpx.Response(200, json={"data": run})
        if request.method == "POST" and "/abort" in path:
            return httpx.Response(200, json={"data": {}})
        if request.method == "GET" and path.startswith("/v2/datasets/"):
            return httpx.Response(200, json=self.items)
        if request.method == "GET" and path == "/v2/key-value-stores/kvs1/keys":
            items = [{"key": key, "size": len(body)} for key, body in self.records.items()]
            return httpx.Response(200, json={"data": {"items": items, "count": len(items)}})
        if request.method == "GET" and path.startswith("/v2/key-value-stores/kvs1/records/"):
            key = path.rsplit("/", 1)[1]
            self.record_auth.append(request.headers.get("Authorization", ""))
            self.record_query_has_token.append("token" in dict(request.url.params))
            body = self.records.get(key)
            if body is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, content=body, headers={"content-type": "video/mp4"})
        return httpx.Response(404, json={})

    def client(self, ledger: Any | None = None) -> ApifyClient:
        http = httpx.Client(transport=httpx.MockTransport(self.handler))
        return ApifyClient("tok-secret", ledger=ledger, http=http, poll_interval_s=0.0)


def _items(*urls: str) -> list[dict[str, Any]]:
    return [{"id": url.rsplit("/", 1)[1], "webVideoUrl": url} for url in urls]


# ---------------------------------------------------------------------------
# allowlist（tiktok.com 配下の canonical HTTPS だけ）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://www.tiktok.com/@a/video/1", True),
        ("https://tiktok.com/@a/video/1", True),
        ("https://vt.tiktok.com/ZSabc/", True),
        ("http://www.tiktok.com/@a/video/1", False),  # 平文
        ("https://www.tiktok.com:8443/@a/video/1", False),  # 非標準ポート
        ("https://user:pw@www.tiktok.com/@a/video/1", False),  # 資格情報
        ("https://www.tiktok.com/@a/video/1#frag", False),  # fragment
        ("https://www.tiktok.com.evil.example/@a/video/1", False),  # suffix 偽装
        ("https://eviltiktok.com/@a/video/1", False),
        ("https://www.instagram.com/reel/abc/", False),
        ("https://www.youtube.com/watch?v=x", False),
        ("", False),
    ],
)
def test_tiktok_post_url_allowed_matrix(url: str, allowed: bool) -> None:
    assert tiktok_post_url_allowed(url) is allowed


def test_download_rejects_non_tiktok_url_before_any_call() -> None:
    fake = _FakeTikTokApify({})
    with pytest.raises(ApifyError, match="APIFY_URL_NOT_ALLOWED"):
        fake.client().tiktok_download_videos(
            [_URL1, "https://www.instagram.com/reel/abc/"], max_videos=5, request_id="t"
        )
    assert fake.calls == []  # fail-close: run は起動していない＝課金ゼロ


# ---------------------------------------------------------------------------
# 0本 / 1本 / N本 / 上限 / 例外
# ---------------------------------------------------------------------------


def test_download_zero_when_kvs_has_no_video_record() -> None:
    fake = _FakeTikTokApify({}, items=_items(_URL1))
    videos, cost = fake.client().tiktok_download_videos([_URL1], max_videos=5, request_id="t")
    assert videos == []
    assert cost == pytest.approx(0.004)  # dataset 1件ぶんは課金される（result + add-on）
    assert not any("/records/" in call for call in fake.calls)


def test_download_one_matches_record_by_video_id_suffix() -> None:
    fake = _FakeTikTokApify({_KEY1: _MP4, "cover-x.jpg": b"\xff\xd8"}, items=_items(_URL1))
    videos, cost = fake.client().tiktok_download_videos([_URL1], max_videos=5, request_id="t")
    assert len(videos) == 1
    video = videos[0]
    assert video.post_url == _URL1
    assert video.video_id == "7634863996553776404"
    assert video.kvs_key == _KEY1
    assert video.body == _MP4
    assert video.content_type == "video/mp4"
    assert cost == pytest.approx(0.004)
    body = fake.post_bodies[0]
    assert body["postURLs"] == [_URL1]
    assert body["shouldDownloadVideos"] is True
    assert body["shouldDownloadCovers"] is False
    assert f"POST /v2/acts/{ACTOR_TIKTOK_VIDEO}/runs" in fake.calls


def test_download_n_keeps_request_order_and_skips_missing_records() -> None:
    fake = _FakeTikTokApify({_KEY2: _MP4, _KEY1: _MP4}, items=_items(_URL1, _URL2, _URL3))
    videos, cost = fake.client().tiktok_download_videos(
        [_URL1, _URL2, _URL3], max_videos=5, request_id="t"
    )
    assert [v.post_url for v in videos] == [_URL1, _URL2]  # URL3 は record 無し＝黙って捏造しない
    assert cost == pytest.approx(0.012)


def test_download_caps_post_urls_at_max_videos_and_dedups() -> None:
    fake = _FakeTikTokApify({_KEY1: _MP4, _KEY2: _MP4}, items=_items(_URL1, _URL2))
    videos, _ = fake.client().tiktok_download_videos(
        [_URL1, _URL1, _URL2, _URL3], max_videos=2, request_id="t"
    )
    assert fake.post_bodies[0]["postURLs"] == [_URL1, _URL2]  # 重複除去 + 上限2本
    assert len(videos) == 2


def test_download_max_videos_zero_is_noop() -> None:
    fake = _FakeTikTokApify({_KEY1: _MP4})
    assert fake.client().tiktok_download_videos([_URL1], max_videos=0, request_id="t") == ([], 0.0)
    assert fake.calls == []


def test_download_run_failure_raises_apify_error() -> None:
    fake = _FakeTikTokApify({_KEY1: _MP4})
    fake.final_status = "FAILED"
    with pytest.raises(ApifyError, match="APIFY_RUN_FAILED"):
        fake.client().tiktok_download_videos([_URL1], max_videos=5, request_id="t")


def test_download_skips_oversized_record_and_non_mp4() -> None:
    fake = _FakeTikTokApify(
        {_KEY1: _MP4, _KEY2: b"<html>not a video</html>" + b"\x00" * 40},
        items=_items(_URL1, _URL2),
    )
    videos, _ = fake.client().tiktok_download_videos(
        [_URL1, _URL2], max_videos=5, max_bytes_per_video=16, request_id="t"
    )
    assert videos == []  # 16B 上限超過（URL1）・非 mp4（URL2）は採用しない
    videos, _ = fake.client().tiktok_download_videos([_URL1, _URL2], max_videos=5, request_id="t")
    assert [v.post_url for v in videos] == [_URL1]


def test_get_kvs_record_enforces_size_bound_and_key_shape() -> None:
    fake = _FakeTikTokApify({_KEY1: _MP4})
    client = fake.client()
    with pytest.raises(ApifyError, match="APIFY_RECORD_TOO_LARGE"):
        client.get_kvs_record("kvs1", _KEY1, max_bytes=8)
    with pytest.raises(ApifyError, match="APIFY_KVS_KEY_INVALID"):
        client.get_kvs_record("kvs1", "../etc/passwd", max_bytes=1024)
    with pytest.raises(ApifyError, match="APIFY_KVS_ID_INVALID"):
        client.get_kvs_record("kvs 1", _KEY1, max_bytes=1024)
    with pytest.raises(ApifyError, match="APIFY_HTTP"):
        client.get_kvs_record("kvs1", "video-missing.mp4", max_bytes=1024)
    assert client.get_kvs_record("kvs1", _KEY1, max_bytes=1024) == _MP4


def test_record_get_sends_token_in_header_not_query() -> None:
    fake = _FakeTikTokApify({_KEY1: _MP4}, items=_items(_URL1))
    fake.client().tiktok_download_videos([_URL1], max_videos=1, request_id="t")
    assert fake.record_auth == ["Bearer tok-secret"]
    assert fake.record_query_has_token == [False]


def test_download_without_kvs_id_returns_empty() -> None:
    class _NoKvs(_FakeTikTokApify):
        def handler(self, request: httpx.Request) -> httpx.Response:
            resp = super().handler(request)
            if request.url.path.startswith(("/v2/acts/", "/v2/actor-runs/")):
                data = dict(resp.json()["data"])
                data.pop("defaultKeyValueStoreId")
                return httpx.Response(resp.status_code, json={"data": data})
            return resp

    fake = _NoKvs({_KEY1: _MP4}, items=_items(_URL1))
    videos, _ = fake.client().tiktok_download_videos([_URL1], max_videos=1, request_id="t")
    assert videos == []
    assert not any("/key-value-stores/" in call for call in fake.calls)


class _ReservationLedger:
    def __init__(self) -> None:
        self.reserved: list[float] = []
        self.settled: list[tuple[float, int]] = []

    def reserve(
        self, provider: str, user_email: str, *, est_cost_usd: float, request_id: str
    ) -> tuple[list[str], object]:
        self.reserved.append(est_cost_usd)
        return [], object()

    def settle(self, reservation: object, *, cost_usd: float, units: int, request_id: str) -> None:
        self.settled.append((cost_usd, units))


def test_download_books_cost_guard_with_video_unit_price() -> None:
    fake = _FakeTikTokApify({_KEY1: _MP4, _KEY2: _MP4}, items=_items(_URL1, _URL2))
    ledger = _ReservationLedger()
    fake.client(ledger=ledger).tiktok_download_videos(
        [_URL1, _URL2, _URL3], max_videos=3, request_id="t"
    )
    assert ledger.reserved == [pytest.approx(0.012)]  # 3本 × $0.004 を先に原子予約
    assert ledger.settled == [(pytest.approx(0.008), 2)]  # dataset 実件数で精算
