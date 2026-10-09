"""完成後の状態照会は配信記録に沿って返し、未確認の配信先を推測しない。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills._shared.long_jobs import ORIGIN_KEY, Origin
from teamagent.skills.base import SkillContext
from teamagent.skills.omiyage_report.schema import OmiyageReportStatusInput
from teamagent.skills.omiyage_report.skill import OmiyageReportStatusSkill

_JOB_ID = "omy_" + "7" * 32
_COMPLETED_MESSAGE = "お土産資料の生成が完了しました。"
_DELIVERY_FAILED_MESSAGE = (
    "資料はできあがりましたが、この DM へのお届けに失敗しました。作り直しますか？"
    "（同じ会社・キーワードでもう一度依頼してもらえれば作り直します）"
)


@pytest.mark.parametrize(
    ("delivered", "delivery_target", "expected_message"),
    [
        (True, "dm", _COMPLETED_MESSAGE),
        (True, "thread", _COMPLETED_MESSAGE),
        (False, "none", _DELIVERY_FAILED_MESSAGE),
        (None, "none", _COMPLETED_MESSAGE),
    ],
    ids=["delivered-dm", "delivered-thread", "delivery-failed", "legacy-result"],
)
@pytest.mark.parametrize("result_as_dict", [False, True], ids=["json", "dict"])
def test_done_status_uses_recorded_delivery_state(
    delivered: bool | None,
    delivery_target: str,
    expected_message: str,
    result_as_dict: bool,
) -> None:
    memory: dict[str, dict[str, Any]] = {}
    store = ProposalJobStore(table_name="", memory=memory)
    result: dict[str, Any] = {
        "status": "ready",
        "message": "検索結果から資料を作成しました。",
        "summary_lines": ["ヘアケアの紹介動画が中心でした。"],
        "next_step": "次の一手：導入の見せ方を比べましょう。",
        "delivery_target": delivery_target,
        "deck_plan_s3_uri": "s3://fake-bucket/deck-plan.json",
    }
    if delivered is not None:
        result["slack_delivered"] = delivered
    store.create_job(_JOB_ID, {"kind": "omiyage_report"})
    assert store.mark_running(_JOB_ID)
    assert store.mark_done(_JOB_ID, json.dumps(result, ensure_ascii=False))
    if result_as_dict:
        memory[_JOB_ID]["result_json"] = result

    output = OmiyageReportStatusSkill(store=store).run(
        OmiyageReportStatusInput(job_id=_JOB_ID), SkillContext(request_id="status-delivery-test")
    )

    assert output.status == "done"
    assert output.message == expected_message
    assert output.result_message == result["message"]
    if delivered is False:
        for internal_term in (
            "S3",
            "s3://",
            "job_id",
            _JOB_ID,
            "deck_plan_s3_uri",
            "omiyage_report",
        ):
            assert internal_term not in output.message
        assert "届いているはず" not in output.message
        assert "Drive" not in output.message


@pytest.mark.parametrize("pending", [False, True], ids=["delivery-failed", "delivery-pending"])
def test_done_status_checks_pending_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pending: bool
) -> None:
    monkeypatch.setenv("USE_LONG_JOB_NOTIFY", "1")
    store = ProposalJobStore(table_name="", memory={})
    store.create_job(_JOB_ID, {"kind": "omiyage_report"})
    assert store.mark_running(_JOB_ID)
    assert store.mark_done(
        _JOB_ID,
        json.dumps(
            {
                "status": "ready",
                "message": "資料を作成しました。",
                "summary_lines": ["検索結果の要点"],
                "next_step": "導入の見せ方を比べましょう。",
                "slack_delivered": False,
            },
            ensure_ascii=False,
        ),
    )
    target = Origin("D12345678", None, "U12345678")
    if pending:
        path = tmp_path / "report.pptx"
        path.write_bytes(b"generated report")
        target.defer(object(), str(path), "資料", "完成しました。", "status-delivery-test")
    ctx = SkillContext(request_id="status-delivery-test", metadata={ORIGIN_KEY: target})

    try:
        output = OmiyageReportStatusSkill(store=store).run(
            OmiyageReportStatusInput(job_id=_JOB_ID), ctx
        )
        assert output.status == "done"
        assert output.message == (_COMPLETED_MESSAGE if pending else _DELIVERY_FAILED_MESSAGE)
        assert target.pending == pending
    finally:
        target.discard()
