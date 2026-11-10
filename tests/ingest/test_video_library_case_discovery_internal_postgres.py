"""Real document counting and escaped ILIKE; synthetic rows in a rollback-only schema."""

import uuid
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from teamagent.ingest import video_library_case_discovery as discovery
from tests.connect_web.test_knowledge_list_postgres import db as knowledge_db
from tests.ingest.test_video_library_postgres import _DSN, TransactionPg

__all__ = ["knowledge_db"]
pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEAMAGENT_TEST_DB_DSN")


def document(
    conn: Any,
    metadata: dict[str, Any],
    *,
    title: str = "資料",
    contents: tuple[str, ...] = (),
) -> None:
    doc_id = uuid.uuid4()
    conn.execute(
        """INSERT INTO documents(id,title,source_type,metadata,owner_email,acl_emails,acl_groups)
           VALUES (%s,%s,'gdrive',%s,'other@example.test','{}',ARRAY['example.test'])""",
        (doc_id, title, Jsonb(metadata)),
    )
    for index, content in enumerate(contents):
        conn.execute(
            "INSERT INTO chunks(id,document_id,chunk_idx,content,metadata) VALUES (%s,%s,%s,%s,'{}')",
            (uuid.uuid4(), doc_id, index, content),
        )


def test_lookup_counts_documents_once_excludes_research_and_limits_top_five(
    knowledge_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = knowledge_db
    monkeypatch.setenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", "example.test")
    for _ in range(2):
        document(
            conn,
            {"client_name": "株式会社OVER's", "cls_project": "別の取引先"},
            title="ガラスザムライ カルテ",
        )
        document(
            conn, {"client_name": "OVER's"}, title="別のタイトル", contents=("ガラスザムライ",) * 5
        )
    document(conn, {"client_name": "  ", "cls_project": "株式会社OVER's"}, title="ガラスザムライ")
    for i in range(4):
        document(conn, {"client_name": f"その他{i}"}, title="ガラスザムライ")
    document(conn, {"client_name": None, "cls_project": " "}, title="ガラスザムライ")
    document(conn, {}, title="ガラスザムライ")
    document(conn, {"client_name": "無関係"}, title="ガラス商品")
    for _ in range(10):
        document(
            conn,
            {
                "client_name": "研究ツール",
                "cls_project": "施策ノート",
                "x_research_tool": "research",
            },
            title="ガラスザムライ",
        )
    matches = discovery.lookup_internal_clients(
        TransactionPg(conn),
        "ガラスザムライ",
        "ガラスザムライ",
        request_id="db-internal",  # type: ignore[arg-type]
    )
    assert len(matches) == 5
    assert [(m.client_name, m.document_count) for m in matches[:2]] == [
        ("株式会社OVER's", 3),
        ("OVER's", 2),
    ]
    assert all(m.document_count == 1 and m.client_name.startswith("その他") for m in matches[2:])
    assert discovery.select_internal_advertiser(matches, []) == "OVER's"
    assert conn.execute(
        "SELECT current_user AS role,current_setting('app.user_role') AS user_role,current_setting('app.user_groups') AS groups"
    ).fetchone() == {
        "role": "teamagent_app",
        "user_role": "admin",
        "groups": "example.test",
    }


@pytest.mark.parametrize(
    "term,decoy",
    [("50%_Q", "50extraXQ"), ("a_b", "axb"), ("x\\y", "xy"), ("x%' OR true --", "xother")],
)
def test_ilike_wildcards_backslashes_and_quotes_are_literal(
    knowledge_db: Any,
    term: str,
    decoy: str,
) -> None:
    conn = knowledge_db
    document(conn, {"client_name": "正しい会社"}, title=f"{term} カルテ")
    document(conn, {"cls_project": "正しい会社"}, contents=(f"本文に {term}", f"もう一度 {term}"))
    for _ in range(4):
        document(conn, {"client_name": "誤一致する会社"}, title=decoy, contents=(decoy,))
    matches = discovery.lookup_internal_clients(
        TransactionPg(conn),
        term,
        "unmatched-tag",
        request_id="db-literal",  # type: ignore[arg-type]
    )
    assert [(m.client_name, m.document_count) for m in matches] == [("正しい会社", 2)]


def test_failed_lookup_keeps_enclosing_transaction_usable(knowledge_db: Any) -> None:
    conn = knowledge_db
    conn.execute("DROP TABLE chunks")
    assert (
        discovery.lookup_internal_clients(
            TransactionPg(conn),
            "QTTA",
            "qtta",
            request_id="db-failed",  # type: ignore[arg-type]
        )
        == []
    )
    assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 0
