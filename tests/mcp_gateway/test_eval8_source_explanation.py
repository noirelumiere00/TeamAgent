from time import monotonic
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import SearchHit
from tests.eval8_checks import assert_eval_checks
from tests.mcp_gateway.test_document_followup import REF, URL, _Search, dispatch
from tests.mcp_gateway.test_document_followup import isolated as isolated


async def test_first_named_source_explanation_falls_through_to_normal_search() -> None:
    skill = _Search()
    query = "{client_a}の{campaign_a}について、元資料をもとに説明して"

    out = await dispatch(skill, query, thread="1792700000.123456")

    assert skill.calls == [query]
    assert out["answer"] == f"事例資料 {URL}"
    skill._pgvector.list_authorized_document_references.assert_not_called()
    skill._pgvector.search_similar_new_schema.assert_not_called()


@pytest.mark.parametrize("visible", [True, False])
async def test_original_source_explanation_reads_authorized_drive_body(visible: bool) -> None:
    started = monotonic()
    skill = _Search()
    first = await dispatch(skill, "{client_a}の施策ってどんな感じだった？")
    skill._embed = MagicMock(return_value=[0.1])
    skill._summarize = MagicMock(
        return_value=(
            "調理実演で利用場面を伝えています。[S:1]\n盛り付けも紹介しています。[S:1]",
            0.01,
        )
    )
    skill._pgvector.search_similar_new_schema.return_value = [
        SearchHit(1, "元資料本文: 調理実演と盛り付けを紹介。", 0.9, REF)
    ]
    if not visible:
        skill._pgvector.list_authorized_document_references.return_value = []
    out = await dispatch(skill, "昨日のあなたの返事ではなく、元の資料を根拠に説明して")
    assert skill.calls == ["{client_a}の施策ってどんな感じだった？"]
    if visible:
        assert_eval_checks(
            "own_answer_source", [first["answer"], out["answer"]], elapsed=monotonic() - started
        )
        assert "調理実演" in out["answer"] and URL in out["answer"]
        assert "spreadsheets" not in out["answer"]
        assert skill._summarize.call_args.args[1][0].content.startswith("元資料本文")
        assert skill._pgvector.search_similar_new_schema.call_args.kwargs[
            "filter_drive_file_ids"
        ] == ["ORIGINAL"]
    else:
        assert URL not in out["answer"]
        skill._summarize.assert_not_called()
        skill._pgvector.search_similar_new_schema.assert_not_called()
