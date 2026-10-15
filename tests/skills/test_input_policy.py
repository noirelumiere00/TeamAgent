"""入力の分類・会話台帳・安全境界を実際のスキーマと受付で検証する。"""

from __future__ import annotations

import time
from typing import Any

import pytest

from teamagent.skills._shared import input_policy as policy
from teamagent.skills.base import SkillContext
from teamagent.skills.omiyage_report.preflight import build_needs_input_message, run_preflight
from teamagent.skills.omiyage_report.schema import OmiyageReportSubmitInput
from teamagent.skills.omiyage_report.skill import OmiyageReportSubmitSkill


def metadata(**updates: Any) -> dict[str, Any]:
    return {
        "user_email": "a@example.com",
        "identity_verified": True,
        "channel_id": "D123",
        "thread_ts": "1.0",
        **updates,
    }


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_INPUT_POLICY_V1", "1")
    monkeypatch.setenv("INPUT_POLICY_ALLOWED_EMAILS", "*")
    monkeypatch.setattr(policy, "CACHE", policy.PolicyCache())


@pytest.mark.parametrize(
    "flag,allowed,updates",
    [
        ("0", "*", {}),
        ("true", "*", {}),
        ("1", "", {}),
        ("1", "b@example.com", {}),
        ("1", "*", {"identity_verified": False}),
        ("1", "*", {"user_email": ""}),
        ("1", "*", {"channel_id": ""}),
    ],
)
def test_off_outside_unknown_preserve_preflight_bytes(
    monkeypatch: pytest.MonkeyPatch, flag: str, allowed: str, updates: dict[str, Any]
) -> None:
    monkeypatch.setenv("USE_INPUT_POLICY_V1", flag)
    monkeypatch.setenv("INPUT_POLICY_ALLOWED_EMAILS", allowed)
    ctx = SkillContext(metadata=metadata(**updates))
    assert not policy.enabled(ctx.metadata)
    input = OmiyageReportSubmitInput(brand="エムキュア")
    expected = build_needs_input_message(input, run_preflight(input))
    output = OmiyageReportSubmitSkill().run(input, ctx)
    assert output.message.encode() == expected.encode()
    assert output.status == "needs_input"
    assert output.missing == ["competitors", "keywords"]


def test_nice_never_asks_and_note() -> None:
    result = policy.evaluate(policy.OMIYAGE_FIELDS, {"brand": "エムキュア"})
    assert result.decision == "proceed"
    assert result.missing == ()
    assert (
        result.note
        == "今回は競合比較を省きました、一般キーワード検索を省きました、公式投稿の露出判定を省きました。"
    )


def test_required_asks_once_then_actual_alternative() -> None:
    skill = OmiyageReportSubmitSkill()
    ctx = SkillContext(metadata=metadata())
    first = skill.run(OmiyageReportSubmitInput(), ctx)
    second = skill.run(OmiyageReportSubmitInput(), ctx)
    assert first.status == "needs_input"
    assert "1. 対象ブランド" in first.message
    assert second.status == "failed"
    assert "骨子：" in second.message
    assert "ください" not in second.message


@pytest.mark.parametrize(
    "updates",
    [
        {"user_email": "b@example.com"},
        {"channel_id": "D999"},
        {"thread_ts": "2.0"},
        {"_input_policy_session": "other"},
    ],
)
def test_declined_and_question_do_not_leak(updates: dict[str, Any]) -> None:
    cache = policy.PolicyCache()
    specs = (
        policy.FieldSpec("brand", "required", "対象ブランド"),
        policy.FieldSpec("keywords", "nice_to_have", "検索語", ["美容"]),
    )
    first = cache.apply(metadata(), "tool", specs, {"keywords": ["不要"]})
    assert first.decision == "ask_once"
    same = cache.apply(metadata(), "tool", specs, {"keywords": ["お任せ"]})
    assert same.decision == "proceed_partial"
    assert same.states["keywords"] == "declined"
    assert same.values["keywords"] is None
    other = cache.apply(metadata(**updates), "tool", specs, {})
    assert other.decision == "ask_once"
    assert other.values["keywords"] == ["美容"]


def test_four_states_and_confirm_never_defaulted() -> None:
    specs = tuple(policy.FieldSpec(k, "nice_to_have", "項目", "既定") for k in ("a", "b", "c", "d"))
    result = policy.evaluate(
        (*specs, policy.FieldSpec("send", "confirm_side_effect", "送信確認", True)),
        {"b": "指定", "c": "お任せ", "d": "不要"},
    )
    assert result.states == {
        "a": "unspecified",
        "b": "given",
        "c": "delegated",
        "d": "declined",
        "send": "unspecified",
    }
    assert "send" not in result.values
    assert result.values["d"] is None
    assert result.values["c"] == "既定"


def test_cache_ttl_and_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    cache = policy.PolicyCache(ttl=2, max_entries=2)
    specs = (policy.FieldSpec("brand", "required", "対象ブランド"),)
    assert cache.apply(metadata(), "tool", specs, {}).decision == "ask_once"
    clock[0] = 4.0
    assert cache.apply(metadata(), "tool", specs, {}).decision == "ask_once"
    for n in range(10):
        cache.apply(metadata(thread_ts=str(n)), "tool", specs, {})
    assert len(cache._entries) == 2


def test_decline_is_scoped_to_brand() -> None:
    cache = policy.PolicyCache()
    cache.apply(metadata(), "tool", policy.OMIYAGE_FIELDS, {"brand": "A", "keywords": ["不要"]})
    result = cache.apply(
        metadata(), "tool", policy.OMIYAGE_FIELDS, {"brand": "B", "keywords": ["美容"]}
    )
    assert result.states["keywords"] == "given"


def test_declined_required_is_never_asked() -> None:
    result = policy.evaluate(policy.OMIYAGE_FIELDS, {"brand": "不要"})
    assert result.decision == "proceed_partial"
    assert "ください" not in result.message


def test_questions_have_numbered_choices_and_no_internal_words() -> None:
    result = policy.evaluate(policy.OMIYAGE_FIELDS, {})
    assert "1) 対象を指定する／2) 骨子で進める" in result.message
    assert not any(
        word in result.message for word in ("brand", "required", "nice_to_have", "keywords")
    )


def test_partial_answer_cannot_restart_question_count() -> None:
    cache = policy.PolicyCache()
    specs = (
        policy.FieldSpec("query", "required", "調べたい内容"),
        policy.FieldSpec("url", "required", "対象のURL"),
    )
    assert cache.apply(metadata(), "tool", specs, {}).decision == "ask_once"
    assert cache.apply(metadata(), "tool", specs, {"query": "調査"}).decision == "proceed_partial"
