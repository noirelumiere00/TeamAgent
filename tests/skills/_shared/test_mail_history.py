"""同じ相手との「別スレッド」過去メール取得（_shared/mail_history）のテスト。

Gmail は fake。**クエリに載せる相手アドレスの検証**（From ヘッダ由来の演算子注入遮断）と、
fail-open・スレッド重複排除・返信元スレッド除外を固定する。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from teamagent.skills._shared.mail_history import (
    counterpart_history_section,
    counterpart_query,
    fetch_counterpart_history,
)

OWNER = "s-komata@vectorinc.co.jp"


def _payload(text: str) -> dict[str, Any]:
    data = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
    return {"mimeType": "text/plain", "body": {"data": data}}


@dataclass
class _Ref:
    id: str
    thread_id: str


@dataclass
class _Msg:
    id: str
    thread_id: str
    headers: dict[str, str]
    payload: dict[str, Any]
    internal_date_ms: int = 1_700_000_000_000


@dataclass
class _Ctx:
    request_id: str = "r-test"


@dataclass
class FakeGmail:
    msgs: list[_Msg] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    fail_search: bool = False
    fail_fetch: bool = False

    def list_messages(
        self, query: str | None, request_id: str, *, max_results: int = 50, **kw: Any
    ) -> tuple[list[_Ref], None]:
        self.queries.append(query or "")
        if self.fail_search:
            raise RuntimeError("boom")
        return ([_Ref(id=m.id, thread_id=m.thread_id) for m in self.msgs][:max_results], None)

    def get_message(self, msg_id: str, request_id: str, **kw: Any) -> _Msg:
        if self.fail_fetch:
            raise RuntimeError("boom")
        return next(m for m in self.msgs if m.id == msg_id)


def _msg(mid: str, thread_id: str, sender: str, body: str) -> _Msg:
    return _Msg(
        id=mid,
        thread_id=thread_id,
        headers={"From": sender, "Subject": "件名", "To": OWNER},
        payload=_payload(body),
    )


# ── クエリ検証（From ヘッダは攻撃者が自由に書ける）────────────────────────


def test_query_is_built_for_a_plain_address() -> None:
    query = counterpart_query("tanaka@example.co.jp", lookback_days=90)
    assert (
        query == "(from:tanaka@example.co.jp OR to:tanaka@example.co.jp) newer_than:90d -in:chats"
    )


def test_query_refuses_anything_that_could_carry_an_operator() -> None:
    """`from:` を持ち込める形は 1 つも通さない（通ると他人のメールを引かれる）。"""
    for hostile in (
        'x@y.co" OR from:ceo@corp.com "',
        "x@y.co newer_than:1d",
        "x@y.co OR from:ceo@corp.com",
        "in:anywhere",
        "",
        "notanemail",
        "x@y",  # TLD なし
    ):
        assert counterpart_query(hostile) is None, hostile


def test_query_tolerates_surrounding_whitespace_only() -> None:
    assert counterpart_query("  tanaka@example.co.jp  ") is not None


def test_query_days_are_clamped() -> None:
    assert "newer_than:1d" in str(counterpart_query("a@b.co", lookback_days=0))
    assert "newer_than:365d" in str(counterpart_query("a@b.co", lookback_days=9999))


# ── 取得 ───────────────────────────────────────────────────────────────


def test_fetch_excludes_the_current_thread_and_dedupes_threads() -> None:
    gmail = FakeGmail(
        msgs=[
            _msg("m-now", "t-now", "田中 <tanaka@example.co.jp>", "いまのスレッドの本文"),
            _msg("m-old1", "t-old", "田中 <tanaka@example.co.jp>", "前回は単価50万円でした"),
            _msg("m-old2", "t-old", "田中 <tanaka@example.co.jp>", "同じスレッドの2通目"),
            _msg("m-other", "t-other", "田中 <tanaka@example.co.jp>", "別件のご相談です"),
        ]
    )

    history = fetch_counterpart_history(
        gmail, "tanaka@example.co.jp", OWNER, _Ctx(), exclude_thread_id="t-now"
    )

    assert "いまのスレッドの本文" not in history  # 返信元は thread 履歴と二重に入れない
    assert "前回は単価50万円でした" in history
    assert "別件のご相談です" in history
    assert "同じスレッドの2通目" not in history  # 1 スレッド 1 通（深さより広さ）


def test_fetch_is_fail_open_when_search_or_fetch_breaks() -> None:
    msgs = [_msg("m1", "t1", "田中 <tanaka@example.co.jp>", "本文")]
    assert (
        fetch_counterpart_history(
            FakeGmail(msgs=msgs, fail_search=True), "tanaka@example.co.jp", OWNER, _Ctx()
        )
        == ""
    )
    assert (
        fetch_counterpart_history(
            FakeGmail(msgs=msgs, fail_fetch=True), "tanaka@example.co.jp", OWNER, _Ctx()
        )
        == ""
    )


def test_fetch_never_searches_with_an_unusable_address() -> None:
    gmail = FakeGmail(msgs=[_msg("m1", "t1", "x", "本文")])

    history = fetch_counterpart_history(gmail, 'x@y.co" OR from:ceo@corp.com "', OWNER, _Ctx())

    assert history == ""
    assert gmail.queries == []  # Gmail を 1 度も叩かない


def test_section_is_empty_when_history_is_empty() -> None:
    assert counterpart_history_section("") == ""
    assert counterpart_history_section("本文").startswith("# 同じ相手との過去のやり取り")
