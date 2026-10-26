"""Accept misplaced sales25 fields at the shared input boundary without relaxing validation."""

from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.omiyage_report import input_check
from teamagent.skills.omiyage_report.schema import OmiyageReportSubmitInput, Sales25Intake
from tests.skills.omiyage_report.test_sales25_parent import build, ctx, request
from tests.skills.omiyage_report.test_sales25_parent import flags as flags


def flat_input(claim: str = "効能を断定しない") -> dict[str, Any]:
    return {
        **request().model_dump(),
        "target_product": "指定商品",
        "claim_constraints": claim,
        "queries": ["A ケア", "B ケア", "C ケア", "朝", "乾燥", "時短"],
        "brand_synonyms": {"ブランドA": ["A系列"]},
    }


@pytest.mark.parametrize("claim", ["特になし", "なし", "条件なし", "制約なし"])
@pytest.mark.parametrize("location", ["flat", "nested"])
def test_all_six_fields_accept_explicit_no_constraints(
    monkeypatch: pytest.MonkeyPatch, claim: str, location: str
) -> None:
    data = flat_input(claim)
    if location == "nested":
        data["sales25"] = {
            k: data.pop(k)
            for k in ("target_product", "claim_constraints", "queries", "brand_synonyms")
        }
    original = copy.deepcopy(data)
    caller = Mock()
    monkeypatch.setattr(input_check, "bedrock_text_caller", caller)
    parsed = OmiyageReportSubmitInput.model_validate(data)
    assert data == original
    assert parsed.sales25 is not None
    assert parsed.sales25.target_product == "指定商品"
    assert parsed.sales25.claim_constraints == claim
    assert parsed.sales25.queries == flat_input()["queries"]
    assert parsed.sales25.brand_synonyms == {"ブランドA": ["A系列"]}
    store = ProposalJobStore(table_name="", memory={})
    accepted = build(store).run(parsed, ctx())
    assert accepted.status == "queued" and accepted.job_id
    caller.assert_not_called()
    case = json.loads(store.get_job(accepted.job_id)["request_summary"])["case"]
    assert case["claim_constraints"] == claim and not case["omakase_values"]


@pytest.mark.parametrize("nested_model", [False, True])
def test_nested_values_win_conflicts_and_flat_fields_fill_gaps(nested_model: bool) -> None:
    nested = {
        "target_product": "入れ子の商品",
        "claim_constraints": "入れ子の条件",
        "queries": ["nested"],
        "brand_synonyms": {"ブランドA": ["入れ子の同義語"]},
    }
    data = {**flat_input(), "sales25": Sales25Intake(**nested) if nested_model else nested}
    original = copy.deepcopy(data)
    parsed = OmiyageReportSubmitInput.model_validate(data)
    assert parsed.sales25 is not None and parsed.sales25.model_dump(exclude_unset=True) == nested
    assert data == original
    partial = OmiyageReportSubmitInput.model_validate(
        {**flat_input(), "sales25": {"target_product": ""}}
    )
    assert partial.sales25 is not None and partial.sales25.target_product == ""
    assert partial.sales25.claim_constraints == flat_input()["claim_constraints"]


@pytest.mark.parametrize("key", ["unknown", "mode", "source_job_id"])
def test_unknown_top_level_keys_still_rejected(key: str) -> None:
    with pytest.raises(ValidationError) as exc:
        OmiyageReportSubmitInput.model_validate({**flat_input(), key: "unexpected"})
    assert any(e["loc"] == (key,) and e["type"] == "extra_forbidden" for e in exc.value.errors())


@pytest.mark.parametrize("nested", ["invalid", [], {"unknown": "value"}])
def test_invalid_nested_sales25_still_rejected(nested: Any) -> None:
    with pytest.raises(ValidationError):
        OmiyageReportSubmitInput.model_validate({**flat_input(), "sales25": nested})


@pytest.mark.parametrize(
    "updates",
    [
        {"target_product": "x" * 501},
        {"claim_constraints": None},
        {"queries": ["q"] * 7},
        {"brand_synonyms": []},
    ],
)
def test_flat_fields_keep_nested_validation(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        OmiyageReportSubmitInput.model_validate({**flat_input(), **updates})


def test_null_sales25_accepts_flat_fields() -> None:
    parsed = OmiyageReportSubmitInput.model_validate({**flat_input(), "sales25": None})
    assert parsed.sales25 is not None and parsed.sales25.target_product == "指定商品"
