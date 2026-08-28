"""出典 URL の全機能強制（①）と「次の一手」提案（⑥-b）の横断テスト。

ユーザー最重要指示: 「出典やエビデンス部分は引用元の URL を出す。全ての機能で」。
本番実測で web_research の出典 URL を OpenClaw が書き直しで落とした事故があるため、
**リンクはサーバ側の決定論コードが message 本文へ焼き込む**（LLM に書かせない）。

次の一手（⑥-b）の不変条件:
  - 受け皿ツールが今 ON のときだけ提案する（出来ない約束を作らない）
  - 1 応答につき最大 1 個
  - 提案は文字列を足すだけ＝ツール実行・外部送信を伴わない
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills._shared.next_step import (
    ATTACHMENT_MODE_SUGGESTION,
    CALENDAR_SUGGESTION,
    append_suggestion,
    has_scheduling_cue,
    suggestions_enabled,
    tool_enabled,
)
from teamagent.skills._shared.source_url import (
    slack_permalink,
    slack_thread_permalink,
    slack_workspace_domain,
)
from teamagent.skills.attachment_assist.discover import AttachmentCandidate, evaluate_file
from teamagent.skills.base import SkillContext
from teamagent.skills.clientkarte.skill import ClientKarteSkill
from teamagent.skills.slack_summary.schema import SlackSummaryInput
from teamagent.skills.slack_summary.skill import SlackSummarySkill

ME = "me@vectorinc.co.jp"
ORIGIN_CH = "C0ORIGIN"
ORIGIN_TS = "1755400000.100100"
PERMALINK = f"https://vectorinc.slack.com/archives/{ORIGIN_CH}/p1755400000100100"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SLACK_WORKSPACE",
        "SLACK_WORKSPACE_DOMAIN",
        "SUGGEST_NEXT_STEP",
        "USE_CALENDAR_EVENT_TOOL",
        "USE_KNOWLEDGE_DELIVER",
    ):
        monkeypatch.delenv(name, raising=False)


# ── ① permalink の機械組立（純関数）────────────────────────────────────────


def test_workspace_domain_accepts_both_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WORKSPACE", "vectorinc")
    assert slack_workspace_domain() == "vectorinc"
    # 新しい env 名が優先される
    monkeypatch.setenv("SLACK_WORKSPACE_DOMAIN", "othersub")
    assert slack_workspace_domain() == "othersub"


def test_workspace_domain_accepts_full_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """運用でドメイン全体を書かれても壊れない。"""
    monkeypatch.setenv("SLACK_WORKSPACE_DOMAIN", "https://vectorinc.slack.com/")
    assert slack_workspace_domain() == "vectorinc"


def test_permalink_is_built_deterministically(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WORKSPACE_DOMAIN", "vectorinc")
    assert slack_permalink(ORIGIN_CH, ORIGIN_TS) == PERMALINK
    assert slack_thread_permalink(f"slack://{ORIGIN_CH}/{ORIGIN_TS}") == PERMALINK


@pytest.mark.parametrize(
    ("channel", "ts"),
    [("", ORIGIN_TS), (ORIGIN_CH, ""), ("C1/../evil", ORIGIN_TS), (ORIGIN_CH, "not-a-ts")],
)
def test_permalink_refuses_to_guess(monkeypatch: pytest.MonkeyPatch, channel: str, ts: str) -> None:
    monkeypatch.setenv("SLACK_WORKSPACE_DOMAIN", "vectorinc")
    assert slack_permalink(channel, ts) is None


def test_permalink_is_omitted_without_workspace() -> None:
    """workspace 不明なら壊れたリンクを作らない（fail-open）。"""
    assert slack_permalink(ORIGIN_CH, ORIGIN_TS) is None


# ── ①b slack_summary: 対象スレッドの permalink を末尾に付ける ────────────────


class _Tok:
    access_token = "xoxp-personal"
    slack_user_id = "U_ME"


class _Store:
    def get(self, email: str) -> Any:
        return _Tok()


class _Reader:
    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages

    def read_thread_checked(self, channel: str, ts: str, request_id: str, **kw: Any) -> Any:
        return type("R", (), {"error": "", "messages": tuple(self._messages)})()


class _Msg:
    def __init__(self, text: str) -> None:
        self.text = text
        self.user = "U1"


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text
        self.usage = type("U", (), {"cost_usd": 0.001})()


class _Bedrock:
    def __init__(self, text: str) -> None:
        self._text = text

    def converse(self, **kw: Any) -> _Resp:
        return _Resp(self._text)


def _summary_skill(summary_text: str) -> SlackSummarySkill:
    return SlackSummarySkill(
        slack_store=_Store(),
        reader_factory=lambda _t: _Reader([_Msg("本文")]),
        bedrock=_Bedrock(summary_text),
    )


def _run_summary(skill: SlackSummarySkill) -> Any:
    return skill.run(
        SlackSummaryInput(scope="thread"),
        SkillContext(
            request_id="r",
            metadata={"user_email": ME, "channel_id": ORIGIN_CH, "thread_ts": ORIGIN_TS},
        ),
    )


def test_slack_summary_appends_thread_permalink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WORKSPACE_DOMAIN", "vectorinc")

    out = _run_summary(_summary_skill("論点はA。"))

    assert PERMALINK in out.message
    assert "🔗 出典:" in out.message


def test_slack_summary_omits_permalink_without_workspace() -> None:
    out = _run_summary(_summary_skill("論点はA。"))

    assert "🔗" not in out.message
    assert out.summary == "論点はA。"  # 要約本体は変わらない


# ── ⑥-b フック1: slack_summary → カレンダー登録の提案 ───────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "8/20(木) 15:00 から打合せを実施することで決定した。",
        "キックオフは2026-08-20 10:00で確定。",
        "来週の月曜 14時にレビューを実施することになった。",
    ],
)
def test_scheduling_cue_fires_on_decided_datetime(text: str) -> None:
    assert has_scheduling_cue(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "8/20は先方が忙しいらしい。",  # 日付だけ（決定していない）
        "打合せをやることになった。",  # 決定語だけ（日時未定）
        "",
    ],
)
def test_scheduling_cue_stays_silent_without_both_signals(text: str) -> None:
    assert has_scheduling_cue(text) is False


def test_slack_summary_suggests_calendar_when_tool_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_CALENDAR_EVENT_TOOL", "true")

    out = _run_summary(_summary_skill("8/20(木) 15:00 から打合せを実施することで決定した。"))

    assert out.message.rstrip().endswith(CALENDAR_SUGGESTION)


def test_slack_summary_does_not_suggest_when_tool_is_off() -> None:
    out = _run_summary(_summary_skill("8/20(木) 15:00 から打合せを実施することで決定した。"))

    assert CALENDAR_SUGGESTION not in out.message


def test_slack_summary_does_not_suggest_without_a_decided_datetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_CALENDAR_EVENT_TOOL", "true")

    out = _run_summary(_summary_skill("雑談が続いている。決まったことは特にない。"))

    assert CALENDAR_SUGGESTION not in out.message


def test_slack_summary_suggestion_writes_nothing_to_slack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提案しただけでは実行しない（read-only の死守ラインを壊さない）。"""
    monkeypatch.setenv("USE_CALENDAR_EVENT_TOOL", "true")
    client = MagicMock()
    client.chat_postMessage = AsyncMock(
        side_effect=SlackApiError("must not post", {"ok": False, "error": "nope"})
    )
    skill = _summary_skill("8/20(木) 15:00 から打合せを実施することで決定した。")

    out = _run_summary(skill)

    assert CALENDAR_SUGGESTION in out.message
    client.chat_postMessage.assert_not_awaited()


def test_kill_switch_silences_every_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_CALENDAR_EVENT_TOOL", "true")
    monkeypatch.setenv("SUGGEST_NEXT_STEP", "false")

    out = _run_summary(_summary_skill("8/20(木) 15:00 から打合せを実施することで決定した。"))

    assert CALENDAR_SUGGESTION not in out.message
    assert suggestions_enabled() is False


def test_only_one_suggestion_per_response() -> None:
    once = append_suggestion("本文", CALENDAR_SUGGESTION)
    assert append_suggestion(once, ATTACHMENT_MODE_SUGGESTION) == once


def test_tool_gate_defaults_to_off() -> None:
    """env が読めない環境では提案しない側へ倒す。"""
    assert tool_enabled("USE_CALENDAR_EVENT_TOOL") is False


# ── ①c attachment_assist: 読んだ原本（Slack ファイル）へのリンク ──────────────


def _slack_file(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "F1",
        "name": "見積.pdf",
        "mimetype": "application/pdf",
        "size": 1024,
        "url_private": "https://files.slack.com/files-pri/T1-F1/mitsumori.pdf",
        "permalink": "https://vectorinc.slack.com/files/U1/F1/mitsumori.pdf",
    }
    base.update(over)
    return base


def test_attachment_candidate_keeps_the_slack_permalink() -> None:
    cand, rejected = evaluate_file(_slack_file(), max_bytes=10**7)

    assert rejected is None and cand is not None
    assert cand.permalink == "https://vectorinc.slack.com/files/U1/F1/mitsumori.pdf"


@pytest.mark.parametrize(
    "permalink",
    [
        "https://evil.example.com/files/U1/F1/x.pdf",  # 別ホスト
        "http://vectorinc.slack.com/files/U1/F1/x.pdf",  # 非 HTTPS
        "https://slack.com.evil.example/files/x.pdf",  # 接尾辞偽装
        "javascript:alert(1)",
        "",
    ],
)
def test_attachment_rejects_untrusted_permalink(permalink: str) -> None:
    """file dict は外部由来。任意 URL を「出典」として提示させない。"""
    cand, _ = evaluate_file(_slack_file(permalink=permalink), max_bytes=10**7)

    assert cand is not None and cand.permalink == ""


def _compose(
    mode: str = "summary", permalink: str = "https://vectorinc.slack.com/files/U1/F1/x"
) -> str:
    from teamagent.skills.attachment_assist.skill import _compose_message

    target = AttachmentCandidate(
        file_id="F1",
        name="見積.pdf",
        kind="pdf",
        mime="application/pdf",
        size=1024,
        url="https://files.slack.com/files-pri/T1-F1/x.pdf",
        ts=1.0,
        permalink=permalink,
    )
    return _compose_message(
        target=target,
        mode=mode,
        pages=3,
        chars=100,
        truncated=False,
        answer="要約本文",
        others=[],
        aggregated=False,
    )


def test_attachment_message_carries_the_source_link() -> None:
    assert "🔗 出典: https://vectorinc.slack.com/files/U1/F1/x" in _compose()


def test_attachment_message_omits_link_when_unavailable() -> None:
    assert "🔗" not in _compose(permalink="")


# ── ⑥-b フック3: attachment_assist の他モード案内 ───────────────────────────


def test_attachment_summary_offers_other_modes() -> None:
    assert _compose(mode="summary").rstrip().endswith(ATTACHMENT_MODE_SUGGESTION)


@pytest.mark.parametrize("mode", ["revise", "minutes", "aggregate", "translate"])
def test_attachment_other_modes_do_not_offer_more(mode: str) -> None:
    """目的を指定して呼ばれている＝依頼は完結しているので提案しない。"""
    assert ATTACHMENT_MODE_SUGGESTION not in _compose(mode=mode)


def test_attachment_suggestion_can_be_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUGGEST_NEXT_STEP", "0")
    assert ATTACHMENT_MODE_SUGGESTION not in _compose(mode="summary")


# ── ①c clientkarte: FB 1 件ごとの出典リンク ─────────────────────────────────


class _Karte:
    def __init__(self, hits: list[SearchHit]) -> None:
        self._hits = hits

    def connection(self, **kw: Any) -> Any:
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=MagicMock())
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    def list_client_timeline_recent(self, **kw: Any) -> list[SearchHit]:
        return self._hits


def _karte_skill(hits: list[SearchHit]) -> ClientKarteSkill:
    bedrock = MagicMock()
    bedrock.converse.return_value = type(
        "R", (), {"text": "カルテ本文", "usage": type("U", (), {"cost_usd": 0.001})()}
    )()
    return ClientKarteSkill(bedrock=bedrock, pgvector=_Karte(hits))


def test_clientkarte_event_links_back_to_the_slack_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_WORKSPACE_DOMAIN", "vectorinc")
    hits = [
        SearchHit(
            chunk_id=1,
            content="温度感は高い",
            score=1.0,
            metadata={"source_uri": f"slack://{ORIGIN_CH}/{ORIGIN_TS}", "client_name": "花王"},
        )
    ]

    from teamagent.skills.clientkarte.schema import ClientKarteInput

    out = _karte_skill(hits).run(
        ClientKarteInput(client_name="花王"), SkillContext(request_id="r", metadata={})
    )

    assert out.events[0].url == PERMALINK


def test_clientkarte_event_url_is_none_when_not_resolvable() -> None:
    hits = [
        SearchHit(chunk_id=1, content="x", score=1.0, metadata={"source_uri": "gdrive://abc"}),
    ]

    from teamagent.skills.clientkarte.schema import ClientKarteInput

    out = _karte_skill(hits).run(
        ClientKarteInput(client_name="花王"), SkillContext(request_id="r", metadata={})
    )

    assert out.events[0].url is None  # 推測した URL は入れない


# ── ①d source_link: シート行直リンク / Drive リンクのパススルー ──────────────


def test_source_link_passes_through_sheet_row_deeplink() -> None:
    from teamagent.skills._shared.source_url import source_link

    url = "https://docs.google.com/spreadsheets/d/1AbC_dEf/edit?gid=123#gid=123&range=57:57"
    assert source_link(url) == url


def test_source_link_passes_through_drive_web_view_link() -> None:
    from teamagent.skills._shared.source_url import source_link

    url = "https://drive.google.com/file/d/1XyZ/view"
    assert source_link(url) == url


@pytest.mark.parametrize(
    "uri",
    [
        "gdrive://abc123",
        "https://evil.example.com/spreadsheets/d/1AbC/edit",
        "http://docs.google.com/spreadsheets/d/1AbC/edit",
        "",
    ],
)
def test_source_link_refuses_internal_ids_and_unknown_hosts(uri: str) -> None:
    from teamagent.skills._shared.source_url import source_link

    assert source_link(uri) is None


def test_source_link_still_resolves_slack_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.skills._shared.source_url import source_link

    monkeypatch.setenv("SLACK_WORKSPACE_DOMAIN", "vectorinc")
    assert (
        source_link("slack://C0AAA/1755500000.123456")
        == "https://vectorinc.slack.com/archives/C0AAA/p1755500000123456"
    )


def test_clientkarte_answer_appends_resolved_source_links() -> None:
    from teamagent.skills.clientkarte.schema import KarteEvent
    from teamagent.skills.clientkarte.skill import _with_source_links

    sheet = "https://docs.google.com/spreadsheets/d/1AbC/edit?gid=1#gid=1&range=5:5"
    events = [
        KarteEvent(chunk_id=1, url=sheet, summary="a"),
        KarteEvent(chunk_id=2, url=sheet, summary="b"),
        KarteEvent(chunk_id=3, url=None, summary="c"),
    ]
    out = _with_source_links("カルテ本文", events)
    assert out == f"カルテ本文\n\n🔗 出典: {sheet}"


def test_clientkarte_answer_caps_source_links_at_three() -> None:
    from teamagent.skills.clientkarte.schema import KarteEvent
    from teamagent.skills.clientkarte.skill import _with_source_links

    events = [
        KarteEvent(
            chunk_id=i, url=f"https://docs.google.com/spreadsheets/d/x{i}/edit", summary=str(i)
        )
        for i in range(5)
    ]
    out = _with_source_links("本文", events)
    assert out.count("🔗 出典:") == 3


def test_clientkarte_answer_unchanged_without_links() -> None:
    from teamagent.skills.clientkarte.schema import KarteEvent
    from teamagent.skills.clientkarte.skill import _with_source_links

    assert _with_source_links("本文", [KarteEvent(chunk_id=1, url=None, summary="x")]) == "本文"
