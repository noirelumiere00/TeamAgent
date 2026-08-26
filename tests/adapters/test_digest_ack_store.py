"""DigestAckStore の DB 非依存テスト。"""

from __future__ import annotations

import re
from typing import Any

import psycopg

from teamagent.adapters.digest_ack_store import DigestAckStore
from teamagent.skills.morning_digest.ack_token import AckItem

_EMAIL = "me@example.com"
# 鍵は本物の導出を通す（16 桁の hex をベタ書きすると gitleaks の generic-api-key に
# 引っかかるうえ、テストが検証している鍵が本番の鍵と別物になる）。
_ITEM = AckItem(
    item_kind="m",
    item_key=DigestAckStore.item_key("m", _EMAIL, "thread-1"),
    anchor=42,
)


class _FailingPg:
    """接続確立時に psycopg と同じ例外を投げる DB 障害フェイク。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def connection(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        raise psycopg.OperationalError("connection refused")


def test_item_key_is_deterministic_scoped_by_normalized_email_and_hex() -> None:
    first = DigestAckStore.item_key("m", " Me@Example.COM ", "gmail-thread-123")
    same = DigestAckStore.item_key("m", "me@example.com", "gmail-thread-123")
    other_user = DigestAckStore.item_key("m", "other@example.com", "gmail-thread-123")

    assert first == same
    assert first != other_user
    assert re.fullmatch(r"[0-9a-f]{16}", first)


def test_active_and_purge_fail_open_on_psycopg_connection_error() -> None:
    pg = _FailingPg()
    store = DigestAckStore(pg)

    assert store.active(" Me@Example.COM ", request_id="read") == {}
    assert store.purge_expired(" Me@Example.COM ", request_id="purge") == 0
    assert pg.calls == [
        {"app_role": "teamagent_app", "user_email": _EMAIL},
        {"app_role": "teamagent_app", "user_email": _EMAIL},
    ]


def test_ack_and_unack_report_zero_on_psycopg_connection_error() -> None:
    pg = _FailingPg()
    store = DigestAckStore(pg)

    assert store.ack(_EMAIL, [_ITEM], request_id="ack") == 0
    assert store.unack(_EMAIL, [_ITEM], request_id="unack") == 0
    assert pg.calls == [
        {"app_role": "teamagent_app", "user_email": _EMAIL},
        {"app_role": "teamagent_app", "user_email": _EMAIL},
    ]
