"""実カルテ・gateway・投稿経路を通し、日付と状態行を DM にそのまま届ける。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from structlog.testing import capture_logs

from teamagent.mcp_gateway import detached_jobs, direct_karte, direct_summary, server
from teamagent.skills.base import SkillContext
from teamagent.skills.clientkarte.schema import ClientKarteInput
from teamagent.skills.clientkarte.skill import ClientKarteSkill
from tests.mcp_gateway.test_surface_video_followup import (
    CHANNEL,
    DM,
    ME,
    OTHER,
    _claim,
    _FakeSlack,
    _resolver_for,
    _spec,
    _Verifier,
)
from tests.skills.clientkarte.test_clientkarte_status import status_skill as status_skill
from tests.skills.search.test_case_state_evidence import row

REQUEST = "JTBの案件って今どうなってる？"


@pytest.fixture
def skill(status_skill: tuple, monkeypatch: pytest.MonkeyPatch) -> ClientKarteSkill:
    skill, pg = status_skill
    source = row(state="lost", state_as_of="2026-09-11", evidence_date="2026-09-11")
    source.metadata.update({"client_name": "JTB", "title": "JTB 企業広報の提案"})
    pg.search_drive_by_client_names.return_value = [source]
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", ME)
    return skill  # type: ignore[no-any-return]


@pytest.fixture
def slack(monkeypatch: pytest.MonkeyPatch) -> _FakeSlack:
    monkeypatch.setenv(direct_karte.ENABLED_ENV, "1")
    monkeypatch.setenv(direct_karte.ALLOWED_EMAILS_ENV, ME)
    monkeypatch.setenv("USE_PAYLOAD_OFFLOAD", "0")
    monkeypatch.delenv("ENABLE_PROGRESS_NOTIFY", raising=False)
    monkeypatch.setattr(server, "_record_usage", lambda **kw: None)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0)
    fake = _FakeSlack()
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: fake)
    return fake


def _baseline(skill: ClientKarteSkill, *, email: str = ME, channel: str = DM) -> dict[str, Any]:
    return skill.run(
        ClientKarteInput(client_name="JTB"),
        SkillContext(
            metadata={"user_email": email, "request_text": REQUEST if channel == DM else ""}
        ),
    ).model_dump(mode="json")


async def _dispatch(
    skill: ClientKarteSkill, *, email: str = ME, channel: str = DM
) -> dict[str, Any]:
    contents = await server.dispatch_tool(
        _spec(skill, "clientkarte"),
        "clientkarte",
        {"client_name": "JTB", server.USER_CONTEXT_KEY: {"request_text": REQUEST}},
        identity_resolver=_resolver_for(email),
        caller_claim_verifier=_Verifier(_claim(channel=channel)),  # type: ignore[arg-type]
    )
    return json.loads(contents[0].text)  # type: ignore[no-any-return]


@pytest.mark.parametrize("emails", [ME, "*"])
async def test_posts_complete_answer_without_model_material(
    skill: ClientKarteSkill, slack: _FakeSlack, monkeypatch: pytest.MonkeyPatch, emails: str
) -> None:
    monkeypatch.setenv(direct_karte.ALLOWED_EMAILS_ENV, emails)
    baseline = _baseline(skill)
    with capture_logs() as logs:
        result = await _dispatch(skill)
    answer = baseline["answer"]
    assert "（8/24 時点）" in answer
    assert "予定だった（結果は未確認）" in answer
    assert "\n状態:" in answer and "根拠日: 2026-09-11" in answer
    assert slack.posts == [
        {
            "channel": DM,
            "thread_ts": None,
            "blocks": None,
            "text": detached_jobs.slack_escape(answer),
        }
    ]
    assert result["status"] == "posted" and result["delivered"] is True
    assert "投稿済み" in result["note"]
    assert "JTB" not in str(result) and "events" not in result and "https://" not in str(result)
    event = next(log for log in logs if log["event"] == "direct_karte_posted")
    assert event["status"] == "posted"
    assert "JTB" not in str(event) and "https://" not in str(event)


@pytest.mark.parametrize("case", ["off", "outside", "empty", "channel"])
async def test_ineligible_keeps_original_output(
    skill: ClientKarteSkill, slack: _FakeSlack, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    if case == "off":
        monkeypatch.delenv(direct_karte.ENABLED_ENV)
    if case == "empty":
        monkeypatch.delenv(direct_karte.ALLOWED_EMAILS_ENV)
    email = OTHER if case == "outside" else ME
    channel = CHANNEL if case == "channel" else DM
    assert await _dispatch(skill, email=email, channel=channel) == _baseline(
        skill, email=email, channel=channel
    )
    assert slack.posts == []


@pytest.mark.parametrize("failure", ["not_ok", "exception", "timeout"])
async def test_failure_is_open_and_timeout_does_not_duplicate(
    skill: ClientKarteSkill, slack: _FakeSlack, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    calls = []

    async def post_message(**kwargs: Any) -> None:
        calls.append(kwargs)
        if failure == "timeout":
            raise TimeoutError
        if failure == "exception":
            raise RuntimeError("非公開の本文 https://private.example")

    monkeypatch.setattr(slack, "post_message", post_message)
    with capture_logs() as logs:
        result = await _dispatch(skill)
    if failure == "timeout":
        assert result["status"] == "posted" and result["delivered"] is False
        assert "JTB" not in str(result) and "events" not in result
    else:
        assert result == _baseline(skill)
    assert len(calls) == (1 if failure == "timeout" else 2)
    assert "private.example" not in str(logs)


def test_unexpected_post_exception_keeps_original_output(
    slack: _FakeSlack, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("非公開の本文 https://private.example")

    monkeypatch.setattr(detached_jobs, "post_to_origin_status", fail)
    data = {"answer": "回答"}
    with capture_logs() as logs:
        result = direct_karte.maybe_post(
            data,
            verified_caller=_claim(),
            metadata={"identity_verified": True, "user_email": ME},
            request_id="r",
        )
    assert result is None and data == {"answer": "回答"} and slack.posts == []
    assert logs[-1]["status"] == "failed" and "private.example" not in str(logs)


@pytest.mark.parametrize("answer", [None, "", "  ", 123])
def test_empty_or_invalid_answer_is_not_posted(slack: _FakeSlack, answer: Any) -> None:
    assert (
        direct_karte.maybe_post(
            {"answer": answer},
            verified_caller=_claim(),
            metadata={"identity_verified": True, "user_email": ME},
            request_id="r",
        )
        is None
    )
    assert slack.posts == []


def test_policy_defaults_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(direct_karte.ENABLED_ENV, raising=False)
    monkeypatch.delenv(direct_karte.ALLOWED_EMAILS_ENV, raising=False)
    monkeypatch.setenv(direct_summary.ENABLED_ENV, "1")
    monkeypatch.setenv(direct_summary.ALLOWED_EMAILS_ENV, "*")
    assert direct_karte.load_policy() == direct_summary.DirectPolicy()
    monkeypatch.setenv(direct_karte.ENABLED_ENV, "true")
    monkeypatch.setenv(direct_karte.ALLOWED_EMAILS_ENV, f" {ME.upper()} ,,{OTHER}")
    assert direct_karte.load_policy() == direct_summary.DirectPolicy(True, frozenset({ME, OTHER}))
