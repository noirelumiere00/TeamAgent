"""digest_ack Skill（☑️確認済みボタンの押下処理）のテスト。

検証観点:
  - G1: user_email 未指定/空は PermissionError
  - fail-closed: 無効トークンでは **ストアを一度も呼ばない**
  - ack 成功で取り消しトークンが返る / unack では返らない
  - 書込 0 件を成功と言わない
  - G3: 返答文に item_key・トークンが出ない
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.digest_ack.schema import DigestAckInput
from teamagent.skills.digest_ack.skill import DigestAckSkill
from teamagent.skills.morning_digest.ack_token import (
    AckItem,
    encode_ack_all_token,
    encode_ack_token,
    encode_unack_token,
)
from teamagent.skills.morning_digest.draft_token import encode_draft_token

ME = "me@vectorinc.co.jp"
_HMAC_SECRET = "mail-primary-" + "m" * 32
_ITEM_A = AckItem("m", "0123456789abcdef", 1_718_681_400)
_ITEM_B = AckItem("s", "fedcba9876543210", 3)


class _Store:
    """本番の失敗モードを再現するフェイク。

    実 ``DigestAckStore`` は書込に失敗しても例外を投げず **0 を返す**（呼出側が
    利用者に失敗を伝える契約）。フェイクもその形にする。例外を投げるフェイクにすると、
    「0 を成功と誤って扱う」実装を通してしまう。
    """

    def __init__(self, *, ack_rows: int = 1, unack_rows: int = 1) -> None:
        self._ack_rows = ack_rows
        self._unack_rows = unack_rows
        self.ack_calls: list[tuple[str, tuple[AckItem, ...]]] = []
        self.unack_calls: list[tuple[str, tuple[AckItem, ...]]] = []

    def ack(self, user_email: str, items: Any, *, request_id: str) -> int:
        self.ack_calls.append((user_email, tuple(items)))
        return self._ack_rows

    def unack(self, user_email: str, items: Any, *, request_id: str) -> int:
        self.unack_calls.append((user_email, tuple(items)))
        return self._unack_rows


@pytest.fixture(autouse=True)
def _hmac_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _HMAC_SECRET)
    monkeypatch.delenv("MAIL_ACTION_TTL_S", raising=False)


def _ctx(email: str | None = ME) -> SkillContext:
    meta = {"user_email": email} if email is not None else {}
    return SkillContext(request_id="req-ack", metadata=meta)


def _run(token: str, store: _Store, *, ctx: SkillContext | None = None) -> Any:
    return DigestAckSkill(store=store).run(DigestAckInput(ack_token=token), ctx or _ctx())


# ── G1 ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("email", [None, "", "   "])
def test_fail_closed_without_requester(email: str | None) -> None:
    token = encode_ack_token([_ITEM_A], ME) or ""
    with pytest.raises(PermissionError):
        _run(token, _Store(), ctx=_ctx(email))


# ── fail-closed ──────────────────────────────────────────────────────────


def test_empty_token_is_no_input() -> None:
    store = _Store()
    out = _run("", store)
    assert out.error == "no_input"
    assert store.ack_calls == [] and store.unack_calls == []


@pytest.mark.parametrize(
    "bad",
    ["not-a-token", "a.b", ""],
)
def test_broken_token_never_reaches_the_store(bad: str) -> None:
    store = _Store()
    out = _run(bad, store)
    assert out.error in {"expired", "no_input"}
    assert out.acked == 0 and out.unacked == 0
    assert store.ack_calls == [] and store.unack_calls == []


def test_other_owner_token_is_rejected() -> None:
    token = encode_ack_token([_ITEM_A], "someone-else@vectorinc.co.jp") or ""
    store = _Store()
    out = _run(token, store)
    assert out.error == "expired"
    assert store.ack_calls == []


def test_draft_token_is_not_accepted_as_ack() -> None:
    """purpose 分離の配線が Skill まで通っていること（別用途トークンの転用防止）。"""
    store = _Store()
    out = _run(encode_draft_token("thread-123", ME) or "", store)
    assert out.error == "expired"
    assert store.ack_calls == []


# ── ack ──────────────────────────────────────────────────────────────────


def test_single_ack_returns_undo_token() -> None:
    store = _Store(ack_rows=1)
    out = _run(encode_ack_token([_ITEM_A], ME) or "", store)

    assert out.error == ""
    assert out.acked == 1
    assert out.undo_token, "取り消し導線が返ること（裁定: 押下直後の ephemeral に出す）"
    assert store.ack_calls == [(ME, (_ITEM_A,))]
    assert "新しい返信が来たら" in out.message


def test_ack_all_passes_every_item_through() -> None:
    store = _Store(ack_rows=2)
    out = _run(encode_ack_all_token([_ITEM_A, _ITEM_B], ME) or "", store)

    assert out.acked == 2
    assert store.ack_calls == [(ME, (_ITEM_A, _ITEM_B))]
    assert "2 件" in out.message


def test_zero_rows_is_not_reported_as_success() -> None:
    """書込 0 件を成功と言わない（言うと『押したのに翌朝また出る』を診断できなくなる）。"""
    store = _Store(ack_rows=0)
    out = _run(encode_ack_token([_ITEM_A], ME) or "", store)

    assert out.error == "store_failed"
    assert out.acked == 0
    assert out.undo_token == ""
    assert "確認済みにしました" not in out.message


# ── unack ────────────────────────────────────────────────────────────────


def test_unack_removes_and_returns_no_undo_token() -> None:
    store = _Store(unack_rows=1)
    out = _run(encode_unack_token([_ITEM_A], ME) or "", store)

    assert out.error == ""
    assert out.unacked == 1
    assert out.acked == 0
    assert out.undo_token == "", "取り消しの取り消しは作らない"
    assert store.unack_calls == [(ME, (AckItem("m", _ITEM_A.item_key, 0),))]
    assert store.ack_calls == []


def test_unack_zero_rows_is_reported_as_failure() -> None:
    store = _Store(unack_rows=0)
    out = _run(encode_unack_token([_ITEM_A], ME) or "", store)
    assert out.error == "store_failed"
    assert out.unacked == 0


# ── G3 ───────────────────────────────────────────────────────────────────


def test_message_leaks_no_internal_values() -> None:
    token = encode_ack_all_token([_ITEM_A, _ITEM_B], ME) or ""
    out = _run(token, _Store(ack_rows=2))

    assert _ITEM_A.item_key not in out.message
    assert _ITEM_B.item_key not in out.message
    assert token not in out.message
    assert ME not in out.message
