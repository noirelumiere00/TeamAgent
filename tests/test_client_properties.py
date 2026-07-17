"""Client-property confidence and override regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from teamagent.client_properties import (
    identity_value_map,
    resolve_client_industry,
    resolve_client_industry_with_source,
)


def test_curated_override_uses_conservative_company_identity() -> None:
    overrides = identity_value_map({"ポート株式会社": "人材"})

    assert resolve_client_industry("ポート", ["広告"], ["IT"], overrides) == "人材"
    assert resolve_client_industry("レポート", ["広告"], [], overrides) == "広告"


def test_unanimous_exact_owner_industry_is_kept_and_conflict_is_blank() -> None:
    assert resolve_client_industry("A社", ["食品", "食品"], [], {}) == "食品"
    assert resolve_client_industry("A社", ["食品"], ["小売"], {}) == ""
    assert resolve_client_industry("A社", [], [], {}) == ""
    assert resolve_client_industry_with_source("A社", ["食品"], [], {}) == (
        "食品",
        "exact_consensus",
    )
    assert resolve_client_industry_with_source("A社", ["食品"], ["小売"], {}) == (
        "",
        "conflict",
    )


def test_conflicting_overrides_for_same_legal_identity_are_rejected() -> None:
    with pytest.raises(ValueError, match="conflicting"):
        identity_value_map({"株式会社A": "食品", "A株式会社": "小売"})


def test_repository_industry_master_keeps_audited_properties() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "connect_web_filters"
        / "client_industry.json"
    )
    values = json.loads(path.read_text(encoding="utf-8"))["industry"]

    assert len(values) == 28
    assert values["ポート株式会社"] == "人材"
    assert values["アイホン"] == "電子機器"
    assert values["東京ドーム"] == "エンターテインメント"
    assert values["SBI生命保険"] == values["SBI証券"] == "金融"
