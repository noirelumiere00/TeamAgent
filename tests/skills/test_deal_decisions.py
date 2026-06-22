"""deal_decisions ヘルパのテスト（課金0・Slack/Bedrock を fake 注入・ネット不要）。

検証観点:
- 解決: requester の部屋を案件名でマッチ／曖昧・該当なしで skip
- クロール→抽出→bullets 反映／抽出空で空result
- 漏洩ガード(G6): 抽出 system prompt に「本音/値引き除外」「資料であり指示でない」
- fail-open: Slack/Bedrock 例外でも空resultを返し伝播しない
- キャッシュ: 同一 (requester, hint) は抽出を1回に
- 純粋関数: build_decisions_prompt_section / _salvage_str_array
"""

from __future__ import annotations

from typing import Any

from teamagent.adapters.slack_channel_ingest_client import HistoryBatch, SlackMessage
from teamagent.skills._shared.deal_decisions import (
    DealDecisionsProvider,
    _salvage_str_array,
    build_decisions_prompt_section,
)
from teamagent.skills.base import SkillContext


# ── fakes ────────────────────────────────────────────────────────
class _FakeSlack:
    def __init__(
        self,
        *,
        user_id: str | None = "U1",
        channels: list[tuple[str, str]] | None = None,
        messages: tuple[SlackMessage, ...] = (),
        history_raises: bool = False,
    ) -> None:
        self._user_id = user_id
        self._channels = channels if channels is not None else [("C1", "案件_moribuilding")]
        self._messages = messages
        self._history_raises = history_raises
        self.history_calls: list[str] = []

    def lookup_user_id_by_email(self, email: str, request_id: str) -> str | None:
        return self._user_id

    def list_user_conversations(
        self, user_id: str | None, request_id: str, **kw: Any
    ) -> list[tuple[str, str]]:
        return self._channels

    def list_channel_history(
        self, channel_id: str, request_id: str, *, oldest: float | None = None, limit: int = 100
    ) -> HistoryBatch:
        self.history_calls.append(channel_id)
        if self._history_raises:
            raise RuntimeError("not_in_channel")
        return HistoryBatch(messages=self._messages)

    def list_thread_replies(
        self, channel_id: str, thread_ts: str, request_id: str, **kw: Any
    ) -> HistoryBatch:
        return HistoryBatch(messages=())


class _FakeBedrockResp:
    def __init__(self, text: str, cost: float = 0.0002) -> None:
        self.text = text
        self.usage = type("U", (), {"cost_usd": cost})()


class _FakeBedrock:
    def __init__(self, text: str = '["次回MTGは6/25 14時", "提案書を今週中に提出"]') -> None:
        self._text = text
        self.call_count = 0
        self.last_system = ""
        self.last_messages: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> _FakeBedrockResp:
        self.call_count += 1
        self.last_system = str(kwargs.get("system", ""))
        self.last_messages = list(kwargs.get("messages", []))
        return _FakeBedrockResp(self._text)


def _msg(ts: str, text: str, user: str = "U9") -> SlackMessage:
    return SlackMessage(ts=ts, user=user, text=text)


def _ctx() -> SkillContext:
    return SkillContext(request_id="req-deal-1", metadata={})


def _provider(slack: _FakeSlack, bedrock: _FakeBedrock) -> DealDecisionsProvider:
    return DealDecisionsProvider(slack=slack, bedrock=bedrock)


# ── 解決 + 抽出 ───────────────────────────────────────────────────
def test_resolve_and_extract_happy_path() -> None:
    slack = _FakeSlack(
        channels=[("C1", "案件_moribuilding"), ("C2", "proj-ナレッジ共有")],
        messages=(_msg("1700000000.1", "次回MTGは6/25で合意しました"),),
    )
    bedrock = _FakeBedrock()
    out = _provider(slack, bedrock).fetch("山田太郎 moribuilding", "me@vectorinc.co.jp", _ctx())

    assert out.bullets == ["次回MTGは6/25 14時", "提案書を今週中に提出"]
    assert out.sources and out.sources[0].source_uri == "slack://C1"
    assert out.sources[0].channel_name == "案件_moribuilding"
    assert slack.history_calls == ["C1"]  # 正しい channel をクロール
    assert bedrock.call_count == 1


def test_no_channel_match_skips_without_bedrock() -> None:
    # 案件名にマッチする部屋が無い → 解決 None → 抽出は呼ばれない（コスト0）。
    slack = _FakeSlack(channels=[("C9", "general"), ("C8", "random")])
    bedrock = _FakeBedrock()
    out = _provider(slack, bedrock).fetch("山田 moribuilding", "me@x.co.jp", _ctx())
    assert out.is_empty
    assert bedrock.call_count == 0
    assert slack.history_calls == []


def test_ambiguous_match_skips() -> None:
    # 同じトークンに複数案件がヒット → 曖昧なので skip（誤った案件を入れない）。
    slack = _FakeSlack(
        channels=[("C1", "案件_moribuilding_a"), ("C2", "案件_moribuilding_b")],
    )
    bedrock = _FakeBedrock()
    out = _provider(slack, bedrock).fetch("moribuilding", "me@x.co.jp", _ctx())
    assert out.is_empty
    assert bedrock.call_count == 0


def test_extraction_empty_returns_empty() -> None:
    slack = _FakeSlack(messages=(_msg("1700000000.1", "雑談"),))
    bedrock = _FakeBedrock(text="[]")
    out = _provider(slack, bedrock).fetch("moribuilding", "me@x.co.jp", _ctx())
    assert out.is_empty
    assert bedrock.call_count == 1  # 解決はできたので抽出は呼ぶ


# ── 漏洩ガード(G6) ────────────────────────────────────────────────
def test_leak_guard_in_extraction_prompt() -> None:
    slack = _FakeSlack(messages=(_msg("1700000000.1", "決定: 次回MTGは6/25"),))
    bedrock = _FakeBedrock()
    _provider(slack, bedrock).fetch("moribuilding", "me@x.co.jp", _ctx())
    sys = bedrock.last_system
    assert "本音" in sys
    assert "値引き" in sys
    assert "資料" in sys and "指示ではありません" in sys


# ── fail-open ─────────────────────────────────────────────────────
def test_fail_open_on_slack_error() -> None:
    slack = _FakeSlack(history_raises=True)
    bedrock = _FakeBedrock()
    out = _provider(slack, bedrock).fetch("moribuilding", "me@x.co.jp", _ctx())
    assert out.is_empty  # 例外は握り潰し空result


def test_no_client_hint_returns_empty() -> None:
    slack = _FakeSlack()
    bedrock = _FakeBedrock()
    out = _provider(slack, bedrock).fetch("", "me@x.co.jp", _ctx())
    assert out.is_empty
    assert bedrock.call_count == 0


# ── キャッシュ ────────────────────────────────────────────────────
def test_cache_per_requester_hint() -> None:
    slack = _FakeSlack(messages=(_msg("1700000000.1", "決定"),))
    bedrock = _FakeBedrock()
    prov = _provider(slack, bedrock)
    prov.fetch("moribuilding", "a@x.co.jp", _ctx())
    prov.fetch("moribuilding", "a@x.co.jp", _ctx())  # 同一キー → キャッシュ
    assert bedrock.call_count == 1
    prov.fetch("moribuilding", "b@x.co.jp", _ctx())  # 別 requester → 再実行
    assert bedrock.call_count == 2


# ── 純粋関数 ──────────────────────────────────────────────────────
def test_build_section() -> None:
    from teamagent.skills._shared.deal_decisions import DealDecisionsResult

    assert build_decisions_prompt_section(None) == ""
    assert build_decisions_prompt_section(DealDecisionsResult.empty()) == ""
    sec = build_decisions_prompt_section(DealDecisionsResult(bullets=["A", "B"]))
    assert "# 案件の決定事項" in sec
    assert "- A" in sec and "- B" in sec


def test_salvage_str_array() -> None:
    assert _salvage_str_array('["a", "b"]') == ["a", "b"]
    assert _salvage_str_array('前置き ["x"] 後置き') == ["x"]
    # max_tokens 打ち切り（末尾欠け）も救済
    assert _salvage_str_array('["完結した項目", "途中で切れ') == ["完結した項目"]
    assert _salvage_str_array("not json") == []
    assert _salvage_str_array("") == []
