"""構造化事例から元deck IDを失わないSQL射影。"""

from tests.adapters.test_pgvector_case_studies import _client, _Conn


def test_case_lookup_projects_original_drive_id_without_changing_population() -> None:
    row = {"document_id": "case-1", "case_source": "deck", "drive_file_id": "DECK"}
    conn = _Conn([row])
    result = _client().list_case_studies(
        conn,
        industry="食品",
        stage=4,
        soft_industry=True,
        limit=30,  # type: ignore[arg-type]
    )
    assert result == [row]
    assert "d.metadata->>'drive_file_id'" in conn.sql[0]
    assert "AS drive_file_id" in conn.sql[0]
    assert "d.metadata->>'case_corpus' = 'true'" in conn.sql[0]
