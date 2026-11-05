"""本番の Drive 優先設定で、名指し資料検索から添付までページ番号を保つ。"""

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.mcp_gateway import direct_search_answer
from teamagent.skills.base import SkillContext
from teamagent.skills.knowledge_deliver.schema import KnowledgeDeliverInput
from teamagent.skills.knowledge_deliver.skill import KnowledgeDeliverSkill
from teamagent.skills.search import fanout
from teamagent.skills.search.composite import SlackLookup
from teamagent.skills.search.expand import ExpansionResult
from tests.skills.knowledge_deliver.test_knowledge_deliver import _gdrive_mock, _slack_mock
from tests.skills.search.test_fanout import META, make


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_named_material_chunk_metadata_page_reaches_dm(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", "*")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", "*")
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER", "1")
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS", "*")
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER_KINDS", "not_found,list_fast,comparison")
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))
    monkeypatch.setattr(fanout, "build_expansion_planner", MagicMock)
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), None, (), "empty", 1, 0.0)
    )
    search, pg = make([])  # 意味検索に出ず、名指し・資料種別の救済検索だけに出る資料。
    pg.list_client_names.return_value = ["花王"]
    search._summarize = MagicMock(return_value=("施策の全体構成です。", 0.0))
    conn = pg.connection.return_value.__enter__.return_value
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {"timeout": "0"}
    title = "提案_株式会社花王_meltプロモーション施策.pdf"
    file_id = "1sgF6klCvfR5aTn3l70GJ-1oWu8UMM5dN"
    chunk = {"content": "meltの施策の全体構成", "page_num": None, "metadata": {"page_num": "12"}}

    def rows() -> list[dict[str, object]]:
        sql = cur.execute.call_args.args[0]
        row: dict[str, object] = {
            "chunk_id": -1,
            "document_id": "melt-document",
            "title": title,
            "source_type": "gdrive",
            "source_uri": f"gdrive://{file_id}",
            "metadata": {"client_name": "花王", "cls_doc_type": "提案書"},
        }
        # DB の射影を再現し、テスト側で page_num を SearchHit に注入しない。
        if "FROM chunks c" in sql:
            row.update(content=chunk["content"], page_num=chunk["page_num"])
            if "c.metadata->>'page_num' AS metadata_page_num" in sql:
                row["metadata_page_num"] = chunk["metadata"]["page_num"]
        return [row]

    cur.fetchall.side_effect = rows
    pg.find_documents_for_advertiser.side_effect = PgVectorClient(
        "postgresql://stub"
    ).find_documents_for_advertiser
    slack = _slack_mock()
    gdrive = _gdrive_mock()
    skill = KnowledgeDeliverSkill(search=search, slack=slack, gdrive=gdrive)
    # server.py と同じく knowledge_deliver には compact だけを付け、direct_answer は付けない。
    assert direct_search_answer.load_policy().enabled
    assert direct_search_answer.allowed_kinds() == {"not_found", "list_fast", "comparison"}
    ctx = SkillContext(metadata={**META, "_search_compact_answer": True})

    out = skill.run(
        KnowledgeDeliverInput(
            query="花王のmeltの提案書で、施策の全体構成が書いてあるところをファイルで送って"
        ),
        ctx,
    )

    assert out.delivered_count == 1
    assert out.answer == (
        f"該当する提案書です。\n\n{title}\n原本: https://drive.google.com/file/d/{file_id}/view"
        "\n該当: 12 ページ"
    )
    assert all(ref.pages == [12] for ref in out.references)
    gdrive.download_file_bytes.assert_called_once_with(file_id=file_id, request_id=ctx.request_id)
    slack.upload_files.assert_awaited_once()
    assert slack.upload_files.await_args.kwargs["initial_comment"] == out.answer
    calls = pg.find_documents_for_advertiser.call_args_list
    assert any(c.kwargs.get("doc_type") == "提案書" for c in calls)
    assert all(c.kwargs["embedding"] == [0.1] * 8 for c in calls)
    if fanout_enabled:
        assert any(c.kwargs.get("doc_type") is None for c in calls)
