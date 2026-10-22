"""検索・MCP応答・DM直接便・0034の参照保存を同じ正本でつなぐ。"""

import json
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from teamagent.mcp_gateway import detached_jobs, server
from teamagent.skills.search import drive_first, fanout
from tests.mcp_gateway.test_direct_search_answer import enable
from tests.mcp_gateway.test_document_followup import isolated as isolated
from tests.mcp_gateway.test_surface_video_followup import (
    ME,
    _claim,
    _FakeSlack,
    _resolver_for,
    _spec,
    _Verifier,
)
from tests.skills.search.test_fanout import _converse, _vault_hit, make
from tests.skills.search.test_fanout import setup as setup

URL = "https://drive.google.com/file/d/DOC2/view"


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("delivery", ["model", "posted", "failed"])
@pytest.mark.parametrize("fallback", [False, True])
async def test_first_answer_and_followup_keep_the_same_used_document(
    monkeypatch: pytest.MonkeyPatch,
    isolated: MagicMock,
    fanout_enabled: bool,
    delivery: str,
    fallback: bool,
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", ME)
    monkeypatch.setattr(
        drive_first, "resolve_saved_files", lambda skill, hits, ctx: [None] * len(hits)
    )
    skill, pg = make(
        [_vault_hit(i, title=f"資料{i}", source_uri=f"gdrive://DOC{i}") for i in range(1, 4)]
    )
    skill._bedrock.converse.side_effect = (
        [_converse("根拠不明です。[S:999]"), _converse("進行表を先に共有します。")]
        if fallback
        else [_converse("進行表を先に共有します。[S:2]")]
    )
    skill._direct_answer_kind = MagicMock(return_value="comparison")
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    slack = _FakeSlack()
    if delivery != "model":
        enable(monkeypatch)
    if delivery == "failed":
        monkeypatch.setattr(
            slack, "post_message", AsyncMock(side_effect=RuntimeError("post failed"))
        )
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: slack)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0)
    result = await server.dispatch_tool(
        _spec(skill, "search"),
        "search",
        {"query": "提案の進め方を比較して"},
        identity_resolver=_resolver_for(ME),
        caller_claim_verifier=cast(Any, _Verifier(_claim())),
    )
    payload = json.loads(result[0].text)
    answer = "進行表を先に共有します。" + ("" if fallback else f"\n\n<{URL}|資料を見る>")
    expected_ids = [] if fallback else ["DOC2"]
    if delivery == "posted":
        assert payload["delivered"] is True
        assert len(slack.posts) == 1 and slack.posts[0]["text"] == answer
        if not fallback:
            assert slack.posts[0]["blocks"][0]["text"]["verbatim"] is True
        assert payload["direct_delivery"]["answer"] == answer
    else:
        assert payload["answer"] == answer
        assert payload.get("answer_format") == (None if fallback else "compact")
        assert [ref["drive_file_id"] for ref in payload["displayed_refs"]] == expected_ids
    assert payload["document_followup_available"] is not fallback
    saved = isolated.save.call_args.args[2]
    assert [ref["drive_file_id"] for ref in saved] == expected_ids
    pg.list_authorized_document_references.return_value = [
        {"source_type": "gdrive", "source_uri": "gdrive://DOC2", "external_id": "DOC2"}
    ]
    skill._bedrock.converse.reset_mock()
    followup = await server.dispatch_tool(
        _spec(skill, "search"),
        "search",
        {"query": "その資料も出して"},
        identity_resolver=_resolver_for(ME),
        caller_claim_verifier=cast(Any, _Verifier(_claim())),
    )
    restored = json.loads(followup[0].text)
    followup_answer = restored.get("direct_delivery", {}).get("answer", restored.get("answer", ""))
    assert (URL in followup_answer) is not fallback
    assert "DOC1" not in followup_answer and "DOC3" not in followup_answer
    skill._bedrock.converse.assert_not_called()
