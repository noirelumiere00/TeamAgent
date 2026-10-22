"""通常回答へ付けた使用済み資料も、直後の追問で再認可して返す。"""

from unittest.mock import MagicMock

import pytest

from teamagent.skills.search.schema import DocumentReference, SearchOutput
from tests.mcp_gateway.test_document_followup import OTHER_URL, REF, URL, _Search, dispatch
from tests.mcp_gateway.test_document_followup import isolated as isolated
from tests.mcp_gateway.test_surface_video_followup import ME


async def test_normal_fallback_used_drive_ref_is_saved_and_restored(
    monkeypatch: pytest.MonkeyPatch, isolated: MagicMock
) -> None:
    from teamagent.adapters.pgvector_client import SearchHit
    from teamagent.skills.search.compact import append_used_drive_links

    skill = _Search()
    refs: list[DocumentReference] = []
    body = "対象分野の事例資料では、商品紹介を工夫しています。"
    answer = append_used_drive_links(
        body,
        [
            SearchHit(1, "記録", 0.9, {**REF, "title": REF["title"]}),
            SearchHit(
                2,
                "記録",
                0.9,
                {
                    "title": "未使用資料",
                    "source_type": "gdrive",
                    "source_uri": "gdrive://UNRELATED",
                },
            ),
        ],
        refs,
    )
    output = SearchOutput(answer=answer, displayed_refs=refs, found=True, total_cost_usd=0)
    run = MagicMock(return_value=output)
    monkeypatch.setattr(skill, "run", run)
    first = await dispatch(skill, "保存率を上げるコツは？")
    assert first["document_followup_available"] is True
    saved = isolated.save.call_args.args[2]
    assert [ref["drive_file_id"] for ref in saved] == ["ORIGINAL"]
    assert first["answer"] == body + f"\n\n<{URL}|資料を見る>"
    followup = await dispatch(skill, "その資料も出して")
    assert URL in followup["answer"] and OTHER_URL not in followup["answer"]
    run.assert_called_once()
    assert skill._pgvector.connection.call_args.kwargs["user_email"] == ME
