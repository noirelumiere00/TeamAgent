"""件数・選別・リンクはコード、要点だけフェイクLLM。外部接続なし。"""

import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import compact, fanout, material_files
from teamagent.skills.search.answer_mode import is_case_list
from teamagent.skills.search.case_list import render
from teamagent.skills.search.composite import SlackItem, SlackLookup
from teamagent.skills.search.fanout_sources import CaseRow, SourceResult
from teamagent.skills.search.schema import SearchInput, SlackHitOut
from tests.skills.search.test_answer_modes import _fake_bedrock, _hit
from tests.skills.search.test_fanout import META, make
from tests.skills.search.test_fanout import setup as setup


def hit(index, *, industry="飲料", score=0.9, **meta):
    original = _hit(index, f"架空企画{index}")
    return replace(
        original,
        content="商品の実演動画を配信。利用場面を紹介。",
        score=score,
        metadata={**original.metadata, "cls_industry": industry, **meta},
    )


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("compact_enabled", [False, True])
@pytest.mark.parametrize("query", ["飲料系の事例を5件出して", "飲料系の事例資料を５件出して"])
def test_five_vault_seventeen_slack_three_cases_keep_five_with_only_three_points(
    monkeypatch,
    fanout_enabled,
    compact_enabled,
    query,
):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setattr(material_files, "file_reader", lambda *a, **kw: None)
    vault = [
        hit(1),
        hit(2),
        hit(11, industry=None, score=0.99),
        hit(12, industry=None, score=0.98),
        hit(1, score=0.8),
    ]
    slack = SlackLookup(
        "ok",
        "ok",
        items=tuple(
            SlackItem(
                hit=SlackHitOut(
                    permalink=f"https://example.slack.com/archives/C0FAKE/p17844240000000{i:02}",
                    file_names=[f"架空飲料企画{i % 6 + 5}.pdf"],
                    file_ids=[f"F0FAKE{i % 6 + 5}"],
                ),
                text=f"架空飲料企画{i % 6 + 5}の資料。",
            )
            for i in range(17)
        ),
    )
    rows = SourceResult(
        source="case",
        status="ok",
        reason="ok",
        items=tuple(
            CaseRow(
                title=f"架空企画{i}",
                source_uri=f"gdrive://ROW{i}",
                client=f"架空社{i}",
                brand="",
                product=f"架空企画{i}",
                effect="商品の実演動画を配信。",
                source="deck",
                kind="case",
                external_use="unknown",
                stage=2,
                in_main=False,
                industry="化粧品" if i == 99 else "飲料",
                updated_at="2026-09-01",
            )
            for i in (3, 4, 99)
        ),
    )
    skill, _ = make(vault)

    def retrieve(*a, **kw):
        kw["fanout_probe"]["case"] = rows
        return vault

    monkeypatch.setattr(skill, "_retrieve", retrieve)
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: slack)
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: slack)
    summary = MagicMock(
        return_value=(
            json.dumps(
                {
                    str(
                        i
                    ): "商品の実演動画を配信。\n利用場面を紹介。\nより詳しく見たい資料はありますか？"
                    for i in range(1, 4)
                }
            ),
            0.001,
        )
    )
    monkeypatch.setattr(skill, "_summarize", summary)
    out = skill.run(
        SearchInput(query=query),
        SkillContext(
            metadata={
                **META,
                compact.CTX_KEY: compact_enabled,
            }
        ),
    )
    assert is_case_list(query)
    assert out.answer_format == "list" and out.found
    cards = out.answer.split("\n\n")
    assert len(cards) == 5
    assert all(card.startswith("*") and "（2026年9月）" in card for card in cards[:4])
    assert all(card.count("https://") == 1 and "|資料を見る>" in card for card in cards)
    assert "ROW99" not in out.answer and "架空企画99" not in out.answer
    assert "FILE11" not in out.answer and "FILE12" not in out.answer
    assert out.answer.count("FILE1/view") == 1
    assert "見つかったのは" not in out.answer
    assert out.answer.count("商品の実演動画を配信。") == 3
    assert all(card.count("• ") <= 3 for card in cards)
    assert "ありますか" not in out.answer and "？" not in out.answer
    assert out.suggested_next is None
    summary.assert_called_once()
    assert summary.call_args.kwargs["case_points"] is True
    assert len(summary.call_args.args[1]) == 5
    assert out.total_cost_usd >= 0.001


def test_selection_filters_industry_client_deduplicates_and_orders_before_model():
    hits = [
        hit(99, industry="化粧品", score=1),
        hit(1, score=0.6),
        hit(2, score=0.9),
        hit(3, score=0.8),
        hit(4, score=0.7),
        hit(5, score=0.65),
        hit(6, industry=None, score=1),
        hit(7, score=1, client_name="架空別社"),
    ]
    hits = [replace(h, metadata={"client_name": "架空指定社", **h.metadata}) for h in hits]
    hits.extend(
        [
            hit(2),
            hit(8, campaign="同一施策", client_name="架空指定社"),
            hit(9, campaign="同一施策", client_name="架空指定社"),
        ]
    )
    model = MagicMock(return_value="{}")
    answer = render(
        hits,
        "飲料",
        lambda h: None,
        requested_count=5,
        asked_industry="飲料",
        client="架空指定社",
        summarize=model,
    )
    selected = model.call_args.args[0]
    assert [h.chunk_id for h in selected] == [2, 8, 3, 4, 5]
    assert answer.count("|資料を見る>") == 5


@pytest.mark.parametrize("response", ["{}", "[]", "invalid JSON", RuntimeError("fake failure")])
def test_only_two_eligible_keeps_cards_and_one_shortfall_line_on_model_failure(response):
    model = MagicMock(
        **(
            {"side_effect": response}
            if isinstance(response, Exception)
            else {"return_value": response}
        )
    )
    answer = render(
        [hit(1), hit(99, industry="化粧品"), hit(2)],
        "飲料",
        lambda h: None,
        requested_count=5,
        asked_industry="飲料",
        summarize=model,
    )
    assert answer.count("|資料を見る>") == 2
    assert answer.count("見つかったのは 2 件です。") == 1
    assert "FILE99" not in answer


def test_case_points_prompt_sends_only_selected_inputs_and_returns_raw_json():
    skill, _ = make([])
    bedrock = _fake_bedrock()
    bedrock.converse.return_value = replace(
        bedrock.converse.return_value, text='{"1":"実演動画。"}'
    )
    text, cost = skill._summarize(
        "飲料事例を5件", [hit(1), hit(2)], "fake-request", bedrock=bedrock, case_points=True
    )
    assert json.loads(text) == {"1": "実演動画。"}
    call = bedrock.converse.call_args.kwargs
    inputs = json.loads(call["messages"][0]["content"][0]["text"])
    assert set(inputs) == {"1", "2"}
    assert inputs["1"]["title"] == "架空企画1"
    assert "最大2行" in call["system"] and "聞き返し" in call["system"]
    assert cost > 0
