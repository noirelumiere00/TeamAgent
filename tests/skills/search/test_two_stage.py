"""二段返し（USE_SEARCH_TWO_STAGE・既定 OFF）のテスト。

検証の主眼:
1. フラグ OFF は従来挙動と **バイト等価**（ゲートの印が付いていても 1 バイトも変わらない）
2. ON なら要約完了を待たずヒットを返し、回答は発信元スレッドへ後追い投稿される
3. 後追いの失敗は fail-open（ヒット一覧は既に届いているので検索は成立している）
4. 宛先ガード: ctx.metadata に無い宛先へは絶対に投げない（既定チャンネルを持たない）

実 DB 0・実 Bedrock 0・実 Slack 0（tests/skills/test_search_skill.py と同じモック作法）。
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.adapters.slack_client import SlackPostResult
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchInput
from teamagent.skills.search.skill import SearchSkill
from teamagent.skills.search.two_stage import (
    TWO_STAGE_CTX_KEY,
    TWO_STAGE_ENV,
    TWO_STAGE_NOTICE,
    FollowupTarget,
    resolve_followup_target,
    to_slack_mrkdwn,
    two_stage_enabled,
)


@pytest.fixture
def fake_bedrock() -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text="要約テキスト [chunk_id: 1]",
        usage=TokenUsage(
            input_tokens=200,
            output_tokens=80,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.0018,
        ),
        model_id="jp.anthropic.claude-haiku-4-5",
        latency_ms=300,
        stop_reason="end_turn",
    )
    return mock


@pytest.fixture
def fake_pgvector() -> MagicMock:
    mock = MagicMock()
    cm_mock = MagicMock()
    cm_mock.__enter__ = MagicMock(return_value=MagicMock())
    cm_mock.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm_mock
    mock.search_similar.return_value = [
        SearchHit(
            chunk_id=1,
            content="PR代行は飲食・コスメ・教育で実績あり",
            score=0.91,
            metadata={"source": "proposal_2024_drink.pdf", "title": "飲料提案"},
        ),
    ]
    return mock


class FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024


def _slack_mock(*, posted: bool = True, user_id: str | None = "U1", dm: str | None = "D1"):
    """Slack のフェイク。post_message の呼び出し引数を検証するために使う。"""
    m = MagicMock()
    m.post_message = AsyncMock(
        side_effect=lambda channel, text, request_id, thread_ts=None: SlackPostResult(
            channel=channel, ts="1.0", ok=posted
        )
    )
    m.lookup_user_id_by_email = AsyncMock(return_value=user_id)
    m.open_dm = AsyncMock(return_value=dm)
    return m


def _skill(bedrock: MagicMock, pgvector: MagicMock, slack: Any = None) -> SearchSkill:
    return SearchSkill(
        bedrock=bedrock,
        pgvector=pgvector,
        embedder=FakeEmbedder(),
        target_table="proposal_chunks",
        slack=slack,
    )


def _ctx(
    *,
    marked: bool = True,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    email: str | None = None,
) -> SkillContext:
    md: dict[str, Any] = {}
    if marked:
        md[TWO_STAGE_CTX_KEY] = True
    if channel_id:
        md["channel_id"] = channel_id
    if thread_ts:
        md["thread_ts"] = thread_ts
    if email:
        md["user_email"] = email
    return SkillContext(request_id="req-two-stage", metadata=md)


# ── 宛先ガード（純関数・変異テストの的） ───────────────────────────────────
def test_target_is_none_without_channel_and_email() -> None:
    """channel_id も user_email も無ければ **投げ先なし**（既定チャンネルへ落とさない）。"""
    assert resolve_followup_target({}) is None
    assert resolve_followup_target(None) is None
    assert resolve_followup_target({"thread_ts": "111.222"}) is None
    # 空文字・非文字列は宛先として採らない
    assert resolve_followup_target({"channel_id": "", "user_email": "   "}) is None
    assert resolve_followup_target({"channel_id": 12345}) is None


def test_target_prefers_origin_thread() -> None:
    t = resolve_followup_target(
        {"channel_id": "C1", "thread_ts": "111.222", "user_email": "u@vectorinc.co.jp"}
    )
    assert t == FollowupTarget(channel_id="C1", thread_ts="111.222", email="u@vectorinc.co.jp")
    assert t is not None and t.kind == "channel"


def test_target_falls_back_to_requester_dm() -> None:
    """channel が無ければ依頼者本人の DM。thread_ts は捨てる（他所のスレッドに刺さない）。"""
    t = resolve_followup_target({"thread_ts": "111.222", "user_email": "u@vectorinc.co.jp"})
    assert t == FollowupTarget(channel_id=None, thread_ts=None, email="u@vectorinc.co.jp")
    assert t is not None and t.kind == "dm"


def test_mrkdwn_link_conversion() -> None:
    """後追いは bot 直投稿なので markdown リンクを Slack 記法へ変換する。"""
    src = "📎 *資料リンク*\n- [花王提案.pdf](https://drive.google.com/file/d/X/view)"
    assert to_slack_mrkdwn(src) == (
        "📎 *資料リンク*\n- <https://drive.google.com/file/d/X/view|花王提案.pdf>"
    )
    assert to_slack_mrkdwn("") == ""


# ── フラグ OFF: 従来挙動とバイト等価 ────────────────────────────────────
def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TWO_STAGE_ENV, raising=False)
    assert two_stage_enabled() is False


def test_flag_off_is_byte_identical(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """フラグ OFF なら、ゲートの印や channel_id が付いていても出力 JSON がバイト等価。"""
    monkeypatch.delenv(TWO_STAGE_ENV, raising=False)
    slack = _slack_mock()

    plain = _skill(fake_bedrock, fake_pgvector, slack).run(
        input=SearchInput(query="PR代行の実績"),
        ctx=SkillContext(metadata={}),  # 印なし（/app・slack_bot 直呼び相当）
    )
    marked = _skill(fake_bedrock, fake_pgvector, slack).run(
        input=SearchInput(query="PR代行の実績"),
        ctx=_ctx(channel_id="C1", thread_ts="111.222", email="u@vectorinc.co.jp"),
    )

    assert marked.model_dump_json() == plain.model_dump_json()
    assert marked.answer == "要約テキスト"  # 従来どおり同期要約
    assert marked.total_cost_usd == pytest.approx(0.0018)
    slack.post_message.assert_not_awaited()


def test_flag_on_without_gateway_mark_stays_synchronous(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """印が無い面（connect-web /app・slack_bot 直呼び・knowledge_deliver 内部呼び）は不変。"""
    monkeypatch.setenv(TWO_STAGE_ENV, "1")
    slack = _slack_mock()
    out = _skill(fake_bedrock, fake_pgvector, slack).run(
        input=SearchInput(query="PR代行の実績"),
        ctx=SkillContext(metadata={"channel_id": "C1", "user_email": "u@vectorinc.co.jp"}),
    )
    assert out.answer == "要約テキスト"
    assert out.total_cost_usd == pytest.approx(0.0018)
    slack.post_message.assert_not_awaited()


def test_flag_on_without_target_stays_synchronous(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """宛先が決まらない呼び出しは二段返しにしない（黙って回答を捨てない）。"""
    monkeypatch.setenv(TWO_STAGE_ENV, "1")
    slack = _slack_mock()
    out = _skill(fake_bedrock, fake_pgvector, slack).run(
        input=SearchInput(query="PR代行の実績"), ctx=_ctx()
    )
    assert out.answer == "要約テキスト"
    slack.post_message.assert_not_awaited()


def test_flag_on_zero_hits_keeps_legacy_message(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0 件は元々 Bedrock を呼ばない＝後追いにする価値が無い。従来文言のまま。"""
    monkeypatch.setenv(TWO_STAGE_ENV, "1")
    fake_pgvector.search_similar.return_value = []
    slack = _slack_mock()
    out = _skill(fake_bedrock, fake_pgvector, slack).run(
        input=SearchInput(query="該当なし"), ctx=_ctx(channel_id="C1")
    )
    assert "見つかりません" in out.answer
    slack.post_message.assert_not_awaited()


# ── フラグ ON: ヒット先出し + 後追い投稿 ────────────────────────────────
def test_two_stage_returns_hits_before_summary_and_posts_followup(
    fake_pgvector: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """要約完了を待たずに返り、その後で発信元スレッドへ後追い投稿する。

    converse は release イベントまでブロックする。**run() が返る＝要約を待っていない**
    ことの証明（従来どおり同期要約していたらこのテストはハングする）。
    """
    monkeypatch.setenv(TWO_STAGE_ENV, "1")
    release = threading.Event()
    posted = threading.Event()

    blocking_bedrock = MagicMock()

    def _blocking_converse(**kwargs: Any) -> ConverseResponse:
        assert release.wait(timeout=10), "converse が呼ばれる前に解放されなかった"
        return ConverseResponse(
            text="要約テキスト [chunk_id: 1]",
            usage=TokenUsage(
                input_tokens=200,
                output_tokens=80,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                cost_usd=0.0018,
            ),
            model_id="jp.anthropic.claude-haiku-4-5",
            latency_ms=300,
            stop_reason="end_turn",
        )

    blocking_bedrock.converse.side_effect = _blocking_converse

    slack = MagicMock()

    async def _post(
        channel: str, text: str, request_id: str, thread_ts: str | None = None
    ) -> SlackPostResult:
        posted.set()
        return SlackPostResult(channel=channel, ts="1.0", ok=True)

    slack.post_message = AsyncMock(side_effect=_post)

    skill = _skill(blocking_bedrock, fake_pgvector, slack)
    out = skill.run(
        input=SearchInput(query="PR代行の実績"),
        ctx=_ctx(channel_id="C1", thread_ts="111.222", email="u@vectorinc.co.jp"),
    )

    # 第一報: ヒットは全部載っている・回答は「続きを投げる」定型文・コストは 0
    assert out.answer == TWO_STAGE_NOTICE
    assert "詳細な考察" in out.answer  # OpenClaw に自作させないための一文
    assert len(out.hits) == 1
    assert out.hits[0].chunk_id == 1
    assert out.total_cost_usd == 0.0

    # ここで初めて要約を進ませる → 後追い投稿が発信元スレッドに届く
    release.set()
    assert posted.wait(timeout=10), "後追い投稿が呼ばれなかった"
    slack.post_message.assert_awaited_once()
    args, kwargs = slack.post_message.await_args
    assert args[0] == "C1"
    assert args[1] == "要約テキスト"  # _strip_internal_markers 済み
    assert kwargs["thread_ts"] == "111.222"


def test_followup_posts_to_dm_when_no_channel(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """channel が無ければ依頼者本人の DM に届く（knowledge_deliver と同じフォールバック）。"""
    slack = _slack_mock()
    skill = _skill(fake_bedrock, fake_pgvector, slack)
    ok = skill.deliver_followup_answer(
        query="q",
        hits=fake_pgvector.search_similar.return_value,
        file_urls={},
        request_id="req-1",
        target=FollowupTarget(channel_id=None, thread_ts=None, email="u@vectorinc.co.jp"),
    )
    assert ok is True
    slack.lookup_user_id_by_email.assert_awaited_once_with("u@vectorinc.co.jp", "req-1")
    slack.open_dm.assert_awaited_once_with("U1", "req-1")
    assert slack.post_message.await_args.args[0] == "D1"


def test_followup_channel_failure_falls_back_to_dm(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """スレッド投稿が ok=False でも本人 DM へ落とす（第二報を落とさない）。"""
    slack = _slack_mock()
    calls: list[str] = []

    async def _post(
        channel: str, text: str, request_id: str, thread_ts: str | None = None
    ) -> SlackPostResult:
        calls.append(channel)
        return SlackPostResult(channel=channel, ts="1.0", ok=channel != "C1")

    slack.post_message = AsyncMock(side_effect=_post)
    skill = _skill(fake_bedrock, fake_pgvector, slack)
    ok = skill.deliver_followup_answer(
        query="q",
        hits=fake_pgvector.search_similar.return_value,
        file_urls={},
        request_id="req-1",
        target=FollowupTarget(channel_id="C1", thread_ts=None, email="u@vectorinc.co.jp"),
    )
    assert ok is True
    assert calls == ["C1", "D1"]


def test_followup_without_email_never_guesses_a_destination(
    fake_bedrock: MagicMock, fake_pgvector: MagicMock
) -> None:
    """channel 投稿が失敗し email も無ければ **どこにも投げない**（推測しない）。"""
    slack = _slack_mock(posted=False)
    skill = _skill(fake_bedrock, fake_pgvector, slack)
    ok = skill.deliver_followup_answer(
        query="q",
        hits=fake_pgvector.search_similar.return_value,
        file_urls={},
        request_id="req-1",
        target=FollowupTarget(channel_id="C1", thread_ts=None, email=None),
    )
    assert ok is False
    slack.lookup_user_id_by_email.assert_not_awaited()
    slack.open_dm.assert_not_awaited()
    assert slack.post_message.await_count == 1  # C1 への 1 回だけ（他所へ投げ直さない）


def test_followup_failure_is_fail_open(fake_bedrock: MagicMock, fake_pgvector: MagicMock) -> None:
    """後追いが例外で落ちても呼び出し元へ伝播させない（ヒット一覧は既に届いている）。"""
    slack = MagicMock()
    slack.post_message = AsyncMock(side_effect=RuntimeError("slack down"))
    skill = _skill(fake_bedrock, fake_pgvector, slack)

    with capture_logs() as logs:
        ok = skill.deliver_followup_answer(
            query="q",
            hits=fake_pgvector.search_similar.return_value,
            file_urls={},
            request_id="req-1",
            target=FollowupTarget(channel_id="C1", thread_ts=None, email=None),
        )

    assert ok is False
    events = [e for e in logs if e.get("event") == "search_followup_failed"]
    assert events and events[0]["error"] == "RuntimeError"
    # 本文・クエリ原文はログに出さない（G8）
    assert "q" not in str(events[0].get("query", ""))


def test_two_stage_failure_does_not_break_the_first_response(
    fake_pgvector: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """後追い側が要約で落ちても第一報（ヒット一覧）は正常に返る。"""
    monkeypatch.setenv(TWO_STAGE_ENV, "1")
    broken_bedrock = MagicMock()
    done = threading.Event()

    def _boom(**kwargs: Any) -> ConverseResponse:
        done.set()
        raise RuntimeError("bedrock down")

    broken_bedrock.converse.side_effect = _boom
    slack = _slack_mock()
    out = _skill(broken_bedrock, fake_pgvector, slack).run(
        input=SearchInput(query="PR代行の実績"), ctx=_ctx(channel_id="C1")
    )

    assert out.answer == TWO_STAGE_NOTICE
    assert len(out.hits) == 1
    assert done.wait(timeout=10)
