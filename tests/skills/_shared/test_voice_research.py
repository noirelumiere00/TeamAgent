from __future__ import annotations

from typing import Any

import pytest

from teamagent.adapters.apify_client import ApifyError
from teamagent.skills._shared.voice_research import VoiceResearch, deck_voice_allowed
from teamagent.skills.base import SkillContext


@pytest.fixture(autouse=True)
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_DECK_VOICE_RESEARCH", "1")
    monkeypatch.setenv("DECK_VOICE_ALLOWED_EMAILS", "*")


def ctx() -> SkillContext:
    return SkillContext(
        request_id="voice-test",
        user_id="U1",
        metadata={
            "identity_verified": True,
            "user_email": "owner@example.test",
        },
    )


def test_flag_and_empty_allowlist_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_DECK_VOICE_RESEARCH", raising=False)
    monkeypatch.setenv("DECK_VOICE_ALLOWED_EMAILS", "*")
    assert not deck_voice_allowed(ctx())
    monkeypatch.setenv("USE_DECK_VOICE_RESEARCH", "1")
    monkeypatch.setenv("DECK_VOICE_ALLOWED_EMAILS", "")
    assert not deck_voice_allowed(ctx())
    monkeypatch.setenv("DECK_VOICE_ALLOWED_EMAILS", "owner@example.test")
    assert deck_voice_allowed(ctx())
    ctx_unverified = ctx()
    ctx_unverified.metadata["identity_verified"] = False
    assert not deck_voice_allowed(ctx_unverified)


def test_disabled_collection_never_initializes_paid_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_DECK_VOICE_RESEARCH", "0")
    results = VoiceResearch().collect([("sample", "https://www.tiktok.com/@s/video/1")], ctx())
    assert results[0].status == "not_requested"
    assert results[0].cost_usd == 0


def test_empty_failed_and_partial_keep_results_and_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teamagent.adapters.url_guard._default_resolve", lambda host: ["8.8.8.8"])

    class Apify:
        def tiktok_comments(self, url: str, **kwargs: Any) -> tuple[list[dict[str, Any]], float]:
            if url.endswith("/2"):
                raise ApifyError("APIFY_TIMEOUT")
            return [{"id": "c1", "text": "同じ合成本文", "likes": 1}], 0.1

    from types import SimpleNamespace

    model = SimpleNamespace(
        converse=lambda **kw: SimpleNamespace(text="{}", usage=SimpleNamespace(cost_usd=0))
    )
    research = VoiceResearch(apify=Apify(), bedrock=model)
    batches = research.collect(
        [
            ("brand", "https://www.tiktok.com/@s/video/1"),
            ("brand", "https://www.tiktok.com/@s/video/2"),
        ],
        ctx(),
    )
    assert [b.status for b in batches[:2]] == ["partial", "failed"]
    assert batches[2].status == "skipped"
    assert sum(b.cost_usd for b in batches) >= 0.1
    assert batches[0].items[0].text == "同じ合成本文"


def test_x_reuse_is_verified_owner_and_product_bound() -> None:
    from teamagent.skills._shared.voice_research import remember_verified_x, reused_x
    from teamagent.skills.x_research.schema import XPostCard

    posts = [
        XPostCard(
            post_id="1", url="https://x.com/sample/status/1", text="検証済み合成本文", verified=True
        ),
        XPostCard(post_id="2", url="https://x.com/sample/status/2", text="未検証", verified=False),
    ]
    owner = ctx()
    remember_verified_x(owner, "reuse-test", posts)
    assert len(reused_x(owner, "reuse-test")) == 1
    assert reused_x(owner, "another-product") == []
    other = ctx()
    other.metadata["user_email"] = "other@example.test"
    assert reused_x(other, "reuse-test") == []


def test_success_empty_response_is_not_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("teamagent.adapters.url_guard._default_resolve", lambda host: ["8.8.8.8"])

    class Empty:
        def tiktok_comments(self, url: str, **kwargs: Any) -> tuple[list[dict[str, Any]], float]:
            assert kwargs["require_cost_reservation"] is True
            return [], 0

    batches = VoiceResearch(apify=Empty()).collect(
        [("empty-test", "https://www.tiktok.com/@s/video/1")], ctx()
    )
    assert batches[0].status == "ok_empty"


def test_acquisition_is_deduplicated_and_limited_to_four_videos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("teamagent.adapters.url_guard._default_resolve", lambda host: ["8.8.8.8"])
    calls: list[str] = []

    class Empty:
        def tiktok_comments(self, url: str, **kwargs: Any) -> tuple[list[Any], float]:
            calls.append(url)
            assert kwargs["max_comments"] == 100
            assert kwargs["deadline_s"] == 60
            assert kwargs["require_cost_reservation"] is True
            return [], 0

    targets = [("cap-test", f"https://www.tiktok.com/@s/video/{i}") for i in range(6)]
    batches = VoiceResearch(apify=Empty()).collect([targets[0], *targets], ctx())
    assert calls == [url for _, url in targets[:4]]
    assert sum(batch.reserved_usd for batch in batches) == pytest.approx(0.4)


def test_classification_reservation_stops_after_five_calls() -> None:
    from types import SimpleNamespace

    from teamagent.skills._shared.voice_research import remember_verified_x
    from teamagent.skills.x_research.schema import XPostCard

    owner = ctx()
    products = [f"classification-cap-test-{i}" for i in range(7)]
    for product in products:
        remember_verified_x(
            owner,
            product,
            [
                XPostCard(
                    post_id="7", url="https://x.com/sample/status/7", text="合成本文", verified=True
                )
            ],
        )
    calls = 0

    def converse(**kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return SimpleNamespace(text="{}", usage=SimpleNamespace(cost_usd=0.01))

    batches = VoiceResearch(bedrock=SimpleNamespace(converse=converse)).collect(
        [], owner, products=products
    )
    assert calls == 5
    assert sum(batch.reserved_usd for batch in batches) == pytest.approx(0.25)
    assert all(batch.status == "partial" for batch in batches)
    assert all(batch.reason == "分類費用上限・未分類あり" for batch in batches[5:])
