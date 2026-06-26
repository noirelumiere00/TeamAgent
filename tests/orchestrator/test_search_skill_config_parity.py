"""QW-2: SearchSkill 構築の env→引数解決を 1 か所（factory）に集約したことの回帰テスト。

かつて runtime/slack_bot.py の SearchSkill 構築は rerank_pool_size / min_relevance_fallback /
use_client_boost / use_knowledge_filters を渡さずコンストラクタ既定に落ち、本番 env を入れても
slack_bot 経路では黙って無効化される構築ドリフトがあった。本テストは

  1. resolve_search_skill_config() が 4 ノブを含む全ノブを env から解決すること
  2. slack_bot.get_search_skill() が build_search_skill_from_env() に委譲すること（独自解決を持たない）

を固定し、両経路が同一 env から同一ノブで構築されることを保証する。
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.orchestrator import factory

# QW-2 で「黙って落ちていた」4 ノブ。両経路で env を反映しなければならない。
_DRIFT_NOBS = (
    "rerank_pool_size",
    "min_relevance_fallback",
    "use_client_boost",
    "use_knowledge_filters",
)


def test_resolve_config_contains_previously_dropped_nobs() -> None:
    """4 ノブが config dict に含まれる（コンストラクタ既定に落とさず env で解決）。"""
    config = factory.resolve_search_skill_config()
    for nob in _DRIFT_NOBS:
        assert nob in config, f"{nob} が env 解決から欠落（構築ドリフト再発）"


def test_resolve_config_reflects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """env を立てると 4 ノブ + 関連ノブが反映される。"""
    monkeypatch.setenv("SEARCH_RERANK_POOL_SIZE", "50")
    monkeypatch.setenv("SEARCH_MIN_RELEVANCE_FALLBACK", "0.3")
    monkeypatch.setenv("USE_CLIENT_BOOST", "false")
    monkeypatch.setenv("USE_KNOWLEDGE_FILTERS", "true")
    monkeypatch.setenv("SEARCH_RERANK_RETURN_SIZE", "80")

    config = factory.resolve_search_skill_config()
    assert config["rerank_pool_size"] == 50
    assert config["min_relevance_fallback"] == pytest.approx(0.3)
    assert config["use_client_boost"] is False
    assert config["use_knowledge_filters"] is True
    assert config["rerank_return_size"] == 80


def test_resolve_config_defaults_are_backward_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env 全未設定の既定（後方互換）: fallback=0.0 / pool=30 / knowledge_filters=False。

    use_client_boost のみ factory の文書化された既定 ON（両経路で統一）。
    """
    for name in (
        "SEARCH_RERANK_POOL_SIZE",
        "SEARCH_RERANK_RETURN_SIZE",
        "SEARCH_MIN_RELEVANCE",
        "SEARCH_MIN_RELEVANCE_FALLBACK",
        "USE_CLIENT_BOOST",
        "USE_KNOWLEDGE_FILTERS",
        "USE_COHERE_RERANK",
    ):
        monkeypatch.delenv(name, raising=False)
    config = factory.resolve_search_skill_config()
    assert config["rerank_pool_size"] == 30
    assert config["rerank_return_size"] == 100
    assert config["min_relevance"] == pytest.approx(0.0)
    assert config["min_relevance_fallback"] == pytest.approx(0.0)
    assert config["use_knowledge_filters"] is False
    assert config["use_cohere_rerank"] is False
    # client-boost は両経路で既定 ON（A/B +4pp 実証・固有名詞のみ発火・fail-open）。
    assert config["use_client_boost"] is True


def test_slack_bot_delegates_to_shared_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    """slack_bot.get_search_skill は共有ビルダーに委譲する（独自 env 解決を持たない）。"""
    from teamagent.runtime import slack_bot

    sentinel = object()
    called = {"n": 0}

    def _fake_builder() -> Any:
        called["n"] += 1
        return sentinel

    # 共有ビルダー名は factory 由来だが、slack_bot は関数内 import するためそちらを差し替える。
    monkeypatch.setattr(factory, "build_search_skill_from_env", _fake_builder)
    monkeypatch.setattr(
        factory, "resolve_search_skill_config", lambda: {"use_cohere_rerank": False}
    )

    dispatcher = slack_bot.SkillDispatcher(router=object())  # router 注入で Bedrock 構築を回避
    skill = dispatcher.get_search_skill()

    assert skill is sentinel
    assert called["n"] == 1
    # キャッシュされ二度目は再構築しない。
    assert dispatcher.get_search_skill() is sentinel
    assert called["n"] == 1
