"""ClientKarteSkill の単体テスト (bedrock / pgvector をモック)。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.clientkarte.schema import ClientKarteInput
from teamagent.skills.clientkarte.skill import ClientKarteSkill


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


@pytest.fixture
def fake_pgvector_with_timeline() -> MagicMock:
    mock = MagicMock()
    mock.connection.return_value = _conn_mock()
    mock.list_client_timeline.return_value = [
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
    return mock


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
    fake_pgvector_with_timeline.list_client_timeline.assert_called_once()


def test_clientkarte_passes_client_name_and_limit_to_adapter(
    fake_bedrock: MagicMock, fake_pgvector_with_timeline: MagicMock
) -> None:
    skill = ClientKarteSkill(bedrock=fake_bedrock, pgvector=fake_pgvector_with_timeline)
    skill.run(ClientKarteInput(client_name="マンダム", limit=10), ctx=SkillContext())
    kwargs = fake_pgvector_with_timeline.list_client_timeline.call_args.kwargs
    assert kwargs["client_name"] == "マンダム"
    assert kwargs["limit"] == 10


def test_clientkarte_no_fb_skips_bedrock(fake_bedrock: MagicMock) -> None:
    """FB が 0 件なら Bedrock を呼ばず「記録がありません」を返す。"""
    pg = MagicMock()
    pg.connection.return_value = _conn_mock()
    pg.list_client_timeline.return_value = []

    skill = ClientKarteSkill(bedrock=fake_bedrock, pgvector=pg)
    out = skill.run(ClientKarteInput(client_name="存在しない社"), ctx=SkillContext())

    assert out.event_count == 0
    assert out.total_cost_usd == 0.0
    assert "見つかりません" in out.answer
    fake_bedrock.converse.assert_not_called()
