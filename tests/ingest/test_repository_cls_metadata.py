"""既存分類 metadata の一括読み出し契約を実 DB なしで検証する。"""

from __future__ import annotations

from typing import Any

from teamagent.ingest.repository import IngestRepository

_EXPECTED_CLASSIFICATION_KEYS = {
    "cls_project",
    "cls_industry",
    "cls_doc_type",
    "cls_phase",
    "cls_solution",
    "cls_budget",
    "cls_target",
    "cls_is_template",
    "cls_is_recurring",
    "cls_entities",
    "industry",
}


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _FakeConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.cursor_instance = _FakeCursor(rows)

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cursor(self, *, row_factory: Any = None) -> _FakeCursor:
        return self.cursor_instance


class _FakePgVector:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.connection_instance = _FakeConnection(rows)
        self.connection_calls: list[dict[str, Any]] = []

    def connection(self, **kwargs: Any) -> _FakeConnection:
        self.connection_calls.append(kwargs)
        return self.connection_instance


def test_get_document_classification_metadata_reads_exact_keys_in_one_query() -> None:
    all_values = {key: f"value:{key}" for key in _EXPECTED_CLASSIFICATION_KEYS}
    pgvector = _FakePgVector(
        [
            {
                "source_type": "gdrive",
                "external_id": "FILE-1",
                "classification_metadata": {
                    **all_values,
                    "client_name": "分類外",
                    "cls_future": "未許可キー",
                },
            },
            {
                "source_type": "slack",
                "external_id": "C1:1.0",
                "classification_metadata": {},
            },
        ]
    )
    repository = IngestRepository(pgvector, owner_email="bot@example.com")  # type: ignore[arg-type]

    result = repository.get_document_classification_metadata(
        [("gdrive", "FILE-1"), ("slack", "C1:1.0")]
    )

    assert result == {
        ("gdrive", "FILE-1"): all_values,
        ("slack", "C1:1.0"): {},
    }
    assert len(pgvector.connection_calls) == 1
    [executed] = pgvector.connection_instance.cursor_instance.executed
    sql, params = executed
    assert "jsonb_each(d.metadata)" in sql
    assert "unnest(%s::text[], %s::text[])" in sql
    assert "d.source_type = requested.source_type::document_source_type" in sql
    assert set(params[0]) == _EXPECTED_CLASSIFICATION_KEYS
    assert len(params[0]) == len(_EXPECTED_CLASSIFICATION_KEYS)
    assert "industry" in params[0]
    assert params[1] == ["gdrive", "slack"]
    assert params[2] == ["FILE-1", "C1:1.0"]


def test_get_document_classification_metadata_empty_input_skips_database() -> None:
    pgvector = _FakePgVector([])
    repository = IngestRepository(pgvector, owner_email="bot@example.com")  # type: ignore[arg-type]

    assert repository.get_document_classification_metadata([]) == {}
    assert pgvector.connection_calls == []
    assert pgvector.connection_instance.cursor_instance.executed == []
