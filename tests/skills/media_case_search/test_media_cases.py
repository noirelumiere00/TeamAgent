"""Run realistic partial batches through HTTP, budget, model, and job boundaries."""

from __future__ import annotations

import copy
import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from teamagent.adapters.apify_media_cases import ApifyCaseAssets
from teamagent.adapters.cost_guard import CostLimitExceededError
from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.base import SkillContext
from teamagent.skills.media_case_search.distill import CaseDistiller, normalize_post
from teamagent.skills.media_case_search.schema import MediaCaseInput
from teamagent.skills.media_case_search.skill import MediaCaseSearchSkill, accounts, query_filters
from tests.adapters.test_apify_media_cases import png
from tests.adapters.test_apify_tiktok_video import _FakeTikTokApify

HANDLE = "trendsnap_official"
CTX = SkillContext(metadata={"identity_verified": True, "user_email": "member@example.com"})


def raw_post(number=1, **extra):
    post_id = str(8000000000000000000 + number)
    return {
        "id": post_id,
        "webVideoUrl": f"https://www.tiktok.com/@{HANDLE}/video/{post_id}",
        "text": f"テスト旅行社の国内旅行キャンペーン {number}",
        "hashtags": [{"name": "旅行"}],
        "createTime": 1793100000,
        "playCount": 12000,
        "diggCount": 120,
        **extra,
    }


@pytest.mark.parametrize("hour, day", [(14, "2026-10-28"), (15, "2026-10-29")])
def test_posted_on_uses_japan_date(hour, day):
    stamp = datetime(2026, 10, 28, hour, tzinfo=UTC)
    post = normalize_post(raw_post(createTime=int(stamp.timestamp())), HANDLE)
    assert post["posted_at"] == stamp.isoformat()
    assert post["posted_on"] == day


def extraction(*, brand="テスト旅行社", roundup=False, evidence=None):
    return {
        "is_roundup": roundup,
        "contains_personal_names": False,
        "items": [
            {
                "brand": brand,
                "product": "国内旅",
                "campaign": "国内旅行キャンペーン",
                "category": "旅行",
                "campaign_types": ["campaign"],
                "geography": "domestic",
                "summary": "国内旅行のキャンペーンを紹介。",
                "evidence": evidence or [{"source": "caption", "excerpt": "国内旅行キャンペーン"}],
            }
        ],
        "pr_evidence": [],
    }


class FakeGemini:
    model_id = "gemini-2.5-flash"

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []

    def _reply(self, prompt, images, cap):
        assert cap == 0.03
        self.calls.append((prompt, len(images)))
        caption = json.loads(prompt.split("投稿データ: ", 1)[1])["caption"]
        result = self.replies.pop(0) if self.replies else extraction()
        text = "{invalid JSON" if caption.endswith(" 5") else json.dumps(result, ensure_ascii=False)
        return SimpleNamespace(text=text, cost_usd=0.0001)

    def generate_text(self, prompt, request_id, *, json_mode, max_cost_usd):
        assert json_mode
        return self._reply(prompt, [], max_cost_usd)

    def analyze_images_bytes(self, images, prompt, request_id, *, max_cost_usd):
        return self._reply(prompt, images, max_cost_usd)


class MemoryStore:
    def __init__(self):
        now = datetime.now(UTC).isoformat()
        self.states = {
            h: {"last_success": now, "initialized": True} for h in accounts() if h != HANDLE
        }
        self.posts = {}
        self.lock = threading.Lock()

    def state(self, handle):
        return copy.deepcopy(self.states.get(handle, {}))

    def save_state(self, handle, state):
        self.states[handle] = copy.deepcopy(state)

    def post(self, post_id):
        return copy.deepcopy(self.posts.get(post_id, {}))

    def save_post(self, metadata, text, request_id):
        self.posts[metadata["post_id"]] = copy.deepcopy(metadata)

    def update_metrics(self, post_id, metrics, observed_at):
        self.posts[post_id].update(metrics=metrics, metrics_observed_at=observed_at)

    @contextmanager
    def worker_lock(self):
        acquired = self.lock.acquire(blocking=False)
        try:
            yield (lambda: None) if acquired else None
        finally:
            if acquired:
                self.lock.release()

    def coverage(self, handles, identity):
        return sum(p["account_handle"] in handles for p in self.posts.values())

    def search(self, query, filters, identity, limit):
        # SQL contract is tested separately; fake returns the stored candidates.
        return [{**p, "score": 0.8} for p in self.posts.values()]


def make_skill(ledger, fake=None, *, store=None, model=None, launch=None, jobs=None):
    return MediaCaseSearchSkill(
        store=store or MemoryStore(),
        jobs=jobs or ProposalJobStore(table_name="", memory={}),
        apify=(fake or _FakeTikTokApify({})).client(ledger),
        distiller=CaseDistiller(model or FakeGemini(), ledger),
        launch=launch or (lambda fn: fn()),
    )


def test_initial_import_partial_json_missing_thumbnail_and_saved_search(ledger):
    raw = [raw_post(n) for n in range(150)]
    fake = _FakeTikTokApify(
        {f"cover-{p['id']}.png": png() for i, p in enumerate(raw) if i != 3}, raw
    )
    model = FakeGemini()
    skill = make_skill(ledger, fake, model=model)
    before = skill.run(MediaCaseInput(query="国内 旅行 CP"), CTX)
    assert before.status == "ready" and not fake.calls
    result = skill.ingest_initial([HANDLE])
    assert result["posts"] == 150 and result["failed"] == 1
    assert len(fake.post_bodies) == 1 and len(skill.store.posts) == 149
    assert skill.store.state(HANDLE)["failed_ids"] == [raw[5]["id"]]
    assert skill.store.post(raw[3]["id"])["image_count"] == 0
    assert len(model.calls) == 150
    assert [
        json.loads(prompt.split("投稿データ: ")[1])["caption"]
        for prompt, image_count in model.calls
        if image_count == 0
    ] == [raw[3]["text"]]
    ready = skill.run(MediaCaseInput(query="国内 旅行 CP"), CTX)
    assert ready.status == "ready" and len(fake.post_bodies) == 1
    assert "149 本" in ready.message and "国内旅行キャンペーン" in ready.message
    assert "PR 不明（参考）" in ready.message and "PR確定" not in ready.message
    assert "選んで" not in ready.message and "一部未完了" in ready.message


def analyze(ledger, *, raw=None, records=None, replies=None, previous=None):
    raw = raw or raw_post()
    fake = _FakeTikTokApify(records or {}, [raw])
    client = fake.client(ledger)
    assets = ApifyCaseAssets(client, client.tiktok_profiles(HANDLE))
    model = FakeGemini(replies)
    result = CaseDistiller(model, ledger).analyze(
        normalize_post(raw, HANDLE), raw, assets, "test", previous or {}
    )
    return result, model


def test_only_unknown_photo_roundup_gets_4_4_2_extra_slides(ledger):
    raw = raw_post(isSlideshow=True, slideshowImageLinks=["unused"])
    records = {f"cover-{raw['id']}.png": png()}
    records.update({f"slide-{raw['id']}-{n}.png": png() for n in range(1, 15)})
    (metadata, _, _), model = analyze(
        ledger, raw=raw, records=records, replies=[extraction(brand=None, roundup=True)]
    )
    assert [count for _, count in model.calls] == [1, 4, 4, 2]
    assert metadata["image_count"] == 11
    assert metadata["items"][0]["brand"] == "テスト旅行社"


@pytest.mark.parametrize(
    "photo,roundup,brand", [(False, True, None), (True, True, "既知"), (True, False, None)]
)
def test_no_video_or_unnecessary_slide_analysis(ledger, photo, roundup, brand):
    raw = raw_post(**({"isSlideshow": True, "slideshowImageLinks": ["unused"]} if photo else {}))
    records = {f"cover-{raw['id']}.png": png(), f"slide-{raw['id']}-1.png": png()}
    (_, _, _), model = analyze(
        ledger, raw=raw, records=records, replies=[extraction(brand=brand, roundup=roundup)]
    )
    assert [count for _, count in model.calls] == [1]


@pytest.mark.parametrize(
    "fields,status",
    [
        ({}, "unknown"),
        ({"isAd": True}, "possible"),
        ({"isSponsored": True}, "disclosed"),
        ({"isSponsored": False, "hashtags": [{"name": "PR"}]}, "disclosed"),
        ({"hashtags": [{"name": "PR"}]}, "disclosed"),
        ({"text": "国内旅行キャンペーン #PR 広告ではありません"}, "unknown"),
        ({"text": "国内旅行キャンペーン 広告事例を紹介"}, "unknown"),
    ],
)
def test_pr_requires_disclosure_not_commerce_or_generic_ad_word(ledger, fields, status):
    (metadata, _, _), _ = analyze(ledger, raw=raw_post(**fields))
    assert metadata["pr_status"] == status


def test_unchanged_input_reuses_distillation_even_without_cover(ledger):
    (metadata, _, _), _ = analyze(ledger)
    (second, text, cost), model = analyze(ledger, raw=raw_post(playCount=25000), previous=metadata)
    assert second is None and not text and cost == 0 and model.calls == []


def test_personal_names_and_unfounded_evidence_are_not_saved(ledger):
    personal = {**extraction(), "contains_personal_names": True}
    with pytest.raises(ValueError, match="personal names"):
        analyze(ledger, replies=[personal])
    (metadata, text, _), _ = analyze(
        ledger, replies=[extraction(evidence=[{"source": "caption", "excerpt": "存在しない文章"}])]
    )
    assert metadata["items"] == [] and text == ""


def test_budget_failure_clears_unstarted_intent(ledger, monkeypatch):
    monkeypatch.setenv("COST_APIFY_MONTHLY_USD", "0.1")
    fake = _FakeTikTokApify({})
    skill = make_skill(ledger, fake)
    with pytest.raises(CostLimitExceededError):
        skill.ingest_initial([HANDLE])
    assert not skill.store.state(HANDLE)["intent"] and not fake.calls


def test_model_reservation_refusal_does_not_call_gemini(ledger, monkeypatch):
    monkeypatch.setenv("COST_MEDIA_CASE_GEMINI_MONTHLY_USD", "0.02")
    with pytest.raises(CostLimitExceededError):
        analyze(ledger)


def test_unknown_start_blocks_other_accounts_and_never_reposts(ledger):
    store = MemoryStore()
    store.states[HANDLE] = {"intent": True}
    fake = _FakeTikTokApify({})
    skill = make_skill(ledger, fake, store=store)
    with pytest.raises(RuntimeError, match="manual reconciliation"):
        skill.ingest_initial([HANDLE])
    assert store.state(HANDLE)["intent"] and not fake.post_bodies


def test_resume_existing_run_and_overlap_date_watermark(ledger):
    store = MemoryStore()
    fetch_start = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    store.states[HANDLE] = {"intent": True, "run_id": "run-1", "fetch_started_at": fetch_start}
    fake = _FakeTikTokApify({}, [raw_post()])
    skill = make_skill(ledger, fake, store=store)
    skill.ingest_initial([HANDLE])
    assert not fake.post_bodies and store.state(HANDLE)["watermark"] == fetch_start
    store.states[HANDLE]["last_success"] = fetch_start
    skill.run(MediaCaseInput(query="旅行 CP"), CTX)
    expected = (datetime.fromisoformat(fetch_start) - timedelta(days=2)).date().isoformat()
    assert fake.post_bodies[0]["oldestPostDateUnified"] == expected


def test_parallel_replicas_cannot_start_two_runs(ledger):
    started, release = threading.Event(), threading.Event()
    fake = _FakeTikTokApify({}, [raw_post()])
    original = fake.handler
    active = 0
    peak = 0

    def blocking(request):
        nonlocal active, peak
        if request.method == "POST" and "/acts/" in request.url.path:
            active += 1
            peak = max(peak, active)
            started.set()
            assert release.wait(5)
        response = original(request)
        if request.method == "GET" and "/actor-runs/" in request.url.path:
            active -= 1
        return response

    fake.handler = blocking
    store, jobs, threads = MemoryStore(), ProposalJobStore(table_name="", memory={}), []

    def launch(fn):
        thread = threading.Thread(target=fn)
        threads.append(thread)
        thread.start()

    store.states[HANDLE] = {
        "initialized": True,
        "last_success": (datetime.now(UTC) - timedelta(days=8)).isoformat(),
    }
    first = make_skill(ledger, fake, store=store, jobs=jobs, launch=launch)
    second = make_skill(ledger, fake, store=store, jobs=jobs, launch=launch)
    try:
        output = first.run(MediaCaseInput(query="旅行 CP"), CTX)
        assert started.wait(5)
        other = second.run(MediaCaseInput(query="食品"), CTX)
        assert other.deduplicated and not other.job_id
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    assert peak == 1 and len(fake.post_bodies) == 1
    assert jobs.get_job(output.job_id)["status"] == "done"


def test_tool_description_requires_explicit_media_scope():
    description = MediaCaseSearchSkill.description
    for phrase in (
        "トレンドメディア／自社メディアと明示された時だけ",
        "@付き・無しとも可",
        "枕詞なしの一般事例はsearchのみ",
        "本ツール使用時はsearchを併用しない",
        "queryは枕詞と直後の「での／で／の」を除いた残り",
        "トレンドメディア 国内 旅行 CP → query=国内 旅行 CP",
        "「動画出して」「まとめて」は共に案件単位の回答",
        "いちばん伸びた1本の投稿リンク",
        "回答末尾は収録範囲と次の一手の2行",
    ):
        assert phrase in description
    assert description.split("登録handle: ")[1].split(", ") == list(accounts())


@pytest.mark.parametrize("has_posts", [True, False])
@pytest.mark.parametrize("refresh", [True, False])
def test_search_ends_with_coverage_and_one_next_action(ledger, has_posts, refresh):
    from tests.skills.media_case_search.test_campaigns import post

    store = MemoryStore()
    if has_posts:
        store.posts = {str(i): {**post(i), "account_handle": HANDLE} for i in range(3)}
    if refresh:
        store.states[HANDLE] = {
            "initialized": True,
            "last_success": (datetime.now(UTC) - timedelta(days=8)).isoformat(),
        }
    skill = make_skill(ledger, store=store, launch=lambda fn: None)
    input = MediaCaseInput(query="国内旅行 CP")
    output = skill.run(input, CTX)
    assert output.status == ("queued" if refresh else "ready")
    message = output.message
    coverage, next_action = message.splitlines()[-2:]
    assert coverage.startswith(f"※ NewsTV の 10 アカウント・{3 if has_posts else 0} 投稿から")
    assert ("数値は 2026-10-28 の取得時点。" in coverage) == has_posts
    assert next_action == "施設名やブランド名でも事例を探せます。"
    assert message.count("事例を探せます。") == 1
    assert "最大はいちばん伸びた投稿の URL を載せています。" not in message
    assert "未特定" not in message
    if refresh:
        running = skill.run(input, CTX)
        assert running.status == "running"
        assert running.message.splitlines()[-2:] == [coverage, next_action]
        skill.jobs.mark_running(output.job_id)
        skill.jobs.mark_done(output.job_id, "{}")
        status, completed = skill.poll(output.job_id, CTX)
        assert status == "done" and "投稿の追加確認が完了しました。" in completed
        assert completed.splitlines()[-2:] == [coverage, next_action]


def test_domestic_travel_cp_filters_and_no_unverified_access(ledger):
    query, filters = query_filters(MediaCaseInput(query="2026年 国内 旅行 ＣＰ PRのみ"))
    assert "キャンペーン" in query and "コスト" not in query
    assert filters["campaign_type"] == "campaign" and filters["geography"] == "domestic"
    assert filters["categories"] == ["旅行", "宿泊", "交通"] and filters["pr_only"]
    assert filters["posted_from"] == "2026-01-01"
    with pytest.raises(PermissionError):
        make_skill(ledger).run(MediaCaseInput(query="旅行"), SkillContext())
    with pytest.raises(ValidationError):
        MediaCaseInput(query=" ")
    with pytest.raises(ValueError):
        normalize_post(raw_post(webVideoUrl="https://evil.example/@a/video/1"), HANDLE)


def test_capped_delta_keeps_gap_visible_without_daily_paid_loop(ledger):
    store = MemoryStore()
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    store.states[HANDLE] = {"last_success": old, "watermark": old, "initialized": True}
    raw = [raw_post(n, text="国内旅行キャンペーン") for n in range(400)]
    fake = _FakeTikTokApify({}, raw)
    skill = make_skill(ledger, fake, store=store)
    skill.run(MediaCaseInput(query="旅行 CP"), CTX)
    state = store.state(HANDLE)
    assert state["gap"] and state["watermark"] > old and not state["failed_ids"]
    state["last_attempt"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    store.save_state(HANDLE, state)
    result = skill.run(MediaCaseInput(query="旅行 CP"), CTX)
    assert result.status == "ready" and len(fake.post_bodies) == 1
    assert "一部未完了" in result.message


def test_missing_company_acl_stops_before_paid_work(ledger, monkeypatch):
    monkeypatch.delenv("TEAMAGENT_SHARED_COMPANY_DOMAINS")
    fake = _FakeTikTokApify({})
    skill = make_skill(ledger, fake)
    with pytest.raises(ValueError, match="sharing scope"):
        skill.ingest_initial([HANDLE])
    assert not fake.calls
