"""在籍者名簿の試験（Slack は users.list の応答を再現したフェイク）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from teamagent.adapters.slack_member_directory import SlackMemberDirectory
from teamagent.personal_memory.guard import Reason, check_entry

TEAM = "T0123ABCDE"


def _member(uid: str, real: str, **flags: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": uid,
        "team_id": TEAM,
        "real_name": real,
        "profile": {"real_name": real, "display_name": "", "first_name": "", "last_name": ""},
    }
    base.update(flags)
    return base


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Slack:
    """users.list のページング応答を返す。fail=True で ok:false を返す。"""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[str | None] = []
        self.fail = False
        self.raise_error = False

    def __call__(self, cursor: str | None) -> Mapping[str, Any]:
        self.calls.append(cursor)
        if self.raise_error:
            raise ConnectionError("slack down")
        if self.fail:
            return {"ok": False, "error": "ratelimited"}
        index = 0 if cursor is None else int(cursor)
        nxt = str(index + 1) if index + 1 < len(self.pages) else ""
        return {"ok": True, "members": self.pages[index], "response_metadata": {"next_cursor": nxt}}


def test_collects_active_members_across_pages() -> None:
    slack = _Slack(
        [
            [_member("U0000000A1", "田中 太郎"), _member("U0000000B1", "Bot", is_bot=True)],
            [
                _member("U0000000C1", "佐藤　花子"),
                _member("U0000000D1", "退職 者", deleted=True),
                _member("U0000000E1", "ゲスト 太", is_restricted=True),
                _member("U0000000F1", "外部 太", is_stranger=True),
                {**_member("U0000000G1", "他社 太"), "team_id": "T0OTHERTEAM"},
            ],
        ]
    )
    directory = SlackMemberDirectory(team_id=TEAM, fetch_page=slack, clock=_Clock())
    names = directory.refresh_if_stale()
    assert {"田中", "太郎", "佐藤", "花子"} <= names
    assert not {"Bot", "退職", "ゲスト", "外部", "他社"} & names
    assert slack.calls == [None, "1"]


def test_short_tokens_are_not_used() -> None:
    slack = _Slack([[_member("U0000000A1", "林 実"), _member("U0000000B1", "Al Smith")]])
    names = SlackMemberDirectory(team_id=TEAM, fetch_page=slack, clock=_Clock()).refresh_if_stale()
    assert names == {"Smith"}


def test_cached_names_do_no_io_and_expire() -> None:
    clock = _Clock()
    slack = _Slack([[_member("U0000000A1", "田中 太郎")]])
    directory = SlackMemberDirectory(
        team_id=TEAM, fetch_page=slack, clock=clock, ttl_s=100, stale_max_s=1000
    )
    assert directory.cached_member_names() == frozenset()
    directory.refresh_if_stale()
    calls = len(slack.calls)
    clock.now = 50
    assert "田中" in directory.cached_member_names()
    directory.refresh_if_stale()  # TTL 内なので取り直さない
    assert len(slack.calls) == calls
    clock.now = 2000  # stale_max を過ぎた
    assert directory.cached_member_names() == frozenset()


@pytest.mark.parametrize("mode", ["fail", "raise"])
def test_failure_keeps_previous_until_stale_max(mode: str) -> None:
    clock = _Clock()
    slack = _Slack([[_member("U0000000A1", "田中 太郎")]])
    directory = SlackMemberDirectory(
        team_id=TEAM, fetch_page=slack, clock=clock, ttl_s=10, stale_max_s=100
    )
    directory.refresh_if_stale()
    setattr(slack, "fail" if mode == "fail" else "raise_error", True)
    clock.now = 50  # TTL 切れ → 取り直し失敗 → 前回を使う
    assert "田中" in directory.refresh_if_stale()
    clock.now = 150  # 前回の成功から stale_max を超えた → 空
    assert directory.refresh_if_stale() == frozenset()


def test_failure_backs_off_before_retrying() -> None:
    clock = _Clock()
    slack = _Slack([[_member("U0000000A1", "田中 太郎")]])
    slack.raise_error = True
    directory = SlackMemberDirectory(
        team_id=TEAM, fetch_page=slack, clock=clock, ttl_s=10, retry_s=600
    )
    assert directory.refresh_if_stale() == frozenset()
    calls = len(slack.calls)
    clock.now = 599  # 失敗から retry_s 未満は取り直さない（15 秒ごとの掃除で連打しない）
    directory.refresh_if_stale()
    assert len(slack.calls) == calls
    slack.raise_error = False
    clock.now = 600
    assert "田中" in directory.refresh_if_stale()
    assert len(slack.calls) == calls + 1


def test_truncated_listing_is_not_used() -> None:
    pages = [[_member(f"U{i:09d}", f"名前{i} 姓{i}")] for i in range(25)]
    slack = _Slack(pages)
    directory = SlackMemberDirectory(team_id=TEAM, fetch_page=slack, clock=_Clock())
    assert directory.refresh_if_stale() == frozenset()


def test_member_names_let_colleagues_through_guard_but_not_clients() -> None:
    slack = _Slack([[_member("U0000000A1", "田中 太郎")]])
    names = SlackMemberDirectory(team_id=TEAM, fetch_page=slack, clock=_Clock()).refresh_if_stale()
    assert check_entry("資料は田中さんに回す", member_names=names).ok
    rejected = check_entry("資料は山田様に確認", member_names=names)
    assert Reason.PERSON_NAME in rejected.reasons
    # 名簿が空（取得失敗・古すぎ）なら同僚名も落とす＝fail-closed
    assert Reason.PERSON_NAME in check_entry("資料は田中さんに回す", member_names=()).reasons
