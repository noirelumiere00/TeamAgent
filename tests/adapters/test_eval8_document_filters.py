from unittest.mock import MagicMock

from teamagent.adapters.pgvector_client import PgVectorClient
from tests.adapters.test_pgvector_filters import _run


def test_source_explanation_filter_is_exact_bound_and_empty_is_closed() -> None:
    base_sql, base_params = _run()
    assert _run(filter_drive_file_ids=None) == (base_sql, base_params)
    for ids in (["ORIGINAL"], []):
        sql, params = _run(filter_drive_file_ids=ids)
        assert "d.source_type = 'gdrive' AND d.external_id = ANY(%s)" in sql
        assert ids in params
        assert "ORIGINAL" not in sql


def test_named_material_type_is_filtered_before_limit() -> None:
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [
        {
            "chunk_id": -1,
            "document_id": "proposal",
            "title": "提案_{client_a}【ご提案書】.pdf",
            "source_uri": "gdrive://PROPOSAL",
            "source_type": "gdrive",
            "metadata": {},
        }
    ]
    hits = PgVectorClient(dsn="postgresql://stub").find_documents_for_advertiser(
        conn, "{client_a}", drive_only=True, doc_type="提案書"
    )
    sql, params = cursor.execute.call_args.args
    assert sql.index("CASE WHEN") < sql.index("LIMIT")
    assert "競合資料" in sql and "報告書" in sql
    assert params[-2:] == ["提案書", 8]
    assert "{client_a}" not in sql
    assert hits[0].content == "提案_{client_a}【ご提案書】.pdf"
