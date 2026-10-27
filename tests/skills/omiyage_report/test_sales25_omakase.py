"""Omakase intake uses fake models/search and never contacts external services."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.omiyage_report import input_check, omakase, sales25
from teamagent.skills.omiyage_report.schema import OmiyageReportSubmitInput, Sales25Intake
from tests.skills.omiyage_report.test_sales25_parent import (
    Pipeline,
    build,
    ctx,
    request,
    result,
)
from tests.skills.omiyage_report.test_sales25_parent import (
    flags as flags,
)


def candidates(**updates: Any) -> str:
    return json.dumps(
        {
            "competitors": ["ブランドB", "ブランドC"],
            "keywords": ["朝のケア", "乾燥対策", "時短ケア"],
            "category": "ケア用品",
            "target_product": "ケア商品",
            **updates,
        },
        ensure_ascii=False,
    )


@pytest.mark.parametrize("delegate", [None, "お任せ", "おまかせ", "未定", "不明"])
def test_omakase_accepts_and_starts_search(
    monkeypatch: pytest.MonkeyPatch, delegate: str | None
) -> None:
    """「ブランドAのお土産資料を作って。キーワードはお任せ」のsubmit入力。"""
    caller = Mock(return_value=candidates())
    monkeypatch.setattr(input_check, "bedrock_text_caller", caller)
    store = ProposalJobStore(table_name="", memory={})
    pipeline = Pipeline()
    accepted = build(store, pipeline).run(
        OmiyageReportSubmitInput(brand="ブランドA", keywords=[delegate] if delegate else []), ctx()
    )
    assert accepted.status == "queued" and accepted.job_id
    caller.assert_called_once()
    assert "お任せで次の条件にしました: " in accepted.message
    assert "（違えばこのスレッドで直してください）" in accepted.message
    assert all(
        v in accepted.message for v in ("ブランドB", "乾燥対策", "ケア商品", "条件なし（お任せ）")
    )
    assert len([c for c in pipeline.calls if ":search_" in c]) == 6
    assert result(store, accepted.job_id).final_pptx_ref is not None
    case = json.loads(store.get_job(accepted.job_id)["request_summary"])["case"]
    assert len({q.casefold() for q in case["queries"]}) == 6
    assert "brand" not in case["omakase_values"]


@pytest.mark.parametrize("competitors", [["指定競合1"], ["指定競合1", "指定競合2"]])
def test_partial_inputs_preserve_given_values(
    monkeypatch: pytest.MonkeyPatch, competitors: list[str]
) -> None:
    monkeypatch.setattr(input_check, "bedrock_text_caller", Mock(return_value=candidates()))
    store = ProposalJobStore(table_name="", memory={})
    case, missing = sales25.preflight(
        store,
        OmiyageReportSubmitInput(
            brand="ブランドA",
            competitors=competitors,
            keywords=["指定中立語"],
            sales25=Sales25Intake(claim_constraints="効能を断定しない"),
        ),
        ctx(),
    )
    assert missing is None
    assert case["competitors"][: len(competitors)] == competitors
    assert case["keywords"][0] == "指定中立語"
    assert case["claim_constraints"] == "効能を断定しない"
    assert "claim_constraints" not in case["omakase_values"]
    assert all(v not in omakase.note(case) for v in [*competitors, "指定中立語"])
    # Across instances, completed conditions are reused without another model call.
    monkeypatch.setattr(input_check, "bedrock_text_caller", lambda *a: pytest.fail("saved intake"))
    again, missing = sales25.preflight(store, OmiyageReportSubmitInput(), ctx())
    assert missing is None and again["omakase_values"] == case["omakase_values"]
    corrected, missing = sales25.preflight(
        store, OmiyageReportSubmitInput(category="指定カテゴリー"), ctx()
    )
    assert missing is None and corrected["category"] == "指定カテゴリー"
    assert "category" not in corrected["omakase_values"]


@pytest.mark.parametrize("brand", ["", "お任せ", "未定", "不明"])
def test_no_brand_asks_without_calling_model(monkeypatch: pytest.MonkeyPatch, brand: str) -> None:
    caller = Mock(side_effect=AssertionError("must not infer the client"))
    monkeypatch.setattr(input_check, "bedrock_text_caller", caller)
    output = build(ProposalJobStore(table_name="", memory={})).run(
        OmiyageReportSubmitInput(brand=brand), ctx()
    )
    assert output.status == "needs_input" and "brand" in output.missing and not output.job_id
    caller.assert_not_called()


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "{broken",
        "[]",
        candidates(competitors=["ブランドA"]),
        candidates(competitors=["ブランドB", "ブランドB"]),
        candidates(keywords=["お任せ"]),
        candidates(category=""),
    ],
)
def test_invalid_model_responses_do_not_start_jobs(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    caller = Mock(return_value=reply)
    monkeypatch.setattr(input_check, "bedrock_text_caller", caller)
    store = ProposalJobStore(table_name="", memory={})
    output = build(store).run(OmiyageReportSubmitInput(brand="ブランドA"), ctx())
    assert output.status == "needs_input" and not output.job_id
    assert caller.call_count == 2 and not store._memory


def test_model_exception_is_recoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(input_check, "bedrock_text_caller", Mock(side_effect=TimeoutError))
    output = build(ProposalJobStore(table_name="", memory={})).run(
        OmiyageReportSubmitInput(brand="ブランドA"), ctx()
    )
    assert output.status == "needs_input"


def test_duplicates_select_other_candidates_and_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    caller = Mock(
        side_effect=[
            candidates(
                competitors=["ブランドA", "ブランドB", " ブランドb "], keywords=["朝のケア"]
            ),
            candidates(
                competitors=["ブランドB", "ブランドC"],
                keywords=["ブランドA ケア", "朝のケア", "乾燥対策", "時短ケア"],
            ),
        ]
    )
    monkeypatch.setattr(input_check, "bedrock_text_caller", caller)
    case, missing = sales25.preflight(
        ProposalJobStore(table_name="", memory={}),
        OmiyageReportSubmitInput(brand="ブランドA"),
        ctx(),
    )
    assert missing is None and caller.call_count == 2
    assert case["competitors"] == ["ブランドB", "ブランドC"]
    assert case["keywords"] == ["朝のケア", "乾燥対策", "時短ケア"]
    assert len(set(case["queries"])) == 6


def test_explicit_duplicate_queries_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(input_check, "bedrock_text_caller", Mock(return_value=candidates()))
    case, missing = sales25.preflight(
        ProposalJobStore(table_name="", memory={}),
        OmiyageReportSubmitInput(brand="ブランドA", sales25=Sales25Intake(queries=["同じ語"] * 6)),
        ctx(),
    )
    assert missing is not None and missing.missing == ["queries"]
    assert case["queries"] == ["同じ語"] * 6


def test_complete_input_needs_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    caller = Mock()
    monkeypatch.setattr(input_check, "bedrock_text_caller", caller)
    case, missing = sales25.preflight(ProposalJobStore(table_name="", memory={}), request(), ctx())
    assert missing is None and not omakase.note(case)
    caller.assert_not_called()


def test_duplicate_candidates_use_reserves_in_one_response(monkeypatch: pytest.MonkeyPatch) -> None:
    caller = Mock(
        return_value=candidates(
            competitors=["ブランドA", "ブランドB", " ブランドb ", "ブランドC"],
            keywords=["朝のケア", "朝のケア", "ブランドB ケア", "乾燥対策", "時短ケア"],
        )
    )
    monkeypatch.setattr(input_check, "bedrock_text_caller", caller)
    case, missing = sales25.preflight(
        ProposalJobStore(table_name="", memory={}),
        OmiyageReportSubmitInput(brand="ブランドA"),
        ctx(),
    )
    assert missing is None and len(set(case["queries"])) == 6
    caller.assert_called_once()


def test_only_competitors_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(input_check, "bedrock_text_caller", Mock(return_value=candidates()))
    case, missing = sales25.preflight(
        ProposalJobStore(table_name="", memory={}),
        OmiyageReportSubmitInput(brand="ブランドA", competitors=["指定競合1", "指定競合2"]),
        ctx(),
    )
    assert missing is None and case["competitors"] == ["指定競合1", "指定競合2"]
    assert "competitors" not in case["omakase_values"]
    assert set(case["omakase_values"]) == {
        "keywords",
        "category",
        "target_product",
        "claim_constraints",
    }


def test_placeholders_in_all_fields_are_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(input_check, "bedrock_text_caller", Mock(return_value=candidates()))
    case, missing = sales25.preflight(
        ProposalJobStore(table_name="", memory={}),
        OmiyageReportSubmitInput(
            brand="ブランドA",
            competitors=["未定", "お任せ"],
            keywords=["不明"],
            category="おまかせ",
            sales25=Sales25Intake(target_product="お任せ", claim_constraints="未定"),
        ),
        ctx(),
    )
    assert missing is None and len(set(case["queries"])) == 6
    assert set(case["omakase_values"]) == set(omakase.LABELS)
