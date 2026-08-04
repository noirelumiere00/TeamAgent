"""ProposalBuilderInput の外部入力境界（MCP JSON）検証。

本番の呼び出し形＝JSON 由来のプレーンな dict/str/ISO 日付文字列を
そのまま model_validate に渡す（実測でこの経路が入口即死していた）。
"""

from datetime import date

import pytest
from pydantic import ValidationError

from teamagent.skills.proposal_builder.schema import ProposalBuilderInput


def _minimal_payload() -> dict:
    return {"gemini_json": "{}", "posting_start_date": "2026-09-01"}


def test_mcp_json_iso_date_string_is_accepted() -> None:
    """親 _StrictModel の strict=True がキー単位マージで残ると必ず落ちる回帰。"""

    value = ProposalBuilderInput.model_validate(_minimal_payload())
    assert value.posting_start_date == date(2026, 9, 1)


def test_child_config_explicitly_disables_strict() -> None:
    # strict キー自体を親から継承すると ISO 文字列が拒否される（罠の直接検査）
    assert ProposalBuilderInput.model_config.get("strict") is False


def test_extra_keys_are_still_forbidden() -> None:
    # strict を緩めても extra="forbid" の防壁は維持されていること
    payload = _minimal_payload() | {"unexpected_key": 1}
    with pytest.raises(ValidationError):
        ProposalBuilderInput.model_validate(payload)


def test_malformed_date_is_still_rejected() -> None:
    payload = _minimal_payload() | {"posting_start_date": "9月1日"}
    with pytest.raises(ValidationError):
        ProposalBuilderInput.model_validate(payload)
