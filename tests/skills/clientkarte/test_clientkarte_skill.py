"""ClientKarteSkill の単体テスト (bedrock / pgvector をモック)。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.clientkarte.schema import ClientKarteInput
from teamagent.skills.clientkarte.skill import ClientKarteSkill, _truncate_at_sentence_boundary


@pytest.fixture
def fake_bedrock() -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text="### 1. 一行サマリ\nヒアリング進行中、BANT B [chunk_id: 1]",
        usage=TokenUsage(
            input_tokens=300,
            output_tokens=120,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.005,
        ),
        model_id="jp.anthropic.claude-sonnet-4-6",
        latency_ms=400,
        stop_reason="end_turn",
    )
    return mock


def _conn_mock() -> MagicMock:
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _pgvector_with_hits(hits: list[SearchHit]) -> MagicMock:
    mock = MagicMock()
    mock.connection.return_value = _conn_mock()
    mock.list_client_timeline_recent.return_value = hits
    return mock


@pytest.fixture
def fake_pgvector_with_timeline() -> MagicMock:
    return _pgvector_with_hits(
        [
            SearchHit(
                chunk_id=1,
                content="ケイパ提示。既存素材活用が刺さった",
                score=1.0,
                metadata={
                    "occurred_at": "2026-05-10",
                    "deal_phase": "ケイパ",
                    "bant_score": "B（前向き）",
                    "next_action": "提案準備",
                    "positive_reaction": "PDCA柔軟性",
                },
            ),
            SearchHit(
                chunk_id=2,
                content="ヒアリング。IMP保証に高評価",
                score=1.0,
                metadata={
                    "occurred_at": "2026-05-22",
                    "deal_phase": "ヒアリング",
                    "bant_score": "B（前向き）",
                    "next_action": "6月提案",
                },
            ),
        ]
    )


class FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024


def test_clientkarte_synthesizes_timeline(
    fake_bedrock: MagicMock, fake_pgvector_with_timeline: MagicMock
) -> None:
    skill = ClientKarteSkill(bedrock=fake_bedrock, pgvector=fake_pgvector_with_timeline)
    out = skill.run(ClientKarteInput(client_name="日本ガイシ"), ctx=SkillContext())

    assert out.client_name == "日本ガイシ"
    assert out.event_count == 2
    assert "一行サマリ" in out.answer
    assert out.total_cost_usd == pytest.approx(0.005)
    # 時系列イベントに構造化メタが乗る
    assert out.events[0].deal_phase == "ケイパ"
    assert out.events[1].deal_phase == "ヒアリング"
    assert out.events[0].occurred_at == "2026-05-10"
    fake_pgvector_with_timeline.list_client_timeline_recent.assert_called_once()
    fake_pgvector_with_timeline.list_client_timeline.assert_not_called()


def test_clientkarte_passes_client_name_and_limit_to_adapter(
    fake_bedrock: MagicMock, fake_pgvector_with_timeline: MagicMock
) -> None:
    skill = ClientKarteSkill(bedrock=fake_bedrock, pgvector=fake_pgvector_with_timeline)
    skill.run(ClientKarteInput(client_name="マンダム", limit=10), ctx=SkillContext())
    kwargs = fake_pgvector_with_timeline.list_client_timeline_recent.call_args.kwargs
    assert kwargs["client_name"] == "マンダム"
    assert kwargs["limit"] == 10


def test_clientkarte_no_fb_skips_bedrock(fake_bedrock: MagicMock) -> None:
    """FB が 0 件なら Bedrock を呼ばず「記録がありません」を返す。"""
    pg = MagicMock()
    pg.connection.return_value = _conn_mock()
    pg.list_client_timeline_recent.return_value = []

    skill = ClientKarteSkill(bedrock=fake_bedrock, pgvector=pg)
    out = skill.run(ClientKarteInput(client_name="存在しない社"), ctx=SkillContext())

    assert out.event_count == 0
    assert out.total_cost_usd == 0.0
    assert "見つかりません" in out.answer
    fake_bedrock.converse.assert_not_called()


def test_clientkarte_truncates_at_sentence_boundary_and_prioritizes_metadata(
    fake_bedrock: MagicMock,
) -> None:
    content = "最初の文です。二番目の文はかなり長く続きます。最後です。"
    pg = _pgvector_with_hits(
        [
            SearchHit(
                chunk_id=10,
                content=content,
                score=1.0,
                metadata={
                    "occurred_at": "2026-07-01",
                    "client_reaction": "前向きだが予算を懸念",
                    "shared_memo": "決裁者同席で次回提案",
                },
            )
        ]
    )
    skill = ClientKarteSkill(
        bedrock=fake_bedrock,
        pgvector=pg,
        event_summary_max_chars=20,
        synthesis_body_max_chars=20,
    )

    out = skill.run(ClientKarteInput(client_name="テスト社"), ctx=SkillContext())

    assert out.events[0].summary == "最初の文です。…"
    message = fake_bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
    assert "顧客反応: 前向きだが予算を懸念" in message
    assert "共有メモ: 決裁者同席で次回提案" in message
    body_label = "補足本文（構造化メタにない情報の補完）:"
    assert message.index("構造化メタ:") < message.index(body_label)
    assert f"{body_label} 最初の文です。…" in message


def test_sentence_boundary_exactly_at_limit_is_not_cut_mid_sentence() -> None:
    assert _truncate_at_sentence_boundary("123456789。X", 10) == "123456789。"


def test_clientkarte_char_limits_are_configurable_by_env(
    fake_bedrock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIENTKARTE_EVENT_SUMMARY_MAX_CHARS", "12")
    monkeypatch.setenv("CLIENTKARTE_SYNTHESIS_BODY_MAX_CHARS", "14")
    pg = _pgvector_with_hits([SearchHit(chunk_id=11, content="A" * 500, score=1.0, metadata={})])
    skill = ClientKarteSkill(bedrock=fake_bedrock, pgvector=pg)

    out = skill.run(ClientKarteInput(client_name="テスト社"), ctx=SkillContext())

    assert out.events[0].summary == ("A" * 11) + "…"
    message = fake_bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
    assert f"補足本文（構造化メタにない情報の補完）: {('A' * 13)}…" in message


def test_clientkarte_char_limit_defaults_remain_160_and_200(
    fake_bedrock: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLIENTKARTE_EVENT_SUMMARY_MAX_CHARS", raising=False)
    monkeypatch.delenv("CLIENTKARTE_SYNTHESIS_BODY_MAX_CHARS", raising=False)
    pg = _pgvector_with_hits([SearchHit(chunk_id=12, content="A" * 500, score=1.0, metadata={})])
    skill = ClientKarteSkill(bedrock=fake_bedrock, pgvector=pg)

    out = skill.run(ClientKarteInput(client_name="テスト社"), ctx=SkillContext())

    assert out.events[0].summary == ("A" * 159) + "…"
    message = fake_bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
    assert f"補足本文（構造化メタにない情報の補完）: {('A' * 199)}…" in message
