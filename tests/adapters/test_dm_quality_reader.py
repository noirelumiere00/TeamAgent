from datetime import UTC, datetime
from typing import Any

import pytest

START = datetime(2026, 10, 14, tzinfo=UTC)
END = datetime(2026, 10, 21, tzinfo=UTC)
USER = "U0123456789"
TEAM = "T0123456789"
BOT = "B0123456789"


def message(offset: int, *, bot: bool = False, **extra: Any) -> dict[str, Any]:
    return {
        "ts": f"{int(START.timestamp()) + offset}.000001",
        "user": "U0AICOBOT01" if bot else USER,
        "text": "合成回答" if bot else "合成依頼",
        **({"bot_id": BOT} if bot else {}),
        **extra,
    }


class ReadOnlySlack:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        parent = message(-100, reply_count=202, latest_reply=message(3)["ts"])
        self.history = [parent, message(1), message(2, bot=True), message(4)]
        self.fail_history = False

    def __getattr__(self, name: str) -> Any:
        assert name in {
            "auth_test",
            "conversations_list",
            "conversations_history",
            "conversations_replies",
        }, f"Slack write or unapproved API: {name}"

        def call(**kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, kwargs))
            if name == "auth_test":
                return {"ok": True, "team_id": TEAM, "bot_id": BOT, "user_id": "U0AICOBOT01"}
            if name == "conversations_list":
                return {"ok": True, "channels": [{"id": "D0123456789", "user": USER}]}
            if name == "conversations_history":
                if self.fail_history:
                    raise RuntimeError("sensitive error text must not escape")
                return {"ok": True, "messages": self.history}
            parent = self.history[0]["ts"]
            if not kwargs.get("cursor"):
                return {
                    "ok": True,
                    "messages": [message(3, thread_ts=parent)],
                    "has_more": True,
                    "response_metadata": {"next_cursor": "next"},
                }
            return {"ok": True, "messages": [message(5, bot=True, thread_ts=parent)]}

        return call


def extract(slack: ReadOnlySlack) -> Any:
    from teamagent.adapters.dm_quality_reader import DmQualityReader

    return DmQualityReader(slack).extract(
        start=START, end=END, observed_until=END, expected_team=TEAM
    )


def test_reads_old_parent_and_all_reply_pages_without_slack_writes() -> None:
    slack = ReadOnlySlack()
    result = extract(slack)
    assert result.failures == []
    assert len(result.exchanges) == 3
    assert sorted(len(x.answers) for x in result.exchanges) == [0, 1, 1]
    assert len([c for c in slack.calls if c[0] == "conversations_replies"]) == 2
    assert all(
        c[0]
        in {"auth_test", "conversations_list", "conversations_history", "conversations_replies"}
        for c in slack.calls
    )
    assert "conversations_open" not in {c[0] for c in slack.calls}


def test_partial_slack_failure_is_recorded_without_sensitive_exception() -> None:
    slack = ReadOnlySlack()
    slack.fail_history = True
    result = extract(slack)
    assert result.failures
    assert "sensitive" not in str(result)


def test_wrong_workspace_fails_closed() -> None:
    from teamagent.adapters.dm_quality_reader import DmQualityError, DmQualityReader

    with pytest.raises(DmQualityError, match="workspace_mismatch"):
        DmQualityReader(ReadOnlySlack()).extract(
            start=START, end=END, observed_until=END, expected_team="T0999999999"
        )


def test_repeated_messages_are_not_double_counted() -> None:
    slack = ReadOnlySlack()
    slack.history += [message(1), message(2, bot=True)]
    assert len(extract(slack).exchanges) == 3


def test_keyless_inputs_stay_distinct_and_replay_stably() -> None:
    first = extract(ReadOnlySlack()).exchanges
    replay = extract(ReadOnlySlack()).exchanges
    assert len({x.input_id for x in first}) == len(first)
    assert [x.input_id for x in first] == [x.input_id for x in replay]
    assert all(len(x.input_id) == 64 and USER not in x.input_id for x in first)


def test_writer_is_rejected_before_it_reaches_the_sdk() -> None:
    from teamagent.adapters.dm_quality_reader import DmQualityError, DmQualityReader

    slack = ReadOnlySlack()
    with pytest.raises(DmQualityError, match="slack_write_forbidden"):
        DmQualityReader(slack)._call("chat_postMessage", text="never sent")
    assert not slack.calls


def test_has_more_without_cursor_is_missing_not_success() -> None:
    class Broken(ReadOnlySlack):
        def conversations_history(self, **kwargs):
            return {"ok": True, "messages": [], "has_more": True}

    assert extract(Broken()).failures


def test_all_answers_are_kept_and_feedback_messages_are_excluded() -> None:
    slack = ReadOnlySlack()
    slack.history = [message(1)] + [message(i, bot=True) for i in range(2, 12)]
    slack.history.append(message(12, bot=True, text="ありがとうございます（👍 を記録しました）"))
    assert len(extract(slack).exchanges[0].answers) == 10


def test_followup_keeps_context_without_mutating_the_prior_answer() -> None:
    slack = ReadOnlySlack()
    slack.history = [
        message(1, text="資料を探して"),
        message(2, bot=True, text="確認です"),
        message(3, text="はい"),
        message(4, bot=True, text="回答"),
    ]
    exchanges = extract(slack).exchanges
    assert exchanges[1].context == [
        {"question": "資料を探して", "answers": ["確認です"], "files": []}
    ]


def test_rate_limit_uses_retry_after_without_any_write(monkeypatch) -> None:
    from types import SimpleNamespace

    from slack_sdk.errors import SlackApiError

    from teamagent.adapters.dm_quality_reader import DmQualityReader

    sleeps = []
    monkeypatch.setattr("teamagent.adapters.dm_quality_reader.time.sleep", sleeps.append)

    class RateLimited:
        calls = 0

        def auth_test(self):
            self.calls += 1
            if self.calls == 1:
                raise SlackApiError(
                    "not logged", SimpleNamespace(status_code=429, headers={"Retry-After": "2"})
                )
            return {"ok": True}

    assert DmQualityReader(RateLimited())._call("auth_test") == {"ok": True}
    assert sleeps == [2]
