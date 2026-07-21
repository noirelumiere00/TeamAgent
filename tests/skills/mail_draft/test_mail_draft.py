"""mail_draft Skill のテスト。

実 HMAC トークン（encode→decode を通す）で検証し、生成本体（generate_draft_for_thread）は
FakeMorning でモックする。Gmail/Bedrock には触れない。
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.mail_draft.schema import MailDraftInput
from teamagent.skills.mail_draft.skill import MailDraftSkill
from teamagent.skills.morning_digest.draft_token import encode_draft_token

OWNER = "s-komata@vectorinc.co.jp"
_MAIL_SECRET = "mail-draft-test-secret-" + "m" * 32


class _FakeMorning:
    """MorningDigestSkill の差し替え。generate_draft_for_thread が固定 result を返す。"""

    def __init__(self, result: dict[str, Any], **_kw: Any) -> None:
        self._result = result

    def generate_draft_for_thread(
        self, thread_id: str, requester: str, ctx: SkillContext
    ) -> dict[str, Any]:
        out = dict(self._result)
        out.setdefault("thread_url", f"https://mail.google.com/mail/u/0/#all/{thread_id}")
        return out


def _ctx(email: str = OWNER) -> SkillContext:
    return SkillContext(request_id="r", user_id="U1", metadata={"user_email": email})


def _patch_morning(monkeypatch: Any, result: dict[str, Any]) -> None:
    import teamagent.skills.morning_digest.skill as ms

    monkeypatch.setattr(ms, "MorningDigestSkill", lambda **kw: _FakeMorning(result, **kw))


def test_valid_token_creates_draft(monkeypatch: Any) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_SECRET)
    _patch_morning(monkeypatch, {"created": True, "error": None, "cost_usd": 0.01})
    token = encode_draft_token("thr_1", OWNER)
    out = MailDraftSkill().run(MailDraftInput(draft_token=token), _ctx())
    assert out.created is True
    assert out.error == ""
    assert "作成しました" in out.message
    assert "#all/thr_1" in out.open_url  # その案件スレッドへのリンク


def test_invalid_token_is_expired(monkeypatch: Any) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_SECRET)
    out = MailDraftSkill().run(MailDraftInput(draft_token="garbage.token"), _ctx())
    assert out.created is False
    assert out.error == "expired"


def test_token_owner_mismatch_rejected(monkeypatch: Any) -> None:
    # 別人の所有トークンを本人が押しても decode が None（fail-closed）。
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_SECRET)
    token = encode_draft_token("thr_1", "someone-else@vectorinc.co.jp")
    out = MailDraftSkill().run(MailDraftInput(draft_token=token), _ctx(OWNER))
    assert out.error == "expired"


def test_requires_user_email() -> None:
    with pytest.raises(PermissionError):
        MailDraftSkill().run(MailDraftInput(draft_token="x.y"), _ctx(email=""))


def test_quota_exceeded(monkeypatch: Any) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_SECRET)
    _patch_morning(monkeypatch, {"created": True, "error": None})
    skill = MailDraftSkill()
    token = encode_draft_token("thr_q", OWNER)
    for _ in range(MailDraftSkill._QUOTA_LIMIT):
        assert skill.run(MailDraftInput(draft_token=token), _ctx()).created is True
    out = skill.run(MailDraftInput(draft_token=token), _ctx())
    assert out.error == "quota"
    assert out.created is False


def test_error_mapping_not_connected(monkeypatch: Any) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_SECRET)
    _patch_morning(monkeypatch, {"created": False, "error": "not_connected"})
    token = encode_draft_token("thr_2", OWNER)
    out = MailDraftSkill().run(MailDraftInput(draft_token=token), _ctx())
    assert out.error == "not_connected"
    assert "連携" in out.message


def test_already_has_draft(monkeypatch: Any) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_SECRET)
    _patch_morning(monkeypatch, {"created": False, "already": True})
    token = encode_draft_token("thr_3", OWNER)
    out = MailDraftSkill().run(MailDraftInput(draft_token=token), _ctx())
    assert out.already is True
    assert out.created is False
    assert "既に下書き" in out.message
