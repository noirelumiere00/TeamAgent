"""GmailClient.delete_draft の物理ガード（TeamAgent 製の下書きだけ・denylist は据え置き）。

設計:
- ``users.drafts.delete`` は denylist に **残す**（生 Resource 経由は従来どおり RuntimeError）。
- :meth:`GmailClient.delete_draft` だけが drafts.get で目印ヘッダ
  :data:`TEAMAGENT_DRAFT_HEADER` を確認した上で :meth:`_GmailSafePolicy.armed` により
  その呼び出しの間だけ通す。目印が無ければ :class:`DraftNotOwnedError` で削除しない。
- 送信系は armed でも開かない（``_GATED_METHODS`` 外は ValueError）。
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from teamagent.adapters.gmail_client import (
    TEAMAGENT_DRAFT_HEADER,
    DraftNotOwnedError,
    GmailClient,
    _build_raw_email,
    _GmailSafePolicy,
    draft_has_teamagent_marker,
)


class _Req:
    def __init__(self, resp: dict[str, Any], executed: list[str], tag: str) -> None:
        self._resp = resp
        self._executed = executed
        self._tag = tag

    def execute(self) -> dict[str, Any]:
        self._executed.append(self._tag)
        return self._resp


class _Drafts:
    def __init__(self, headers_by_id: dict[str, list[dict[str, str]]], executed: list[str]) -> None:
        self._headers = headers_by_id
        self._executed = executed

    def get(self, *, userId: str, id: str, format: str = "full") -> _Req:  # noqa: N803
        resp = {
            "id": id,
            "message": {
                "id": "msg-1",
                "threadId": "th-1",
                "payload": {"headers": self._headers[id]},
            },
        }
        return _Req(resp, self._executed, f"get:{format}")

    def delete(self, *, userId: str, id: str) -> _Req:  # noqa: N803
        return _Req({}, self._executed, "delete")


class _Users:
    def __init__(self, drafts: _Drafts) -> None:
        self._drafts = drafts

    def drafts(self) -> _Drafts:
        return self._drafts


class _Service:
    def __init__(self, headers_by_id: dict[str, list[dict[str, str]]]) -> None:
        self.executed: list[str] = []
        self._users = _Users(_Drafts(headers_by_id, self.executed))

    def users(self) -> _Users:
        return self._users


_OWNED = [{"name": "Subject", "value": "Re: x"}, {"name": TEAMAGENT_DRAFT_HEADER, "value": "1"}]
_HUMAN = [{"name": "Subject", "value": "Re: x"}]


def test_delete_draft_deletes_teamagent_made_draft_via_gated_policy() -> None:
    svc = _Service({"d-owned": _OWNED})
    client = GmailClient(service=svc)
    draft = client.delete_draft("d-owned", request_id="r")
    assert draft.id == "d-owned" and draft.thread_id == "th-1"
    assert svc.executed == ["get:metadata", "delete"]  # 確認 → 削除の順


def test_delete_draft_refuses_human_written_draft() -> None:
    svc = _Service({"d-human": _HUMAN})
    client = GmailClient(service=svc)
    with pytest.raises(DraftNotOwnedError):
        client.delete_draft("d-human", request_id="r")
    assert svc.executed == ["get:metadata"], "delete は実行されない"


def test_raw_drafts_delete_stays_blocked_before_and_after_a_gated_delete() -> None:
    svc = _Service({"d-owned": _OWNED})
    client = GmailClient(service=svc)
    with pytest.raises(RuntimeError, match=r"users\.drafts\.delete"):
        client._ensure_safe_service().users().drafts().delete(userId="me", id="d-owned").execute()
    client.delete_draft("d-owned", request_id="r")
    # armed は with を抜けたら解除される＝再び物理封鎖
    with pytest.raises(RuntimeError, match=r"users\.drafts\.delete"):
        client._ensure_safe_service().users().drafts().delete(userId="me", id="d-owned").execute()
    assert svc.executed == ["get:metadata", "delete"]


@pytest.mark.parametrize(
    "method_path",
    ["users.messages.send", "users.drafts.send", "users.messages.delete", "users.threads.trash"],
)
def test_armed_never_opens_send_or_mail_deletion(method_path: str) -> None:
    pol = _GmailSafePolicy()
    with pytest.raises(ValueError):
        with pol.armed(method_path):
            pass
    with pytest.raises(RuntimeError):
        pol.assert_safe(method_path)


def test_armed_is_scoped_to_the_with_block() -> None:
    pol = _GmailSafePolicy()
    with pol.armed("users.drafts.delete"):
        pol.assert_safe("users.drafts.delete")  # 通る
        with pytest.raises(RuntimeError):
            pol.assert_safe("users.drafts.send")  # 同時に他は開かない
    with pytest.raises(RuntimeError):
        pol.assert_safe("users.drafts.delete")


def test_build_raw_email_carries_the_teamagent_marker() -> None:
    raw_b64 = _build_raw_email(to="a@x.com", subject="s", body_text="b")
    pad = "=" * (-len(raw_b64) % 4)
    decoded = base64.urlsafe_b64decode(raw_b64 + pad).decode("utf-8", errors="replace")
    assert f"{TEAMAGENT_DRAFT_HEADER}: 1" in decoded


def test_marker_detection_is_case_insensitive_and_requires_a_value() -> None:
    assert draft_has_teamagent_marker({"x-teamagent-draft": "1"})
    assert draft_has_teamagent_marker({"X-TeamAgent-Draft": "mail_reply"})
    assert not draft_has_teamagent_marker({"X-TeamAgent-Draft": ""})
    assert not draft_has_teamagent_marker({"Subject": "Re: x"})
