"""実際の history / replies 応答からリアクション一覧を返す回帰テスト。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from teamagent.adapters.slack_user_reader import SlackThreadRead
from teamagent.skills.base import SkillContext
from teamagent.skills.slack_summary.period import JST, resolve_period
from teamagent.skills.slack_summary.reactions import reaction_terms, wants_reactions
from teamagent.skills.slack_summary.schema import SlackSummaryInput
from tests.skills.slack_summary.test_slack_summary import (
    ORIGIN_CH,
    ORIGIN_TS,
    OTHER_CH,
    _channel_msg,
    _run,
)
from tests.skills.slack_summary.test_slack_summary import (
    _build as _build_summary,
)

UID_A = "U12345678"
UID_B = "U23456789"
FOCUS = "スタンプを押した人の一覧"


@pytest.fixture(autouse=True)
def _fixed_period(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "teamagent.skills.slack_summary.skill.resolve_period",
        lambda period: resolve_period(period, now=datetime(2025, 8, 18, 12, tzinfo=JST)),
    )


def _build(*args: Any, **kwargs: Any) -> Any:
    skill, factory, bed, store = _build_summary(*args, **kwargs)
    factory._client.search_messages = AsyncMock(
        return_value={"ok": True, "messages": {"matches": [], "total": 0}}
    )
    return skill, factory, bed, store


def _post(text: str = "募集の投稿", **kw: Any) -> dict[str, Any]:
    return {
        **_channel_msg(ORIGIN_TS, UID_A, text),
        "reactions": [{"name": "thumbsup", "users": [UID_A, UID_B], "count": 2}],
        **kw,
    }


def _names(client: MagicMock) -> None:
    async def info(*, user: str) -> dict[str, Any]:
        return {
            "ok": True,
            "user": {
                "profile": {"display_name": "架空メンバーA" if user == UID_A else "架空メンバーB"},
                "is_restricted": user == UID_B,
            },
        }

    client.users_info = AsyncMock(side_effect=info)


@pytest.mark.parametrize("stamp", ["スタンプ", "リアクション"])
@pytest.mark.parametrize(
    "action",
    [
        "押した",
        "押してる人",
        "つけた",
        "付けた",
        "反応した",
        "誰",
        "した人",
        "ユーザー",
        "一覧",
        "リスト",
    ],
)
def test_wants_reactions(stamp: str, action: str) -> None:
    assert wants_reactions(stamp + action)


@pytest.mark.parametrize(
    "intent",
    [
        "このチャンネルを要約して",
        "スタンプの意味",
        "募集した人の一覧",
        "リアクションが多い人気の投稿を要約して",
        "絵文字なしで一覧にして",
        "スタンプラリーの投稿を一覧にして",
        "リアクション動画の事例を一覧で見せて",
    ],
)
def test_does_not_want_reactions(intent: str) -> None:
    assert not wants_reactions(intent)


def test_link_lists_each_stamp_and_replies_without_bedrock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WORKSPACE_DOMAIN", "fictional.slack.com")
    parent = _post(reply_count=1)
    parent["reactions"].append({"name": "eyes", "users": [UID_A], "count": 1})
    reply = _post("返信の募集", ts="1755400050.000100", thread_ts=ORIGIN_TS)
    skill, factory, bed, _ = _build([parent, reply], history_messages=[parent])
    _names(factory._client)
    link = f"https://fictional.slack.com/archives/{ORIGIN_CH}/p1755400000100100"
    out = _run(skill, thread_ts=link, focus=FOCUS)
    assert out.error == "" and out.scope == "thread" and out.message_count == 2
    assert "*2025-08-17* 募集の投稿" in out.message and link in out.message
    assert "・:thumbsup: 2人 — 架空メンバーA、架空メンバーB（ゲスト）" in out.message
    assert "・:eyes: 1人 — 架空メンバーA" in out.message
    assert "返信の募集" in out.message
    assert not out.truncated and not bed.calls
    assert factory.tokens == ["xoxp-personal-token-of-me"]
    assert factory._client.users_info.await_count == 2
    assert factory._client.conversations_history.await_args.kwargs["oldest"] == ORIGIN_TS
    assert factory.calls == [(ORIGIN_CH, ORIGIN_TS)]
    for method in ("reactions_get", "search_messages", "chat_postMessage"):
        assert not getattr(factory._client, method).called


def test_slack_omitted_users_are_disclosed() -> None:
    post = _post(reactions=[{"name": "eyes", "users": [UID_A], "count": 4}])
    skill, factory, bed, _ = _build(history_messages=[post])
    _names(factory._client)
    out = _run(skill, scope="channel", focus=FOCUS)
    assert "ほか3人（Slack が一覧を省略）" in out.message
    assert out.truncated and out.message_count == 1 and not bed.calls


@pytest.mark.parametrize("quote", ["「募集」", "『募集』", "「募集」か『応募』"])
def test_quoted_words_filter_posts(quote: str) -> None:
    skill, factory, bed, _ = _build(
        history_messages=[_post("連絡"), _post("募集", ts="1755400050.000100")]
    )
    _names(factory._client)
    out = _run(skill, scope="channel", focus=quote + FOCUS)
    assert out.message_count == 1 and "募集" in out.message and "連絡" not in out.message
    assert not bed.calls


def test_quoted_words_with_no_match_show_all_reacted_posts_and_say_so() -> None:
    skill, factory, bed, _ = _build(
        history_messages=[_post("連絡"), _post("募集", ts="1755400050.000100")]
    )
    _names(factory._client)
    out = _run(skill, scope="channel", focus="「スタンプを押した人」" + FOCUS)
    assert out.message_count == 2 and "連絡" in out.message and "募集" in out.message
    assert "指定の語を含む投稿には無かったので" in out.message
    assert not bed.calls


@pytest.mark.parametrize("posts", [[], [_post(reactions=[])]])
def test_no_reactions_is_success(posts: list[dict[str, Any]]) -> None:
    skill, factory, bed, _ = _build(history_messages=posts)
    out = _run(skill, scope="channel", focus=FOCUS)
    assert "スタンプの付いた投稿は見つかりませんでした。" in out.message
    assert out.message.startswith("対象期間: 直近90日")
    assert out.message.endswith("ゲスト 0 人・社員 0 人")
    assert out.message_count == 0 and out.error == "" and not bed.calls
    assert not factory._client.users_info.called


def test_cross_channel_guard_precedes_reads() -> None:
    skill, factory, bed, _ = _build(history_messages=[_post()])
    out = _run(skill, channel_id=OTHER_CH, scope="channel", focus=FOCUS)
    assert out.error == "cross_channel_blocked"
    assert not factory.tokens and not bed.calls
    assert not factory._client.conversations_history.called


def test_not_connected_is_unchanged() -> None:
    skill, factory, bed, _ = _build(tok=None)
    out = _run(skill, scope="channel", focus=FOCUS)
    assert out.error == "not_connected" and "連携" in out.message
    assert not factory.tokens and not bed.calls


def test_request_text_also_selects_reactions() -> None:
    skill, factory, bed, _ = _build(history_messages=[_post()])
    _names(factory._client)
    out = skill.run(
        SlackSummaryInput(scope="channel"),
        SkillContext(
            metadata={
                "user_email": "me@example.test",
                "channel_id": ORIGIN_CH,
                "request_text": FOCUS,
            }
        ),
    )
    assert out.message_count == 1 and not bed.calls


def test_output_neutralizes_names_body_and_stamp_and_missing_names() -> None:
    post = _post(
        f"募集 <@{UID_A}> `{UID_B}` <!here>\n第二行は省略",
        reactions=[{"name": f"eyes<@{UID_A}>", "users": [UID_A, UID_B], "count": 2}],
    )
    skill, factory, bed, _ = _build(history_messages=[post])
    factory._client.users_info = AsyncMock(
        side_effect=[
            {"ok": True, "user": {"profile": {"display_name": f"<@架空A> `{UID_A}`"}}},
            {"ok": False, "error": "user_not_found"},
        ]
    )
    out = _run(skill, scope="channel", focus=FOCUS)
    assert "（名前を取得できない人）" in out.message
    assert "‹＠架空A›" in out.message
    assert "第二行" not in out.message
    for forbidden in ("<@", "<!here>", UID_A, UID_B):
        assert forbidden not in out.message
    assert not bed.calls


def test_plain_capital_words_are_not_masked_as_user_ids() -> None:
    skill, factory, bed, _ = _build(history_messages=[_post("UGC募集 WEB版 U2")])
    _names(factory._client)
    out = _run(skill, scope="channel", focus=FOCUS)
    assert "UGC募集 WEB版 U2" in out.message and not bed.calls


def test_post_cap_and_title_length() -> None:
    posts = [
        _post("あ" * 50, ts=f"1755400{i:03}.000100", reactions=[{"name": "eyes", "users": [UID_A]}])
        for i in range(21)
    ]
    skill, factory, bed, _ = _build(history_messages=posts)
    _names(factory._client)
    out = _run(skill, scope="channel", focus=FOCUS)
    assert out.message_count == 20 and out.truncated
    assert "上限のため一部だけ表示" in out.message
    assert out.message.count("上限のため一部だけ表示") == 1
    assert "あ" * 40 in out.message and "あ" * 41 not in out.message
    assert factory._client.users_info.await_count == 1 and not bed.calls


@pytest.mark.parametrize("unique", ["", "ユニーク"])
def test_user_cap_is_shared_across_posts_and_stamps(unique: str) -> None:
    users = [f"U{i:08}" for i in range(201)]
    posts = [
        _post(reactions=[{"name": "eyes", "users": users[:150], "count": 150}]),
        _post(ts="1755400050.000100", reactions=[{"name": "wave", "users": users, "count": 201}]),
    ]
    skill, factory, bed, _ = _build(history_messages=posts)
    _names(factory._client)
    out = _run(skill, scope="channel", focus=FOCUS + unique)
    assert out.truncated and out.message_count == 2
    assert "上限のため一部だけ表示" in out.message
    assert factory._client.users_info.await_count == 200 and not bed.calls
    assert {call.kwargs["user"] for call in factory._client.users_info.await_args_list} == set(
        users[:200]
    )


def test_reaction_log_contains_counts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    log = MagicMock()
    monkeypatch.setattr("teamagent.skills.slack_summary.reactions.logger", log)
    skill, factory, _, _ = _build(history_messages=[_post()])
    _names(factory._client)
    _run(skill, scope="channel", focus=FOCUS)
    call = next(c for c in log.info.call_args_list if c.args == ("slack_summary_reactions",))
    assert set(call.kwargs) == {
        "request_id",
        "read",
        "reacted",
        "terms",
        "widened",
        "messages",
        "stamp_lines",
        "users",
        "truncated",
        "latency_ms",
    }
    assert call.kwargs["messages"] == 1 and call.kwargs["users"] == 2


def test_named_period_gate_is_not_bypassed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_SUMMARY_NAMED_PERIOD_ENABLED", raising=False)
    skill, factory, bed, _ = _build(history_messages=[_post()])
    out = _run(skill, channel_name="fictional", period="今週", focus=FOCUS)
    assert out.error == "feature_disabled"
    assert not factory.tokens and not bed.calls


def test_named_period_reuses_existing_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters.slack_channel_ingest_client import _message_from_raw
    from teamagent.adapters.slack_user_reader import SlackChannelResolution
    from teamagent.skills.slack_summary import skill as mod

    monkeypatch.setenv("SLACK_SUMMARY_NAMED_PERIOD_ENABLED", "1")
    skill, factory, bed, _ = _build(history_messages=[_post()])
    _names(factory._client)
    monkeypatch.setattr(
        "teamagent.adapters.slack_user_reader.SlackUserReader.resolve_channel_checked",
        lambda *args: SlackChannelResolution(channel_id=ORIGIN_CH, is_public=True),
    )
    read = MagicMock(return_value=SlackThreadRead(messages=(_message_from_raw(_post()),)))
    monkeypatch.setattr(mod, "_read_channel_period", read)
    out = _run(skill, channel_name="fictional", period="今週", focus=FOCUS)
    assert out.message_count == 1 and out.scope == "channel" and not bed.calls
    assert read.call_count == 1
    assert read.call_args.args[1:3] == (ORIGIN_CH, "r")


def test_link_does_not_fall_back_to_channel_or_ignore_explicit_post() -> None:
    skill, factory, bed, _ = _build(history_messages=[_post()])
    _names(factory._client)
    out = _run(skill, scope="channel", thread_ts=ORIGIN_TS, focus=FOCUS)
    assert out.message_count == 1 and out.scope == "thread" and not bed.calls
    assert factory.channel_calls == [] and factory.calls == []
    factory._client.conversations_history.assert_awaited_once()
    assert factory._client.conversations_history.await_args.kwargs["latest"] == ORIGIN_TS


@pytest.mark.parametrize("code", ["not_in_channel", "channel_not_found", "message_not_found"])
def test_link_read_denied_is_uniform(code: str) -> None:
    skill, factory, bed, _ = _build(history_error=code)
    out = _run(skill, thread_ts=ORIGIN_TS, focus=FOCUS)
    assert out.error == "not_found"
    assert out.message == "チャンネルが見つからないかアクセス権がありません。"
    assert not factory._client.users_info.called and not bed.calls


def test_link_without_reactions_does_not_use_other_channel_posts() -> None:
    skill, factory, bed, _ = _build(history_messages=[_post(reactions=[])])
    out = _run(skill, thread_ts=ORIGIN_TS, focus=FOCUS)
    assert "スタンプの付いた投稿は見つかりませんでした。" in out.message
    assert not out.error and not bed.calls and not factory.channel_calls


@pytest.mark.parametrize(
    "phrase",
    [
        "含む文字列：**募集**",
        "文字列:募集",
        "文字列募集",
        "募集 を含む投稿",
        "募集を含むメッセージ",
        "**募集** を含むメッセージ",
    ],
)
def test_unquoted_words_filter_posts(phrase: str) -> None:
    skill, factory, _, _ = _build(
        history_messages=[_post("連絡"), _post("募集", ts="1755400050.000100")]
    )
    _names(factory._client)
    out = _run(skill, scope="channel", focus=FOCUS + "\n" + phrase)
    assert out.message_count == 1 and "募集" in out.message and "連絡" not in out.message


def test_unquoted_no_match_still_widens() -> None:
    skill, factory, _, _ = _build(history_messages=[_post("連絡")])
    _names(factory._client)
    out = _run(skill, scope="channel", focus=FOCUS + "\n文字列:募集")
    assert out.message_count == 1 and "連絡" in out.message
    assert "指定の語を含む投稿には無かったので" in out.message


def test_terms_strip_decorations_and_keep_labelled_spaces() -> None:
    assert reaction_terms("メッセージに含む文字列：**制作 パートナー募集**") == [
        "制作 パートナー募集"
    ]


def test_default_period_uses_90_days_and_existing_read_cap() -> None:
    skill, factory, _, _ = _build(history_messages=[_post()])
    _names(factory._client)
    out = _run(skill, scope="channel", focus=FOCUS)
    args = factory._client.conversations_history.await_args.kwargs
    assert (args["oldest"], args["latest"]) == resolve_period(
        "直近90日", now=datetime(2025, 8, 18, 12, tzinfo=JST)
    )
    assert out.message.startswith("対象期間: 直近90日（2025-05-20 00:00")
    assert "ゲスト 1 人・社員 1 人" in out.message and not out.truncated


@pytest.mark.parametrize("word", ["ユニーク", "重複なし", "重複を除く", "一意", "列挙"])
def test_unique_users_count_posts_once_across_stamps(word: str) -> None:
    first = _post()
    first["reactions"].append({"name": "eyes", "users": [UID_B], "count": 1})
    second = _post(ts="1755400050.000100")
    skill, factory, bed, _ = _build(history_messages=[first, second])
    _names(factory._client)
    out = _run(skill, scope="channel", focus=FOCUS + word)
    assert "架空メンバーB（ゲスト） — 2 投稿\n架空メンバーA — 2 投稿" in out.message
    assert out.message.count("架空メンバーB") == 1 and ":eyes:" not in out.message
    assert "募集の投稿" not in out.message and out.message_count == 2
    assert out.message.endswith("ゲスト 1 人・社員 1 人") and not bed.calls


@pytest.mark.parametrize("unique", ["", "ユニーク"])
@pytest.mark.parametrize("word", ["マルチチャンネルゲスト", "ゲストだけ", "ゲストのみ"])
def test_guest_only_and_omitted_users_do_not_inflate_totals(unique: str, word: str) -> None:
    post = _post(
        reactions=[
            {"name": "eyes", "users": [UID_A, UID_B], "count": 5},
            {"name": "wave", "count": 7},
        ]
    )
    skill, factory, _, _ = _build(history_messages=[post])
    _names(factory._client)
    out = _run(skill, scope="channel", focus=FOCUS + unique + word)
    assert "架空メンバーB（ゲスト）" in out.message and "架空メンバーA" not in out.message
    assert out.message.endswith("ゲスト 1 人") and "社員" not in out.message
    assert "ほか N 人）は人数に含めていません" in out.message and out.truncated
    if not unique:
        assert "ほか3人" in out.message and "ほか7人" in out.message


def test_unique_reads_beyond_per_post_display_cap_and_sorts_names() -> None:
    posts = [
        _post(ts=f"1755400{i:03}.000100", reactions=[{"name": "eyes", "users": [UID_A]}])
        for i in range(21)
    ]
    posts[-1]["reactions"][0]["users"] = [UID_B]
    skill, factory, _, _ = _build(history_messages=posts)
    _names(factory._client)
    out = _run(skill, scope="channel", focus=FOCUS + "ユニーク")
    assert "架空メンバーB（ゲスト） — 1 投稿\n架空メンバーA — 20 投稿" in out.message
    assert not out.truncated and out.message_count == 21


def test_unique_sorts_each_group_by_name() -> None:
    labels = {
        "U12345678": ("社員B", False),
        "U23456789": ("ゲストB", True),
        "U34567890": ("社員A", False),
        "U45678901": ("ゲストA", True),
    }
    skill, factory, _, _ = _build(
        history_messages=[_post(reactions=[{"name": "eyes", "users": list(labels), "count": 4}])]
    )

    async def info(*, user: str) -> dict[str, Any]:
        name, guest = labels[user]
        return {"ok": True, "user": {"profile": {"display_name": name}, "is_restricted": guest}}

    factory._client.users_info = AsyncMock(side_effect=info)
    out = _run(skill, scope="channel", focus=FOCUS + "ユニーク")
    assert out.message.splitlines()[1:] == [
        "ゲストA（ゲスト） — 1 投稿",
        "ゲストB（ゲスト） — 1 投稿",
        "社員A — 1 投稿",
        "社員B — 1 投稿",
        "ゲスト 2 人・社員 2 人",
    ]


@pytest.mark.parametrize("unique", ["", "ユニーク"])
def test_omitted_all_users_are_not_counted_as_employees(unique: str) -> None:
    skill, factory, _, _ = _build(
        history_messages=[_post(reactions=[{"name": "eyes", "count": 5}])]
    )
    out = _run(skill, scope="channel", focus=FOCUS + unique)
    assert out.message.endswith("ゲスト 0 人・社員 0 人") and out.truncated
    assert not factory._client.users_info.called
