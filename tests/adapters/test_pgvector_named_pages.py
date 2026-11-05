from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import PgVectorClient


@pytest.mark.parametrize(
    "column,metadata,expected",
    [
        (None, "12", 12),
        (7, "12", 7),
        (0, "12", None),
        (None, "0", None),
        (None, "1.5", None),
        (None, "bad", None),
        (None, None, None),
    ],
)
def test_named_material_uses_matching_chunk_page(
    column: object, metadata: object, expected: int | None
) -> None:
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        {
            "chunk_id": -1,
            "document_id": "proposal",
            "title": "提案書.pdf",
            "source_type": "gdrive",
            "metadata": {"page_num": 99},  # 文書単位の値は本文ページの根拠にしない。
            "content": "施策の全体構成",
            "page_num": column,
            "metadata_page_num": metadata,
        }
    ]
    hits = PgVectorClient("postgresql://stub").find_documents_for_advertiser(
        conn, "花王", embedding=[0.1], embedding_col="embedding_cohere", doc_type="提案書"
    )

    assert hits[0].metadata.get("page_num") == expected
    assert hits[0].content == "提案書.pdf"  # 本文は従来どおり（doc_type 指定時は資料名）
    sql, params = cur.execute.call_args.args
    assert "c.page_num, c.metadata->>'page_num' AS metadata_page_num" in sql
    assert "c.document_id = d.id AND d.source_type = 'gdrive'" in sql
    assert "ORDER BY c.embedding_cohere <=> %s::vector, c.chunk_idx" in sql
    assert "c.metadata->>'title_only' IS DISTINCT FROM 'true'" in sql
    assert "c.metadata->>'boilerplate' IS DISTINCT FROM 'true'" in sql
    assert params == [[0.1], "%花王%", "%花王%", "%花王%", "提案書", 8]


def test_named_material_rejects_unknown_embedding_column() -> None:
    conn = MagicMock()
    with pytest.raises(ValueError, match="embedding_col"):
        PgVectorClient("postgresql://stub").find_documents_for_advertiser(
            conn, "花王", embedding=[0.1], embedding_col="injected"
        )
    conn.cursor.assert_not_called()
