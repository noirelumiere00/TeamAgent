"""SlackUnrepliedProvider（Slack 返信漏れ検知・v0.3 Task1）の単体テスト。

外部 I/O 無し（store / reader ともフェイク）。判定ロジックの境界を攻める:
未連携・scope欠落・未返信検出・返信済み除外・自己発言スキップ・root去重・
permalink の thread_ts 解決・API失敗時の skip（証拠なしに未返信と言わない）・上限。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from teamagent.adapters.slack_channel_ingest_client import SlackMessage
from teamagent.adapters.slack_user_reader import SlackSearchMatch
from teamagent.skills._shared.slack_unreplied import SlackUnrepliedProvider

_UID = "U_ME"


@dataclass
class _Tok:
    access_token: str = "xoxp-test"
    scopes: tuple[str, ...] = ("search:read", "channels:history")
    slack_user_id: str = _UID


class _Store:
    def __init__(self, tok: Any) -> None:
        self._tok = tok

    def get(self, user_email: str) -> Any:
        return self._tok


@dataclass
class _FakeReader:
    matches: list[SlackSearchMatch] = field(default_factory=list)
    threads: dict[tuple[str, str], list[SlackMessage]] = field(default_factory=dict)
    search_queries: list[str] = field(default_factory=list)
    thread_calls: list[tuple[str, str]] = field(default_factory=list)

    def search(self, query: str, request_id: str, *, count: int = 15) -> list[SlackSearchMatch]:
        self.search_queries.append(query)
        return self.matches[:count]

    def read_thread(
        self, channel_id: str, thread_ts: str, request_id: str, *, limit: int = 200
    ) -> list[SlackMessage]:
        self.thread_calls.append((channel_id, thread_ts))
        return self.threads.get((channel_id, thread_ts), [])


def _provider(reader: _FakeReader, tok: Any = None, **kw: Any) -> SlackUnrepliedProvider:
    return SlackUnrepliedProvider(
        slack_store=_Store(tok if tok is not None else _Tok()),
        reader_factory=lambda _xoxp: reader,
        **kw,
    )


def _match(
    ts: str = "1000.1", ch: str = "C1", user: str = "U_OTHER", **kw: Any
) -> SlackSearchMatch:
    return SlackSearchMatch(
        ts=ts,
        text=kw.get("text", f"<@{_UID}> 確認お願いします"),
        channel_id=ch,
        channel_name=kw.get("channel_name", "sales-acme"),
        user=user,
        permalink=kw.get("permalink", f"https://x.slack.com/archives/{ch}/p{ts.replace('.', '')}"),
    )


def _msg(ts: str, user: str | None) -> SlackMessage:
    return SlackMessage(ts=ts, user=user, text="…")


def test_no_token_returns_empty() -> None:
    p = _provider(_FakeReader(), tok=None)
    p._store = _Store(None)  # 未連携
    assert p.collect("a@b.co", 7, "r1") == []


def test_missing_search_scope_returns_empty() -> None:
    reader = _FakeReader(matches=[_match()])
    p = _provider(reader, tok=_Tok(scopes=("channels:history",)))  # 旧スコープ連携
    assert p.collect("a@b.co", 7, "r1") == []
    assert reader.search_queries == []  # API を叩かない


def test_unreplied_mention_detected_and_mapped() -> None:
    reader = _FakeReader(
        matches=[_match(ts="1000.1")],
        threads={("C1", "1000.1"): [_msg("1000.1", "U_OTHER")]},  # 自分の返信なし
    )
    out = _provider(reader).collect("a@b.co", 7, "r1")
    assert len(out) == 1
    m = out[0]
    assert m.channel_name == "sales-acme"
    assert m.permalink.startswith("https://")
    assert m.occurred_at.startswith("1970-01-01T09:16")  # epoch1000秒 = JST 09:16:40
    # 検索クエリは本人メンション＋期間絞り。after: は排他的（日付単位）なので
    # horizon ちょうど前の日を含めるため horizon+1 日遡った日付になる。
    from datetime import datetime, timedelta, timezone

    q = reader.search_queries[0]
    assert f"<@{_UID}>" in q
    expected = (
        (datetime.now(tz=timezone(timedelta(hours=9))) - timedelta(days=8)).date().isoformat()
    )
    assert f"after:{expected}" in q


def test_replied_after_mention_excluded() -> None:
    reader = _FakeReader(
        matches=[_match(ts="1000.1")],
        threads={("C1", "1000.1"): [_msg("1000.1", "U_OTHER"), _msg("1500.0", _UID)]},
    )
    assert _provider(reader).collect("a@b.co", 7, "r1") == []


def test_reply_before_mention_does_not_count() -> None:
    # メンションより前の自分の発言は「返信」ではない（同スレで過去に発言済みでも検知する）。
    reader = _FakeReader(
        matches=[_match(ts="1000.1")],
        threads={("C1", "1000.1"): [_msg("900.0", _UID), _msg("1000.1", "U_OTHER")]},
    )
    assert len(_provider(reader).collect("a@b.co", 7, "r1")) == 1


def test_self_authored_match_skipped_without_thread_check() -> None:
    reader = _FakeReader(matches=[_match(user=_UID)])
    assert _provider(reader).collect("a@b.co", 7, "r1") == []
    assert reader.thread_calls == []  # replies を浪費しない


def test_dedup_by_thread_root() -> None:
    # 同一スレッドの2メンション → replies 照会は1回・結果も1件。
    pl = "https://x.slack.com/archives/C1/p10002?thread_ts=1000.1&cid=C1"
    reader = _FakeReader(
        matches=[_match(ts="1000.2", permalink=pl), _match(ts="1000.3", permalink=pl)],
        threads={("C1", "1000.1"): [_msg("1000.1", "U_OTHER")]},
    )
    out = _provider(reader).collect("a@b.co", 7, "r1")
    assert len(out) == 1
    assert reader.thread_calls == [("C1", "1000.1")]  # permalink の thread_ts が root


def test_thread_read_failure_skips_item() -> None:
    # replies が空（fail-open＝API失敗）→ 判定不能なので「未返信」と主張しない。
    reader = _FakeReader(matches=[_match(ts="1000.1")], threads={})
    assert _provider(reader).collect("a@b.co", 7, "r1") == []


def test_max_items_cap() -> None:
    matches = [_match(ts=f"{1000 + i}.1", ch=f"C{i}") for i in range(8)]
    threads = {(f"C{i}", f"{1000 + i}.1"): [_msg(f"{1000 + i}.1", "U_OTHER")] for i in range(8)}
    out = _provider(_FakeReader(matches=matches, threads=threads), max_items=3).collect(
        "a@b.co", 7, "r1"
    )
    assert len(out) == 3


def test_max_thread_checks_cap() -> None:
    matches = [_match(ts=f"{1000 + i}.1", ch=f"C{i}") for i in range(8)]
    reader = _FakeReader(matches=matches, threads={})  # 全部判定不能でも
    _provider(reader, max_thread_checks=4).collect("a@b.co", 7, "r1")
    assert len(reader.thread_calls) == 4  # replies 呼び出しは上限で打ち切り


def test_store_failure_fail_open() -> None:
    class _BoomStore:
        def get(self, user_email: str) -> Any:
            raise RuntimeError("db down")

    p = SlackUnrepliedProvider(slack_store=_BoomStore(), reader_factory=lambda _x: _FakeReader())
    assert p.collect("a@b.co", 7, "r1") == []
