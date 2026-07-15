"""x_research 4スキルの単体テスト（Apify/Bedrock/store をモック・AWS/課金ゼロ）。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from teamagent.adapters.apify_client import ApifyError, XPost
from teamagent.adapters.cost_guard import CostLimitExceededError
from teamagent.skills.base import SkillContext
from teamagent.skills.x_research.schema import (
    XBuzzMeasureInput,
    XBuzzMeasureStatusInput,
    XNeedsMiningInput,
    XPostCard,
    XVoiceSearchInput,
)
from teamagent.skills.x_research.skill import (
    XBuzzMeasureSkill,
    XBuzzMeasureStatusSkill,
    XNeedsMiningSkill,
    XVoiceSearchSkill,
)


def _post(pid: str, text: str = "白湯うまい", likes: int = 10) -> XPost:
    return XPost(
        post_id=pid,
        url=f"https://x.com/u{pid}/status/{pid}",
        author_handle=f"u{pid}",
        author_name="",
        text=text,
        like_count=likes,
        retweet_count=0,
        reply_count=0,
        created_at="2026-07-01",
        lang="ja",
        source_actor="scraper_one~x-posts-search",
    )


class _FakeApify:
    def __init__(self, posts: list[XPost] | None = None, *, verify_missing: set[str] | None = None):
        self.posts = posts or []
        self.verify_missing = verify_missing or set()
        self.search_calls: list[str] = []
        self.verify_calls: list[list[str]] = []

    def search_posts(self, query: str, **kw: Any) -> tuple[list[XPost], float]:
        self.search_calls.append(query)
        return list(self.posts), 0.01

    def search_posts_period(self, terms: list[str], **kw: Any) -> tuple[list[XPost], float]:
        return list(self.posts), 0.01

    def verify_posts(self, urls: list[str], **kw: Any) -> tuple[dict[str, XPost | None], float]:
        self.verify_calls.append(urls)
        out: dict[str, XPost | None] = {}
        for u in urls:
            pid = u.rsplit("/", 1)[-1]
            hit = next((p for p in self.posts if p.post_id == pid), None)
            out[u] = None if pid in self.verify_missing else hit
        return out, 0.005


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.text = text
        self.usage = type("U", (), {"cost_usd": 0.001})()


class _FakeBedrock:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0

    def converse(self, **kw: Any) -> _FakeResp:
        self.calls += 1
        return _FakeResp(self._text)


def _publisher(path: str, *, request_id: str, query: str) -> str:
    return "https://s3.example/signed"


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="req-test", user_id="U1", metadata={"user_email": "a@vectorinc.co.jp"}
    )


# ---- ① x_voice_search --------------------------------------------------------


def test_voice_search_happy_path() -> None:
    posts = [_post("1", likes=31), _post("2", likes=64), _post("3", "料理に使う白湯", 5)]
    apify = _FakeApify(posts)
    noise_json = json.dumps(
        {
            "keep": ["1", "2"],
            "author_notes": {"1": "美容系"},
            "noise_note": "料理文脈が混入",
        }
    )
    skill = XVoiceSearchSkill(
        apify=apify,  # type: ignore[arg-type]
        bedrock=_FakeBedrock(noise_json),
        publisher=_publisher,
    )
    out = skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯", "アサヒ 白湯"]), _ctx())
    assert out.selected == 2
    assert out.verified_count == 2
    assert out.posts[0].like_count == 64  # いいね降順
    assert out.posts[1].author_note == "美容系"
    assert out.noise_note == "料理文脈が混入"
    assert out.report_url == "https://s3.example/signed"
    assert "白湯" in out.slack_summary and out.total_cost_usd > 0
    assert len(apify.verify_calls) == 1  # 厳選分のみ検証


def test_voice_search_assigns_kaiwai_circles() -> None:
    """LLM が返す author_circles が投稿カードに界隈タグとして配線される（Part4）。"""
    posts = [_post("1", likes=31), _post("2", likes=64)]
    noise_json = json.dumps(
        {
            "keep": ["1", "2"],
            "author_circles": {"1": ["美容界隈", "淡色界隈"], "2": ["ガジェット界隈"]},
            "noise_note": "",
        }
    )
    skill = XVoiceSearchSkill(
        apify=_FakeApify(posts),  # type: ignore[arg-type]
        bedrock=_FakeBedrock(noise_json),
        publisher=_publisher,
    )
    out = skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯"]), _ctx())
    by_id = {p.post_id: p for p in out.posts}
    assert by_id["1"].author_circles == ["美容界隈", "淡色界隈"]  # マルチラベル
    assert by_id["2"].author_circles == ["ガジェット界隈"]


def test_voice_search_bundles_circles_per_author() -> None:
    """同一著者(handle)の複数投稿は界隈を union して両方に付ける（タグをブレさせない）。"""
    p1 = replace(
        _post("1", "コスメ購入品", 50), author_handle="uX", url="https://x.com/uX/status/1"
    )
    p2 = replace(_post("2", "淡色コーデ", 40), author_handle="uX", url="https://x.com/uX/status/2")
    noise_json = json.dumps(
        {
            "keep": ["1", "2"],
            "author_circles": {"1": ["美容界隈"], "2": ["淡色界隈"]},
            "noise_note": "",
        }
    )
    skill = XVoiceSearchSkill(
        apify=_FakeApify([p1, p2]),  # type: ignore[arg-type]
        bedrock=_FakeBedrock(noise_json),
        publisher=_publisher,
    )
    out = skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯"]), _ctx())
    for c in out.posts:
        assert set(c.author_circles) == {"美容界隈", "淡色界隈"}  # 同一著者は union


def test_card_renders_kaiwai_chips() -> None:
    """再現カード枠外に界隈チップを表示（空なら出さない）。"""
    from teamagent.skills.x_research.report import _card

    h = _card(
        XPostCard(
            post_id="1",
            url="https://x.com/u/1",
            author_handle="u",
            author_name="U",
            text="x",
            author_circles=["美容界隈", "淡色界隈"],
        )
    )
    assert "class='kaiwai'" in h and "#美容界隈" in h and "#淡色界隈" in h
    h2 = _card(XPostCard(post_id="2", url="", author_handle="u", text="x"))
    assert "class='kaiwai'" not in h2  # 界隈なしなら出さない（捏造しない）


def test_voice_search_drops_nonstring_circle_elements() -> None:
    """LLMが非文字列(null/dict)や過長文字列を界隈配列に混ぜても、文字列のみ・24字上限で採用。"""
    noise_json = json.dumps(
        {"keep": ["1"], "author_circles": {"1": ["美容界隈", None, {"x": 1}, "あ" * 50]}}
    )
    skill = XVoiceSearchSkill(
        apify=_FakeApify([_post("1")]),  # type: ignore[arg-type]
        bedrock=_FakeBedrock(noise_json),
        publisher=_publisher,
    )
    out = skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯"]), _ctx())
    circles = out.posts[0].author_circles
    assert "美容界隈" in circles
    assert "None" not in circles and all("{" not in c for c in circles)  # null/dict は弾く
    assert all(len(c) <= 24 for c in circles)  # 長さ上限（チップ崩れ防止）


def test_voice_search_unverified_marked_not_dropped() -> None:
    posts = [_post("1"), _post("2")]
    apify = _FakeApify(posts, verify_missing={"2"})
    skill = XVoiceSearchSkill(
        apify=apify,  # type: ignore[arg-type]
        bedrock=_FakeBedrock(json.dumps({"keep": ["1", "2"], "noise_note": ""})),
        publisher=_publisher,
    )
    out = skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯"]), _ctx())
    assert out.selected == 2  # 黙って捨てない
    unverified = [p for p in out.posts if not p.verified]
    assert len(unverified) == 1 and "要再確認" in unverified[0].verify_note


def test_voice_search_zero_results() -> None:
    skill = XVoiceSearchSkill(
        apify=_FakeApify([]), bedrock=_FakeBedrock("{}"), publisher=_publisher
    )  # type: ignore[arg-type]
    out = skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯"]), _ctx())
    assert out.posts == [] and "0件" in out.slack_summary


def test_voice_search_noise_filter_failure_is_fail_open() -> None:
    class _BrokenBedrock:
        def converse(self, **kw: Any) -> _FakeResp:
            raise RuntimeError("bedrock down")

    posts = [_post("1"), _post("2")]
    skill = XVoiceSearchSkill(
        apify=_FakeApify(posts),
        bedrock=_BrokenBedrock(),
        publisher=_publisher,  # type: ignore[arg-type]
    )
    out = skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯"]), _ctx())
    assert out.selected == 2  # 全件候補に残る
    assert any("ノイズ除去" in w for w in out.warnings)


def test_voice_search_cost_limit_message() -> None:
    class _DenyApify:
        def search_posts(self, *a: Any, **kw: Any) -> Any:
            raise CostLimitExceededError("今月のapify利用枠($50)を使い切りました")

    skill = XVoiceSearchSkill(apify=_DenyApify(), bedrock=_FakeBedrock("{}"), publisher=_publisher)  # type: ignore[arg-type]
    out = skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯"]), _ctx())
    assert "使い切りました" in out.slack_summary


def test_voice_search_rollout_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_RESEARCH_ALLOWED_EMAILS", "someone-else@vectorinc.co.jp")
    skill = XVoiceSearchSkill(apify=_FakeApify([]), publisher=_publisher)  # type: ignore[arg-type]
    out = skill.run(XVoiceSearchInput(product_name="白湯", queries=["白湯"]), _ctx())
    assert "段階公開中" in out.slack_summary


# ---- ② x_needs_mining --------------------------------------------------------


def test_needs_mining_clusters_and_min_faves() -> None:
    posts = [
        _post("1", "コンビニでお湯売ってほしい", 12606),
        _post("2", "コンビニ高い", 3468),
        _post("3", "いいね少ない投稿", 1),  # min_faves で足切り
    ]
    needs_json = json.dumps(
        {
            "clusters": [
                {"label": "サービスの穴", "insight": "お湯提供の顕在ニーズ", "post_ids": ["1"]},
                {"label": "価格", "insight": "値段を見ずに買う=贅沢", "post_ids": ["2", "999"]},
            ],
            "hypothesis_summary": "商品よりサービスの穴が大きくバズる",
        }
    )
    skill = XNeedsMiningSkill(
        apify=_FakeApify(posts),  # type: ignore[arg-type]
        analysis_bedrock=_FakeBedrock(needs_json),
        publisher=_publisher,
    )
    out = skill.run(XNeedsMiningInput(theme="コンビニ", min_faves=5), _ctx())
    assert len(out.posts) == 2
    assert [c.label for c in out.clusters] == ["サービスの穴", "価格"]
    assert out.clusters[1].post_ids == ["2"]  # 実在しないIDは落とす
    assert out.hypothesis_summary.startswith("商品より")
    assert out.report_url


def test_needs_mining_classify_failure_degrades() -> None:
    class _BrokenBedrock:
        def converse(self, **kw: Any) -> _FakeResp:
            raise RuntimeError("down")

    posts = [_post("1", likes=100)]
    skill = XNeedsMiningSkill(
        apify=_FakeApify(posts),
        analysis_bedrock=_BrokenBedrock(),
        publisher=_publisher,  # type: ignore[arg-type]
    )
    out = skill.run(XNeedsMiningInput(theme="コンビニ", min_faves=0), _ctx())
    assert len(out.posts) == 1 and out.clusters == []
    assert any("分類" in w for w in out.warnings)


# ---- ④ x_buzz_measure (submit) ------------------------------------------------


class _FakeStore:
    def __init__(self, submit_ok: bool = True, status: dict[str, Any] | None = None):
        self._ok = submit_ok
        self._status = status
        self.last_spec: dict[str, Any] | None = None
        self.cached: dict[str, Any] | None = None
        self.results: dict[str, Any] | None = None

    def submit(self, spec: dict[str, Any]) -> bool:
        self.last_spec = spec
        return self._ok

    def get_status(self, job_id: str) -> dict[str, Any] | None:
        return self._status

    def read_results(self, s3_prefix: str) -> dict[str, Any] | None:
        return self.results

    def cache_report(self, job_id: str, *, report_url: str, spike_analysis: str) -> None:
        self.cached = {"report_url": report_url, "spike_analysis": spike_analysis}


def test_buzz_submit_spec() -> None:
    store = _FakeStore()
    skill = XBuzzMeasureSkill(store=store)  # type: ignore[arg-type]
    out = skill.run(
        XBuzzMeasureInput(
            keyword="セブン 新商品",
            start_date="2026-06-01",
            end_date="2026-06-14",
            campaign_date="2026-06-07",
        ),
        _ctx(),
    )
    assert out.status == "queued" and out.job_id.startswith("xb_")
    spec = store.last_spec
    assert spec is not None
    assert spec["keyword"] == "セブン 新商品"
    assert spec["s3_prefix"] == f"x-research/{out.job_id}/"
    assert spec["requested_by"] == "a@vectorinc.co.jp"


def test_buzz_schema_rejects_long_period_and_outside_campaign() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        XBuzzMeasureInput(keyword="k", start_date="2026-01-01", end_date="2026-06-01")
    with pytest.raises(ValidationError):
        XBuzzMeasureInput(
            keyword="k",
            start_date="2026-06-01",
            end_date="2026-06-10",
            campaign_date="2026-07-01",
        )


# ---- ④ x_buzz_measure_status ---------------------------------------------------


def test_buzz_status_unknown_and_running() -> None:
    skill = XBuzzMeasureStatusSkill(store=_FakeStore(status=None), publisher=_publisher)  # type: ignore[arg-type]
    assert skill.run(XBuzzMeasureStatusInput(job_id="xb_x"), _ctx()).status == "unknown"

    running = _FakeStore(
        status={
            "status": "running",
            "requested_by": "a@vectorinc.co.jp",
            "progress": {"days_done": 3, "days_total": 14},
        }
    )
    skill2 = XBuzzMeasureStatusSkill(store=running, publisher=_publisher)  # type: ignore[arg-type]
    out = skill2.run(XBuzzMeasureStatusInput(job_id="xb_x"), _ctx())
    assert out.status == "running" and out.progress == {"days_done": 3, "days_total": 14}


def test_buzz_status_done_generates_and_caches_report() -> None:
    store = _FakeStore(
        status={
            "status": "done",
            "requested_by": "a@vectorinc.co.jp",
            "s3_prefix": "x-research/xb_1/",
            "total_cost_usd": 0.05,
        }
    )
    store.results = {
        "spec": {
            "keyword": "セブン 新商品",
            "start_date": "2026-07-01",
            "end_date": "2026-07-07",
            "campaign_date": "2026-07-07",
        },
        "daily_counts": [{"date": "2026-07-06", "count": 35}, {"date": "2026-07-07", "count": 43}],
        "top_posts": [
            {
                "post_id": "1",
                "url": "https://x.com/a/status/1",
                "author_handle": "a",
                "text": "新商品出てた",
                "like_count": 120,
                "verified": True,
                "verify_note": "",
            }
        ],
        "total_cost_usd": 0.05,
    }
    bedrock = _FakeBedrock("7/7に発話が集中。発売日に山が立った。")
    skill = XBuzzMeasureStatusSkill(store=store, analysis_bedrock=bedrock, publisher=_publisher)  # type: ignore[arg-type]
    out = skill.run(XBuzzMeasureStatusInput(job_id="xb_1"), _ctx())
    assert out.status == "done"
    assert out.daily_counts and out.top_posts[0].verified
    assert "山が立った" in out.spike_analysis
    assert out.report_url == "https://s3.example/signed"
    assert store.cached is not None  # 初回生成をキャッシュ
    assert bedrock.calls == 1


def test_buzz_status_done_uses_cache_without_regeneration() -> None:
    store = _FakeStore(
        status={
            "status": "done",
            "requested_by": "a@vectorinc.co.jp",
            "s3_prefix": "x-research/xb_1/",
            "report_url": "https://cached",
            "spike_analysis": "既に分析済み",
            "total_cost_usd": 0.05,
        }
    )
    store.results = {
        "spec": {},
        "daily_counts": [{"date": "2026-07-01", "count": 1}],
        "top_posts": [],
    }
    bedrock = _FakeBedrock("呼ばれないはず")
    skill = XBuzzMeasureStatusSkill(store=store, analysis_bedrock=bedrock, publisher=_publisher)  # type: ignore[arg-type]
    out = skill.run(XBuzzMeasureStatusInput(job_id="xb_1"), _ctx())
    assert out.report_url == "https://cached"
    assert out.spike_analysis == "既に分析済み"
    assert bedrock.calls == 0 and store.cached is None


def test_buzz_status_rollout_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_RESEARCH_ALLOWED_EMAILS", "other@vectorinc.co.jp")
    store = _FakeStore(status={"status": "done", "s3_prefix": "x-research/xb_1/"})
    skill = XBuzzMeasureStatusSkill(store=store, publisher=_publisher)  # type: ignore[arg-type]
    out = skill.run(XBuzzMeasureStatusInput(job_id="xb_1"), _ctx())
    # allowlist 外は結果を返さない（他人の結果や Sonnet コストを引けない）
    assert out.status == "denied" and "段階公開中" in out.message
    assert store.results is None or out.daily_counts == []


def test_buzz_status_owner_mismatch_denied() -> None:
    store = _FakeStore(
        status={
            "status": "done",
            "requested_by": "other@vectorinc.co.jp",
            "s3_prefix": "x-research/xb_1/",
        }
    )
    skill = XBuzzMeasureStatusSkill(store=store, publisher=_publisher)  # type: ignore[arg-type]
    out = skill.run(XBuzzMeasureStatusInput(job_id="xb_1"), _ctx())
    assert out.status == "denied" and "本人だけ" in out.message


def test_buzz_status_caches_spike_even_without_report_url() -> None:
    # publisher が None を返す環境（VSEO_REPORT_BUCKET 未設定）でも spike をキャッシュし、
    # 再照会で Sonnet を再生成しない（二重課金防止）。
    store = _FakeStore(
        status={
            "status": "done",
            "requested_by": "a@vectorinc.co.jp",
            "s3_prefix": "x-research/xb_1/",
        }
    )
    store.results = {
        "spec": {"keyword": "k", "start_date": "2026-07-01", "end_date": "2026-07-02"},
        "daily_counts": [{"date": "2026-07-01", "count": 5}],
        "top_posts": [],
    }
    bedrock = _FakeBedrock("山の分析")
    skill = XBuzzMeasureStatusSkill(
        store=store,
        analysis_bedrock=bedrock,
        publisher=lambda p, *, request_id, query: None,  # URL発行不可を模擬
    )  # type: ignore[arg-type]
    out = skill.run(XBuzzMeasureStatusInput(job_id="xb_1"), _ctx())
    assert out.report_url is None and out.spike_analysis == "山の分析"
    assert bedrock.calls == 1
    assert store.cached == {"report_url": "", "spike_analysis": "山の分析"}  # URL空でもcache


# ---- ワーカー -------------------------------------------------------------------


def test_worker_run_job_writes_results(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.workers import x_buzz_job

    statuses: list[tuple[str, dict[str, Any]]] = []
    s3_writes: dict[str, str] = {}
    monkeypatch.setattr(x_buzz_job, "_update_status", lambda t, j, s, d: statuses.append((s, d)))
    monkeypatch.setattr(
        x_buzz_job, "_put_s3", lambda b, k, body, ct: s3_writes.__setitem__(k, body)
    )

    class _DayApify:
        def search_posts_period(self, terms: list[str], *, start: str, **kw: Any) -> Any:
            if start == "2026-07-02":
                raise ApifyError("APIFY_RUN_FAILED: x")
            n = 3 if start == "2026-07-03" else 1
            return [replace(_post(f"{start}-{i}"), like_count=i * 10) for i in range(n)], 0.001

        def verify_posts(self, urls: list[str], **kw: Any) -> Any:
            return {url: _post(f"verified-{i}") for i, url in enumerate(urls)}, 0.001

    rc = x_buzz_job.run_job(
        {
            "job_id": "xb_t",
            "keyword": "k",
            "start_date": "2026-07-01",
            "end_date": "2026-07-03",
            "max_items_per_day": 50,
            "min_faves": 0,
            "s3_prefix": "x-research/xb_t/",
            "requested_by": "a@x.jp",
            "request_id": "req",
        },
        apify=_DayApify(),  # type: ignore[arg-type]
    )
    assert rc == 0
    assert statuses[-1][0] == "done"
    assert statuses[-1][1]["warnings"]  # 欠測日が警告に残る
    results = json.loads(s3_writes["x-research/xb_t/results.json"])
    assert [d["count"] for d in results["daily_counts"]] == [1, 0, 3]
    assert results["failed_days"] == ["2026-07-02"]
    assert len(results["top_posts"]) <= 10
    assert all(p["verified"] for p in results["top_posts"])


def test_worker_marks_top_posts_unverified_when_verification_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.workers import x_buzz_job

    s3_writes: dict[str, str] = {}
    monkeypatch.setattr(x_buzz_job, "_update_status", lambda *args: None)
    monkeypatch.setattr(
        x_buzz_job, "_put_s3", lambda b, k, body, ct: s3_writes.__setitem__(k, body)
    )

    class _VerifyFailApify:
        def search_posts_period(self, terms: list[str], **kw: Any) -> Any:
            return [_post("1")], 0.001

        def verify_posts(self, urls: list[str], **kw: Any) -> Any:
            raise ApifyError("APIFY_RUN_FAILED")

    rc = x_buzz_job.run_job(
        {
            "job_id": "xb_unverified",
            "keyword": "k",
            "start_date": "2026-07-01",
            "end_date": "2026-07-01",
            "requested_by": "a@x.jp",
        },
        apify=_VerifyFailApify(),  # type: ignore[arg-type]
    )
    assert rc == 0
    results = json.loads(s3_writes["x-research/xb_unverified/results.json"])
    assert results["top_posts"][0]["verified"] is False
    assert "要再確認" in results["top_posts"][0]["verify_note"]


# ---- P1: X投稿再現カード（report._card） ----


def test_card_recreates_x_post_with_avatar_media_engagement() -> None:
    from teamagent.skills.x_research.report import _card

    c = XPostCard(
        post_id="1",
        url="https://x.com/u/status/1",
        author_handle="u",
        author_name="ユーザーU",
        text="濃厚で最高",
        like_count=10,
        retweet_count=3,
        reply_count=1,
        view_count=500,
        is_verified=True,
        avatar_data="data:image/png;base64,AAAA",
        media_data=["data:image/png;base64,BBBB"],
        created_at="1750000000000",
        verified=True,
    )
    h = _card(c)
    assert "class='xc'" in h  # 再現カード枠
    assert "data:image/png;base64,AAAA" in h  # avatar 内包
    assert "data:image/png;base64,BBBB" in h  # media 内包
    assert "ユーザーU" in h and "@u" in h
    assert "<span class='bv'>✔</span>" in h  # 青バッジ（is_verified 時のみ）
    assert "❤️ 10" in h and "🔁 3" in h and "💬 1" in h and "👁 500" in h
    assert "✅ 実在検証済み" in h  # 検証チップは枠外ストリップ
    assert "不明" not in h


def test_card_falls_back_to_monogram_and_drops_fumei() -> None:
    from teamagent.skills.x_research.report import _card

    c = XPostCard(
        post_id="2",
        url="",
        author_handle="",
        author_name="名無し太郎",
        text="アイコン無し投稿",
        like_count=0,
        verified=False,
    )
    h = _card(c)
    assert "class='mono'" in h  # avatar 無→モノグラム
    assert "名無し太郎" in h
    assert "@不明" not in h and "投稿者不明" not in h  # @不明 は廃止・名前で埋まる
    assert "⚠️" in h  # 未検証チップ
    assert "👁" not in h  # view0 は描かない（捏造しない）
