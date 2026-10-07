"""83 枚提案書（proposal_builder_submit）の許可リスト ``PROPOSAL_BUILDER_ALLOWED_EMAILS``。

空＝全員拒否（今の本番と同じ）・個人＝その人だけ・``*``＝本人確認済みの全員・未検証は常に拒否。
拒否は例外ではなく返り値（status=failed・「準備中」の文）で、job row も thread も作らない。
変異: ``allowlist.email_allowed`` を常に True にすると、空・他人・未検証(*)の試験が赤。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_builder.schema import ProposalBuilderSubmitInput
from teamagent.skills.proposal_builder.skill import (
    ALLOWED_EMAILS_ENV,
    NOT_READY_MESSAGE,
    ProposalBuilderSubmitSkill,
    submit_allowed,
)

KOMATA = "s-komata@vectorinc.co.jp"
OTHER = "someone@vectorinc.co.jp"


class _Probe:
    """許可されたときだけ通る従来経路（入力検証・thread 起動）を観測する。"""

    def __init__(self) -> None:
        self.validated = 0
        self.launched: list[str] = []

    def validate(self, _input: Any) -> None:
        self.validated += 1

    def launch(self, target: Any, name: str) -> None:
        self.launched.append(name)  # 本体は走らせない（job は queued のまま）


def _skill(probe: _Probe, memory: dict[str, dict[str, Any]]) -> ProposalBuilderSubmitSkill:
    return ProposalBuilderSubmitSkill(
        builder_factory=lambda: None,  # type: ignore[arg-type,return-value]
        store=ProposalJobStore(table_name="", memory=memory),
        thread_launcher=probe.launch,
        input_validator=probe.validate,
        heartbeat_seconds=0,
        retry_after_seconds=30,
    )


def _input() -> ProposalBuilderSubmitInput:
    return ProposalBuilderSubmitInput(
        gemini_json={"A": {"client_name": "テスト社"}},
        posting_start_date=date(2026, 9, 1),
    )


def _ctx(email: str | None, *, verified: bool = True) -> SkillContext:
    metadata: dict[str, Any] = {"channel_id": "D123", "thread_ts": "1.2"}
    if email is not None:
        metadata["user_email"] = email
    metadata["identity_verified"] = verified
    return SkillContext(request_id="pb-allowlist", user_id=email, metadata=metadata)


def _submit(
    monkeypatch: pytest.MonkeyPatch, env: str | None, ctx: SkillContext
) -> tuple[Any, _Probe, dict[str, dict[str, Any]]]:
    if env is None:
        monkeypatch.delenv(ALLOWED_EMAILS_ENV, raising=False)
    else:
        monkeypatch.setenv(ALLOWED_EMAILS_ENV, env)
    probe = _Probe()
    memory: dict[str, dict[str, Any]] = {}
    out = _skill(probe, memory).run(_input(), ctx)
    return out, probe, memory


def _assert_rejected(out: Any, probe: _Probe, memory: dict[str, Any]) -> None:
    assert out.status == "failed"
    assert out.job_id == ""
    assert out.retry_after_seconds == 0
    assert out.message == NOT_READY_MESSAGE
    assert "準備中" in out.message
    # 内部語（job_id・env 名・コード名）を利用者向けの文に載せない
    assert "job" not in out.message.lower() and "ALLOWED" not in out.message
    assert probe.validated == 0 and probe.launched == []
    assert memory == {}


def _assert_accepted(out: Any, probe: _Probe, memory: dict[str, Any]) -> None:
    assert out.status == "queued"
    assert out.job_id.startswith("pb_")
    assert out.retry_after_seconds == 30
    assert probe.validated == 1
    assert probe.launched == [f"proposal-builder-{out.job_id}"]
    assert memory[out.job_id]["status"] == "queued"


@pytest.mark.parametrize("env", [None, "", " , "])
def test_empty_allowlist_rejects_everyone(monkeypatch: pytest.MonkeyPatch, env: str | None) -> None:
    """空・未設定は全員拒否＝今の本番と同じ（小俣さんでも使えない）。"""
    for email in (KOMATA, OTHER):
        out, probe, memory = _submit(monkeypatch, env, _ctx(email))
        _assert_rejected(out, probe, memory)


def test_individual_allowlist_opens_only_to_that_person(monkeypatch: pytest.MonkeyPatch) -> None:
    out, probe, memory = _submit(monkeypatch, f" {KOMATA.upper()} ,", _ctx(KOMATA))
    _assert_accepted(out, probe, memory)

    out, probe, memory = _submit(monkeypatch, KOMATA, _ctx(OTHER))
    _assert_rejected(out, probe, memory)


def test_wildcard_opens_to_every_verified_user(monkeypatch: pytest.MonkeyPatch) -> None:
    for email in (KOMATA, OTHER):
        out, probe, memory = _submit(monkeypatch, "*", _ctx(email))
        _assert_accepted(out, probe, memory)


@pytest.mark.parametrize("env", ["*", KOMATA])
def test_unverified_caller_is_rejected_even_when_listed(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    """LEGACY（resolver 無・identity_verified=False）や未解決 email は、個人指定でも * でも拒否。"""
    out, probe, memory = _submit(monkeypatch, env, _ctx(KOMATA, verified=False))
    _assert_rejected(out, probe, memory)
    out, probe, memory = _submit(monkeypatch, env, _ctx(None, verified=True))
    _assert_rejected(out, probe, memory)


def test_submit_allowed_reads_env_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """TD の env 差し替えだけで段階を進められる（import 時に固定しない）。"""
    meta = {"identity_verified": True, "user_email": KOMATA}
    monkeypatch.setenv(ALLOWED_EMAILS_ENV, "")
    assert submit_allowed(meta) is False
    monkeypatch.setenv(ALLOWED_EMAILS_ENV, KOMATA)
    assert submit_allowed(meta) is True
    monkeypatch.setenv(ALLOWED_EMAILS_ENV, "*")
    assert submit_allowed({"identity_verified": True, "user_email": OTHER}) is True
    assert submit_allowed({"identity_verified": False, "user_email": OTHER}) is False
