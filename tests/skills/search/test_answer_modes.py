"""案件検索 v3 PR 1: 事実の質問と洞察の質問を分ける（回答モード）。

固定すること:
- 質問の分類（洞察の合図があるときだけ insight・一覧の合図で list・それ以外は fact）
- PROMPT_VERSION=v3 のときだけ、要約 LLM への user message に回答モードと資料名が載る
- v2d（本番既定）・v1 では user message もツール結果の形も変更前と同一（PROMPT_VERSION で戻せる）
- v3 / clientkarte v2 の system prompt が「受注・失注を断定しない」「必ず抽象化を強制しない」
- 本番の MCP 経路でカルテの版を KARTE_PROMPT_VERSION で選べる（既定 v1）

実 DB 0・実 Bedrock 0 のフェイクで固定する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import SkillContext
from teamagent.skills.search.answer_mode import MODE_INSTRUCTIONS, classify_answer_mode
from teamagent.skills.search.schema import SearchInput
from teamagent.skills.search.skill import SearchSkill

# ── 分類 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        "サンプル食品の最新の提案書どこ？",
        "アース製薬の撮影日はいつ？",
        "花王の案件の担当は誰？",
        "日立システムズの予算は？",
        "サンプル食品は受注した？",
        "先週の定例の議事録ある？",
        # 資料名に洞察語が入っているだけの資料探し（審査指摘 3）
        "SNS戦略資料どこ？",
        "トレンド傾向レポートを出して",
        "戦略会議の議事録ある？",
        # 「全部」を含んでも 1 件を求める質問（審査指摘 4）
        "全部の中で最新の提案書は？",
        # 過去の提案書を探しているだけ（生成依頼ではない）
        "先月提案してた資料どこ？",
        "過去の提案を出して",
        "企画書を出して",
    ],
)
def test_fact_questions(query: str) -> None:
    assert classify_answer_mode(query) == "fact"


@pytest.mark.parametrize(
    "query",
    [
        "飲料メーカー向けの提案実績を一覧で",
        "コンビニ業界の提案資料を全部出して",
        "今月決まった案件は何件？",
        "すべての案件を一覧で",
        # 10-01 BU1 ヒアリング（杉浦さん）: 複数の施策を並べてほしい質問
        "ADK経由で受注したショート動画施策を知りたい",
    ],
)
def test_list_questions(query: str) -> None:
    assert classify_answer_mode(query) == "list"


@pytest.mark.parametrize(
    "query",
    [
        "UGCのTTOで刺さった訴求の傾向は？",
        "化粧品の提案で勝ちパターンを教えて",
        "価格懸念にはどう切り返せばいい？",
        "代理店経由の案件の共通点",
        "なぜ失注したのか教えて",
        "飲料メーカー向けの提案の傾向を一覧で",  # 洞察の合図が一覧より優先
        # 資料を材料に案を作る依頼（10-01 BU1 ヒアリング・望月さんの使い方／審査指摘 2）
        "サンプル食品向けにコンセプトを考えて",
        "新商品のPR企画を考えて",
        "化粧品ブランドに提案して",
        "施策案を出して",
        "Z世代向けの切り口を練って",
    ],
)
def test_insight_questions(query: str) -> None:
    assert classify_answer_mode(query) == "insight"


def test_empty_query_is_fact() -> None:
    assert classify_answer_mode("") == "fact"


def test_mode_instructions_forbid_proposals_for_fact_and_list() -> None:
    for mode in ("fact", "list"):
        assert "提言は書かない" in MODE_INSTRUCTIONS[mode]  # type: ignore[index]
    assert "打ち手" in MODE_INSTRUCTIONS["insight"]


# ── 要約 LLM に渡る user message ──────────────────────────────────────


class _FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024


def _fake_bedrock() -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text="要約",
        usage=TokenUsage(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.0001,
        ),
        model_id="m",
        latency_ms=1,
        stop_reason="end_turn",
    )
    return mock


def _pgvector(hits: list[SearchHit]) -> MagicMock:
    mock = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm
    mock.search_similar_new_schema.return_value = hits
    return mock


def _hit(chunk_id: int, title: str) -> SearchHit:
    meta: dict[str, Any] = {
        "source_type": "gdrive",
        "source_uri": f"gdrive://FILE{chunk_id}",
        "document_id": f"doc-{chunk_id}",
        "title": title,
        "updated_at": "2026-09-01",
    }
    return SearchHit(chunk_id=chunk_id, content="本文", score=0.9, metadata=meta)


def _run(prompt_version: str, query: str) -> tuple[str, Any]:
    bedrock = _fake_bedrock()
    skill = SearchSkill(
        bedrock=bedrock,
        pgvector=_pgvector([_hit(7, "サンプル食品様_新商品PR施策ご提案.pptx")]),
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_cohere_rerank=False,
        use_client_boost=False,
        prompt_version=prompt_version,
    )
    out = skill.run(input=SearchInput(query=query), ctx=SkillContext())
    text = bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
    return text, out


def test_v3_fact_question_gets_fact_mode_and_title_header() -> None:
    text, _ = _run("v3", "サンプル食品の最新の提案書どこ？")
    assert text.startswith("以下の社内資料から質問に答えてください。\n\n# 回答モード: 事実確認\n")
    assert (
        "[chunk_id: 7, score: 0.900, 資料名: サンプル食品様_新商品PR施策ご提案.pptx, 更新日: 2026-09-01"
        in text
    )


def test_v3_insight_question_gets_insight_mode() -> None:
    text, _ = _run("v3", "サンプル食品の提案で刺さった訴求の傾向は？")
    assert "# 回答モード: 洞察" in text


# 変更前（62049c05）の SearchSkill で同じフェイクを流して取った user message の実物。
_PRE_V3_USER_MESSAGE = (
    "以下の社内資料から質問に答えてください。\n\n"
    "# 質問\nサンプル食品の最新の提案書どこ？\n\n"
    "# 参考資料\n[chunk_id: 7, score: 0.900, 更新日: 2026-09-01]\n本文"
)


@pytest.mark.parametrize("version", ["v2d", "v1"])
def test_pre_v3_versions_are_byte_identical(version: str) -> None:
    """v2d（本番既定）と v1 では要約 LLM への入力もツール結果の形も変更前と同一。

    部分文字列ではなく全文一致で見る（審査指摘 5）。ツール結果は server.py が
    ``model_dump()`` をそのまま返すため、キーが 1 つ増えても本番の入力が変わる（審査指摘 1）。
    ``found``（金庫に該当があったか）は 10-06 に版を問わず意図して足した 1 キー。
    """
    text, out = _run(version, "サンプル食品の最新の提案書どこ？")
    assert text == _PRE_V3_USER_MESSAGE
    assert set(out.model_dump()) == {"answer", "hits", "total_cost_usd", "found"}


def test_v3_tool_output_shape_is_unchanged() -> None:
    """v3 でも回答モードはツール結果に載せない（ログ search_answer_mode で残す）。"""
    _, out = _run("v3", "サンプル食品の最新の提案書どこ？")
    assert set(out.model_dump()) == {"answer", "hits", "total_cost_usd", "found"}


def test_followup_path_also_gets_mode() -> None:
    """二段返しの後追い（deliver_followup_answer → _summarize）にも回答モードが載る。"""
    bedrock = _fake_bedrock()
    skill = SearchSkill(
        bedrock=bedrock,
        pgvector=_pgvector([]),
        embedder=_FakeEmbedder(),
        use_new_schema=True,
        use_cohere_rerank=False,
        use_client_boost=False,
        prompt_version="v3",
    )
    skill._summarize("ADK経由で受注したショート動画施策を知りたい", [_hit(7, "資料A")], "r")
    text = bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
    assert "# 回答モード: 一覧" in text


# ── system prompt の契約 ─────────────────────────────────────────────


def test_search_v3_prompt_contract() -> None:
    system = load_prompt("search", "v3", "system")
    assert "必ず抽象化" not in system  # 事実の質問に抽象化を強制しない
    assert "受注・失注を断定してよい正本" in system
    assert "BANT 評価や温度感から受注・失注を推測して書かない" in system
    assert "「更新日」「資料名の日付」だけを書く" in system
    assert "日付が無い資料には日付を付けない" in system
    assert "参考資料ヘッダの「資料名」で示す" in system
    assert "「Aico から見える資料には無い」と書く" in system


def test_clientkarte_v2_prompt_contract() -> None:
    system = load_prompt("clientkarte", "v2", "system")
    assert "必ず抽象化" not in system
    assert "受注・失注は断定しない" in system
    assert "BANT C は「検討止まり」の評価であって失注ではない" in system
    assert "「最新の営業 FB（日付）では〜」" in system
    assert "chunk_id や技術的な ID は書かない" in system


# ── カルテの版を MCP 経路で選べる ──────────────────────────────────────


def test_karte_prompt_version_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.orchestrator.factory import karte_prompt_version_from_env

    monkeypatch.delenv("KARTE_PROMPT_VERSION", raising=False)
    assert karte_prompt_version_from_env() == "v1"
    monkeypatch.setenv("KARTE_PROMPT_VERSION", "v2")
    assert karte_prompt_version_from_env() == "v2"
    monkeypatch.setenv("KARTE_PROMPT_VERSION", "  ")
    assert karte_prompt_version_from_env() == "v1"
