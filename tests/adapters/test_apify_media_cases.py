"""Profiles use the real reservation and HTTP paths, without paid calls."""

from io import BytesIO

import pytest
from PIL import Image, UnidentifiedImageError

from teamagent.adapters.apify_media_cases import ApifyCaseAssets
from teamagent.adapters.cost_guard import CostGuard, CostLimitExceededError
from tests.adapters.test_apify_tiktok_video import _FakeTikTokApify
from tests.adapters.test_cost_guard import _FakeDdb


def png() -> bytes:
    out = BytesIO()
    Image.new("RGB", (8, 8)).save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> CostGuard:
    import threading

    monkeypatch.setenv("COST_APIFY_MONTHLY_USD", "50")
    monkeypatch.setenv("COST_MEDIA_CASE_GEMINI_MONTHLY_USD", "15")
    monkeypatch.setenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", "example.com")
    monkeypatch.delenv("COST_APIFY_PER_CALL_USD", raising=False)
    monkeypatch.delenv("COST_PER_USER_MONTHLY_USD", raising=False)
    ddb = _FakeDdb()
    original_update = ddb.update_item
    lock = threading.Lock()

    def atomic_update(**kwargs):
        with lock:
            return original_update(**kwargs)

    ddb.update_item = atomic_update
    return CostGuard("test", ddb=ddb)


@pytest.mark.parametrize("date,cost", [(None, 1.201), ("2026-10-01", 1.601)])
def test_profiles_400_reserve_correct_rate_and_free_assets(ledger, date, cost) -> None:
    fake = _FakeTikTokApify({}, [{"id": str(n)} for n in range(400)])
    starts = []
    run = fake.client(ledger).tiktok_profiles(
        "trendsnap_official", oldest_date=date, on_run_started=starts.append
    )
    expected = {
        "profiles": ["trendsnap_official"],
        "resultsPerPage": 400,
        "profileSorting": "latest",
        "profileScrapeSections": ["videos"],
        "excludePinnedPosts": False,
        "shouldDownloadCovers": True,
        "shouldDownloadSlideshowImages": True,
        "downloadSubtitlesOptions": "DOWNLOAD_SUBTITLES",
        "shouldDownloadVideos": False,
    }
    if date:
        expected["oldestPostDateUnified"] = date
    assert fake.post_bodies == [expected]
    assert starts == ["run-1"] and len(run.items) == 400
    assert run.estimated_cost_usd == pytest.approx(cost)
    assert next(iter(ledger._ddb.rows.values()))["cost_micro"] == round(cost * 1_000_000)


def test_empty_run_still_charges_start(ledger) -> None:
    assert _FakeTikTokApify({}).client(ledger).tiktok_profiles("test").estimated_cost_usd == 0.001


@pytest.mark.parametrize("budget,fail", [("0.300", False), ("", False), ("50", True)])
def test_profile_budget_rejection_never_posts(monkeypatch, budget, fail) -> None:
    monkeypatch.setenv("COST_APIFY_MONTHLY_USD", budget)
    fake = _FakeTikTokApify({})
    with pytest.raises(CostLimitExceededError):
        fake.client(CostGuard("test", ddb=_FakeDdb(fail=fail))).tiktok_profiles("test")
    assert fake.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"handle": "https://tiktok.com/@a"},
        {"handle": "a", "max_items": 401},
        {"handle": "a", "oldest_date": "yesterday"},
    ],
)
def test_invalid_profile_inputs_do_not_fetch(kwargs) -> None:
    fake = _FakeTikTokApify({})
    with pytest.raises(ValueError):
        fake.client().tiktok_profiles(**kwargs)
    assert fake.calls == []


def test_resume_reads_existing_run_without_reserving_or_starting() -> None:
    fake = _FakeTikTokApify({}, [{"id": "123"}])
    run = fake.client().resume_tiktok_profile_run("run-1")
    assert run.items == [{"id": "123"}] and run.estimated_cost_usd == 0
    assert not fake.post_bodies


def test_assets_only_fetch_same_run_kvs_and_tolerate_unknown_fields(ledger) -> None:
    records = {
        "cover-123.png": png(),
        "cover-1234.png": png(),
        "slide-123-2.png": png(),
        "slide-123-1.png": png(),
        "subtitle-123.vtt": b"WEBVTT\n00:00:01.000 --> 00:00:02.000\nbrand text",
    }
    fake = _FakeTikTokApify(records)
    client = fake.client(ledger)
    assets = ApifyCaseAssets(client, client.tiktok_profiles("test"))
    refs = assets.for_post({"videoMeta": {"coverUrl": "https://cdn.tiktok.com/image.jpg"}}, "123")
    assert refs.cover_key == "cover-123.png"
    assert refs.slide_keys == ("slide-123-1.png", "slide-123-2.png")
    assert "brand text" in refs.subtitle and "WEBVTT" not in refs.subtitle
    assert assets.image(refs.cover_key) == (png(), "image/png")
    assert assets.for_post({"unpublishedCommerceField": True}, "999").cover_key is None
    for ref in (
        "https://cdn.tiktok.com/a",
        "https://api.apify.com.evil/a",
        "https://api.apify.com/v2/key-value-stores/other/records/a",
        "https://api.apify.com/v2/key-value-stores/kvs1/records/..%2Fa",
    ):
        assert assets._record_key(ref) is None
    assert all(call.split()[1].startswith("/v2/") for call in fake.calls)


def test_malformed_actor_dataset_keeps_reservation_and_does_not_look_empty(ledger) -> None:
    import httpx

    from teamagent.adapters.apify_client import ApifyError

    fake = _FakeTikTokApify({})
    original = fake.handler

    def invalid_dataset(request):
        if "/datasets/" in request.url.path:
            return httpx.Response(200, json={"error": "dataset unavailable"})
        return original(request)

    fake.handler = invalid_dataset
    with pytest.raises(ApifyError, match="APIFY_DATASET_INVALID"):
        fake.client(ledger).tiktok_profiles("test")
    assert next(iter(ledger._ddb.rows.values()))["cost_micro"] == 1201000


def test_attached_inline_subtitles_and_broken_images_are_optional(ledger) -> None:
    fake = _FakeTikTokApify({"cover-1.png": b"not an image"})
    client = fake.client(ledger)
    assets = ApifyCaseAssets(client, client.tiktok_profiles("test"))
    refs = assets.for_post({"subtitles": [{"text": "国内旅行キャンペーン"}]}, "1")
    assert refs.subtitle == "国内旅行キャンペーン"
    with pytest.raises(UnidentifiedImageError):
        assets.image(refs.cover_key)
