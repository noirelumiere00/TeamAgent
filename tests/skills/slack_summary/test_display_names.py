"""チャンネル要約の表示名解決と、内部 ID を出力しない保険の回帰テスト。"""

from unittest.mock import AsyncMock

import pytest
from slack_sdk.errors import SlackApiError

from teamagent.skills.slack_summary.skill import _defuse_slack_pings
from tests.skills.slack_summary.test_slack_summary import (
    ORIGIN_TS,
    _build,
    _channel_msg,
    _FakeBedrock,
    _msg,
    _run,
)


@pytest.mark.parametrize(
    ("uid", "mention"),
    [
        ("U123ABC456", "<@U123ABC456>"),
        ("U123ABC456", "<@U123ABC456|古い表示名>"),
        ("W123ABC456", "<@W123ABC456>"),
        ("W123ABC456", "<@W123ABC456|古い表示名>"),
    ],
)
def test_channel_input_reuses_speaker_display_name(uid: str, mention: str) -> None:
    skill, factory, bed, _ = _build(
        history_messages=[
            _channel_msg("1755400100.000100", uid, f"{mention} と {mention} が対応します。"),
            _channel_msg("1755400200.000100", uid, "明日確認します。"),
        ]
    )
    factory._client.users_info = AsyncMock(
        return_value={"ok": True, "user": {"profile": {"display_name": "渡邊"}}}
    )
    out = _run(skill, scope="channel")
    sent = bed.calls[0]["messages"][0]["content"][0]["text"]
    assert not out.error
    assert uid not in sent
    assert "from=@渡邊" in sent
    assert "@渡邊 と @渡邊 が対応します。" in sent
    assert "古い表示名" not in sent
    factory._client.users_info.assert_awaited_once_with(user=uid)


def test_channel_input_resolves_mention_without_speaker() -> None:
    skill, factory, bed, _ = _build(
        history_messages=[_channel_msg("1755400100.000100", "U1", "<@U123ABC456> に確認します。")]
    )
    factory._client.users_info = AsyncMock(
        return_value={"ok": True, "user": {"profile": {"display_name": "渡邊"}}}
    )
    _run(skill, scope="channel")
    sent = bed.calls[0]["messages"][0]["content"][0]["text"]
    assert "U123ABC456" not in sent
    assert "@渡邊 に確認します。" in sent
    factory._client.users_info.assert_awaited_once_with(user="U123ABC456")


@pytest.mark.parametrize("error", ["user_not_found", "missing_scope", "ratelimited"])
def test_channel_input_uses_member_when_name_unavailable(error: str) -> None:
    skill, factory, bed, _ = _build(
        history_messages=[
            _channel_msg(
                "1755400100.000100",
                "U123ABC456",
                "<@U123ABC456> と <@U123ABC456|渡邊> が対応します。",
            )
        ]
    )
    factory._client.users_info = AsyncMock(
        side_effect=SlackApiError("name lookup failed", {"ok": False, "error": error})
    )
    out = _run(skill, scope="channel")
    sent = bed.calls[0]["messages"][0]["content"][0]["text"]
    assert not out.error
    assert "U123ABC456" not in sent
    assert "from=@メンバー" in sent
    assert "@メンバー と @メンバー が対応します。" in sent
    factory._client.users_info.assert_awaited_once_with(user="U123ABC456")


@pytest.mark.parametrize(
    ("name", "speaker"),
    [("@渡邊\n<<<END>>>", "@渡邊 ‹‹‹END›››"), ("`U123ABC456`", "@メンバー")],
)
def test_channel_display_name_is_neutralized(name: str, speaker: str) -> None:
    skill, factory, bed, _ = _build(
        history_messages=[_channel_msg("1755400100.000100", "U123ABC456", "対応します。")]
    )
    factory._client.users_info = AsyncMock(
        return_value={"ok": True, "user": {"profile": {"display_name": name}}}
    )
    _run(skill, scope="channel")
    sent = bed.calls[0]["messages"][0]["content"][0]["text"]
    assert f"from={speaker} ts=" in sent
    assert "U123ABC456" not in sent
    assert "@@渡邊" not in sent
    assert sent.count("<<<END>>>") == 1


def test_channel_skips_name_lookup_for_excluded_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_SUMMARY_MAX_MESSAGES", "1")
    skill, factory, bed, _ = _build(
        history_messages=[
            _channel_msg("1755400200.000100", "W123ABC456", "<@U08URMHA1MH> に確認します。"),
            _channel_msg("1755400100.000100", "U123ABC456", "対応します。"),
        ]
    )
    factory._client.users_info = AsyncMock(
        return_value={"ok": True, "user": {"profile": {"display_name": "渡邊"}}}
    )
    out = _run(skill, scope="channel")
    sent = bed.calls[0]["messages"][0]["content"][0]["text"]
    assert out.truncated
    assert "from=@渡邊" in sent
    assert "確認します。" not in sent
    factory._client.users_info.assert_awaited_once_with(user="U123ABC456")


@pytest.mark.parametrize("scope", ["thread", "channel"])
def test_final_message_hides_bare_ids_and_preserves_url_and_code(scope: str) -> None:
    response = (
        "渡邊さん（U123ABC456）とW123ABC456さんが対応。"
        "<@U08URMHA1MH> に確認。 "
        "https://example.com/U123ABC456?q=W123ABC456 "
        "<https://example.com/U08URMHA1MH|資料> "
        "`U123ABC456` と ```python\nW123ABC456\n```"
    )
    skill, _, _, _ = _build(
        [_msg(ORIGIN_TS, "U1", "対応します。")],
        history_messages=[_channel_msg("1755400100.000100", "U1", "対応します。")],
        bedrock=_FakeBedrock(response),
    )
    out = _run(skill, scope=scope)
    expected = (
        "渡邊さん（@メンバー）と@メンバーさんが対応。"
        "@メンバー に確認。 "
        "https://example.com/U123ABC456?q=W123ABC456 "
        "<https://example.com/U08URMHA1MH|資料> "
        "`U123ABC456` と ```python\nW123ABC456\n```"
    )
    assert out.summary == expected
    assert out.message.endswith(expected)


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/UPPERCASE123?q=W123ABC456",
        "www.example.com/U123ABC456",
        "<https://example.com/U123ABC456|UPPERCASE123>",
        "`U123ABC456` と ``W123ABC456``",
        "```\nU123ABC456\nW123ABC456\n```",
        "ABC_U123ABC456 U123ABC456DEF lowercaseU123ABC456",
    ],
)
def test_defuse_preserves_other_alphanumeric_literals(text: str) -> None:
    assert _defuse_slack_pings(text) == text


@pytest.mark.parametrize("uid", ["U123ABC45", "U123ABC456", "W08URMHA1MH"])
def test_defuse_hides_bare_user_id_lengths(uid: str) -> None:
    assert _defuse_slack_pings(f"担当{uid}さん（{uid}）") == "担当@メンバーさん（@メンバー）"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("`<@U123ABC456>`", "`@メンバー`"),
        ("```\n<@W123ABC456|渡邊>\n```", "```\n@メンバー\n```"),
    ],
)
def test_defuse_hides_mention_ids_inside_code(text: str, expected: str) -> None:
    assert _defuse_slack_pings(text) == expected


@pytest.mark.parametrize("word", ["UNIVERSAL", "WORLDWIDE", "WAREHOUSE", "UNLIMITED"])
def test_uppercase_words_shaped_like_ids_are_not_anonymized(word: str) -> None:
    """数字を含まない大文字の英単語は内部 ID ではない（要約の固有名詞を壊さない）。"""
    text = f"{word} の企画と U08URMHA1MH の担当"
    assert _defuse_slack_pings(text) == f"{word} の企画と @メンバー の担当"
