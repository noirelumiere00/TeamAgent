"""OperationLogSkill の単体テスト (Bedrock / Slack をモック)。

実 API を呼ばず、会話テキスト → CRM 構造化ログのパースを検証する。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.skills.base import SkillContext
from teamagent.skills.operation_log.schema import OperationLogInput
from teamagent.skills.operation_log.skill import OperationLogSkill

_LLM_OUTPUT = """### 営業活動ログ
- 日時/相手: 日本ガイシ 田中様
- 要点: ショート動画施策の予算感をヒアリング。月50万で検討中。
- 顧客の反応: PDCA の柔軟性を評価。代理店経由に懸念。
- 論点・宿題: 6月までに提案、決裁は部長承認が必要。

```json
{
  "deal_phase": "ヒアリング",
  "action": "予算ヒアリング実施",
  "next_step": "6月上旬までに提案書を提出",
  "bant": {
    "budget": "月50万円で検討",
    "authority": "部長承認が必要",
    "need": "ショート動画での認知拡大",
    "timeline": "6月導入希望"
  }
}
```
"""


@pytest.fixture
def fake_bedrock() -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text=_LLM_OUTPUT,
        usage=TokenUsage(
            input_tokens=800,
            output_tokens=300,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.0069,
        ),
        model_id="jp.anthropic.claude-sonnet-4-6",
        latency_ms=900,
        stop_reason="end_turn",
    )
    return mock


def test_operation_log_parses_structured_fields(fake_bedrock: MagicMock) -> None:
    skill = OperationLogSkill(bedrock=fake_bedrock)
    out = skill.run(
        OperationLogInput(conversation_text="田中: 予算は月50万で…"),
        ctx=SkillContext(),
    )
    assert out.deal_phase == "ヒアリング"
    assert out.action == "予算ヒアリング実施"
    assert "6月" in (out.next_step or "")
    assert out.bant.budget == "月50万円で検討"
    assert out.bant.authority == "部長承認が必要"
    assert out.total_cost_usd == pytest.approx(0.0069)
    # ログ本文は JSON ブロックを除いた人間可読部
    assert "営業活動ログ" in out.log_entry
    assert "```json" not in out.log_entry


def test_operation_log_empty_conversation_skips_llm(fake_bedrock: MagicMock) -> None:
    skill = OperationLogSkill(bedrock=fake_bedrock)
    out = skill.run(OperationLogInput(conversation_text="   "), ctx=SkillContext())
    assert out.source_message_count == 0
    assert "見つかりません" in out.log_entry
    fake_bedrock.converse.assert_not_called()


def test_operation_log_missing_json_block_still_returns_body(
    fake_bedrock: MagicMock,
) -> None:
    """LLM が JSON を返さなくても、ログ本文は必ず返す (fail-safe)。"""
    fake_bedrock.converse.return_value = ConverseResponse(
        text="### 営業活動ログ\n要点だけ書きました。",
        usage=TokenUsage(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.001,
        ),
        model_id="jp.anthropic.claude-sonnet-4-6",
        latency_ms=200,
        stop_reason="end_turn",
    )
    skill = OperationLogSkill(bedrock=fake_bedrock)
    out = skill.run(OperationLogInput(conversation_text="会話"), ctx=SkillContext())
    assert "要点だけ書きました" in out.log_entry
    assert out.deal_phase is None  # JSON 無し → 構造化フィールドは None


def test_operation_log_null_fields_normalized(fake_bedrock: MagicMock) -> None:
    """LLM が "null"/"不明" を返したら None に正規化する。"""
    fake_bedrock.converse.return_value = ConverseResponse(
        text='ログ\n```json\n{"deal_phase":"提案","action":"提案","next_step":"不明",'
        '"bant":{"budget":"null","authority":null,"need":"課題あり","timeline":"—"}}\n```',
        usage=TokenUsage(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.001,
        ),
        model_id="jp.anthropic.claude-sonnet-4-6",
        latency_ms=200,
        stop_reason="end_turn",
    )
    skill = OperationLogSkill(bedrock=fake_bedrock)
    out = skill.run(OperationLogInput(conversation_text="会話"), ctx=SkillContext())
    assert out.next_step is None  # "不明" → None
    assert out.bant.budget is None  # "null" → None
    assert out.bant.authority is None  # JSON null → None
    assert out.bant.timeline is None  # "—" → None
    assert out.bant.need == "課題あり"  # 実値は残る


def test_operation_log_via_slack_thread(fake_bedrock: MagicMock) -> None:
    """channel_id + thread_ts 経路で Slack スレッドを取得して整形する。"""
    from teamagent.adapters.slack_channel_ingest_client import HistoryBatch, SlackMessage

    fake_slack = MagicMock()
    fake_slack.list_thread_replies.return_value = HistoryBatch(
        messages=(
            SlackMessage(ts="1.1", user="U1", text="日本ガイシ訪問。予算月50万。"),
            SlackMessage(ts="1.2", user="U2", text="決裁は部長承認とのこと。"),
        ),
        next_cursor=None,
        has_more=False,
    )
    skill = OperationLogSkill(bedrock=fake_bedrock, slack_ingest=fake_slack)
    out = skill.run(OperationLogInput(channel_id="C123", thread_ts="1.1"), ctx=SkillContext())
    fake_slack.list_thread_replies.assert_called_once()
    assert out.source_message_count == 2
    assert out.deal_phase == "ヒアリング"
