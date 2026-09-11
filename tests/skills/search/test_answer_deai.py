"""検索回答が run() を通ったあと「AI が書いた感じ」の装飾を持たないこと（経路の結合テスト）。

単体（tests/skills/_shared/test_deai_text.py）が関数の写像を固定するのに対し、
ここは「差し込み位置が正しいか」を固定する。具体的には:

  1. LLM が `—` / `--` / `---` を返しても、SearchOutput.answer には残らない。
     （`**` は配信側が Slack の太字へ変換する正規の記法なので、ここでは落とさない。）
  2. 後処理は `_source_links_block` の `[label](url)` 付与より**上流**にあるため、
     本番 env（SEARCH_ANSWER_SOURCE_LINKS=1）で付く markdown リンクは無傷で残る。
     ここが逆順になるとリンクが壊れるので、経路の順序をテストで固定する。

実 DB 0・実 Bedrock 0（tests/skills/search/test_include_answer.py と同じモック作法）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import teamagent.skills.search.skill as skill_mod
from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchInput
from teamagent.skills.search.skill import SearchSkill

# 利用者が実際に受け取った回答の形（2026-09-11 の指摘）を LLM 応答として注入する。
DECORATED_LLM_TEXT = """**直接回答**
採用ショート動画は「1日密着型」が最も効果的。84万回再生（アクセンチュア）。

**刺さったパターン**
- **1日密着**: スケジュール再現で見せる — 求職者に刺さる

---

**推奨アクション**
1. **1日密着構成を軸にする** -- 職種別に出し分ける [chunk_id: 1]
"""


def _bedrock(text: str) -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text=text,
        usage=TokenUsage(
            input_tokens=200,
            output_tokens=80,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.0018,
        ),
        model_id="jp.anthropic.claude-sonnet-4-6",
        latency_ms=300,
        stop_reason="end_turn",
    )
    return mock


def _pgvector() -> MagicMock:
    mock = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm
    mock.search_similar.return_value = [
        SearchHit(
            chunk_id=1,
            content="1日密着型の採用ショート動画が伸びた",
            score=0.91,
            metadata={
                "source": "recruit_2026.pdf",
                "title": "採用提案",
                "drive_url": "https://drive.google.com/file/d/AAA/view",
            },
        ),
    ]
    return mock


class _FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024


def _skill(bedrock: MagicMock) -> SearchSkill:
    return SearchSkill(
        bedrock=bedrock,
        pgvector=_pgvector(),
        embedder=_FakeEmbedder(),
        target_table="proposal_chunks",
    )


def test_answer_has_no_ai_decoration_after_run() -> None:
    """run() を通った answer に `—` `--` `---` が残らない。

    `**` は上流が strong を Slack の太字へ変換するため落とさない（残ることを固定する）。
    """
    out = _skill(_bedrock(DECORATED_LLM_TEXT)).run(
        input=SearchInput(query="採用動画の勝ち筋"), ctx=SkillContext()
    )
    assert "—" not in out.answer
    assert "--" not in out.answer
    assert "**直接回答**" in out.answer
    # 内部マーカー除去（従来の契約）も併存している
    assert "chunk_id" not in out.answer


def test_answer_keeps_numbers_and_proper_nouns_after_run() -> None:
    """装飾を落としても、価値である数字と固有名詞は残る。"""
    out = _skill(_bedrock(DECORATED_LLM_TEXT)).run(
        input=SearchInput(query="採用動画の勝ち筋"), ctx=SkillContext()
    )
    assert "84万回再生（アクセンチュア）" in out.answer
    assert "1日密着" in out.answer


def test_source_links_markdown_survives_postprocessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本番 env（SEARCH_ANSWER_SOURCE_LINKS=1）で付く `[label](url)` を壊さない。"""
    monkeypatch.setenv("SEARCH_ANSWER_SOURCE_LINKS", "1")
    out = _skill(_bedrock(DECORATED_LLM_TEXT)).run(
        input=SearchInput(query="採用動画の勝ち筋"), ctx=SkillContext()
    )
    assert "[採用提案](https://drive.google.com/file/d/AAA/view)" in out.answer
    assert "📎 *資料リンク*" in out.answer
    # リンクを付けても本文側のダッシュは戻らない（`**` は上流が太字にするので残る）
    assert "—" not in out.answer
    assert "**直接回答**" in out.answer


def test_postprocess_runs_upstream_of_source_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """後処理が `_source_links_block` の付与より上流にあることを、実際に固定する。

    出力側の assert（リンクが壊れていない）だけでは順序を固定できない。リンクは
    `](url)` と `📎 *資料リンク*` のどちらも後処理の保護領域か単独 `*` なので、
    差し込みを下流へ移しても結果が同じで緑のまま通る（変異テストで実測）。
    そこで後処理に渡された「入力」を覗き、リンクブロックがまだ付いていないことを見る。
    """
    monkeypatch.setenv("SEARCH_ANSWER_SOURCE_LINKS", "1")
    seen: list[str] = []
    real = skill_mod.strip_ai_decoration

    def _spy(text: str, **kwargs: object) -> str:
        seen.append(text)
        return real(text, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(skill_mod, "strip_ai_decoration", _spy)
    out = _skill(_bedrock(DECORATED_LLM_TEXT)).run(
        input=SearchInput(query="採用動画の勝ち筋"), ctx=SkillContext()
    )
    assert seen, "後処理が要約本文を一度も通っていない"
    for text in seen:
        assert "📎 *資料リンク*" not in text, (
            "後処理がリンク付与より下流にある。リンクの markdown を後処理に通すと"
            "壊れ得るので、差し込みは `_summarize` の直後（_strip_internal_markers）に置く"
        )
        assert "](" not in text
    # 上流にあるからこそ、リンクは無傷で残る。
    assert "[採用提案](https://drive.google.com/file/d/AAA/view)" in out.answer


def test_plain_answer_is_unchanged() -> None:
    """装飾の無い回答は素通し（後処理が余計な書き換えをしない）。"""
    out = _skill(_bedrock("採用ショート動画は1日密着型が効果的です。")).run(
        input=SearchInput(query="採用動画"), ctx=SkillContext()
    )
    assert out.answer == "採用ショート動画は1日密着型が効果的です。"
