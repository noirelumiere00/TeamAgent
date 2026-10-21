"""資料名からの Drive URL 解決は、strict 時だけ一意な file ID を要求する。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from teamagent.adapters.pgvector_client import PgVectorClient


def _connection(*batches: list[dict[str, Any]]) -> tuple[MagicMock, MagicMock]:
    cursor = MagicMock()
    cursor.fetchall.side_effect = batches
    cursor.__enter__.return_value = cursor
    connection = MagicMock()
    connection.cursor.return_value = cursor
    return connection, cursor


def _file(title: str, file_id: str, uri: str | None = None) -> dict[str, str]:
    return {
        "title": title,
        "external_id": file_id,
        "source_uri": uri or f"https://drive.google.com/file/d/{file_id}/view",
    }


def test_strict_ambiguous_exact_title_does_not_retry_prefix() -> None:
    title = "shared_material.pdf"
    conn, cursor = _connection(
        [_file(title, "FILE_A"), _file(title, "FILE_B")],
        [_file("shared_material_revision.pdf", "FILE_C")],
    )
    client = PgVectorClient(dsn="postgresql://stub")

    assert client.resolve_file_urls_by_titles(conn, [title], strict=True) == {}
    assert cursor.execute.call_count == 1


def test_strict_same_file_id_has_one_canonical_url() -> None:
    title = "shared_material.pdf"
    conn, _ = _connection(
        [
            _file(title, "FILE_A", "gdrive://FILE_A"),
            _file(title, "FILE_A", "https://drive.google.com/open?id=FILE_A"),
        ]
    )

    assert PgVectorClient(dsn="postgresql://stub").resolve_file_urls_by_titles(
        conn, [title], strict=True
    ) == {title: "https://drive.google.com/file/d/FILE_A/view"}


def test_strict_ambiguous_prefix_is_unresolved() -> None:
    title = "shared_material.pdf"
    conn, _ = _connection(
        [],
        [_file("shared_material.pptx", "FILE_A"), _file("shared_material_v2.pdf", "FILE_B")],
    )

    assert (
        PgVectorClient(dsn="postgresql://stub").resolve_file_urls_by_titles(
            conn, [title], strict=True
        )
        == {}
    )


@pytest.mark.parametrize("suffix", ["_v2", "_revision", "_20261020"])
def test_strict_unique_prefix_does_not_guess_a_different_version(suffix: str) -> None:
    title = "shared_material.pdf"
    conn, _ = _connection([], [_file(f"shared_material{suffix}.pdf", "FILE_A")])

    assert (
        PgVectorClient(dsn="postgresql://stub").resolve_file_urls_by_titles(
            conn, [title], strict=True
        )
        == {}
    )


def test_strict_extension_and_surrounding_spaces_can_match() -> None:
    title = " shared_material.pdf "
    conn, _ = _connection([], [_file(" shared_material.pptx ", "FILE_A")])

    assert PgVectorClient(dsn="postgresql://stub").resolve_file_urls_by_titles(
        conn, [title], strict=True
    ) == {title.strip(): "https://drive.google.com/file/d/FILE_A/view"}


def test_strict_extension_match_uses_literal_stem_and_distinct_ids() -> None:
    title = "shared_100%_material.pdf"
    conn, cursor = _connection([], [_file("shared_100%_material.pptx", "FILE_A")])

    assert PgVectorClient(dsn="postgresql://stub").resolve_file_urls_by_titles(
        conn, [title], strict=True
    ) == {title: "https://drive.google.com/file/d/FILE_A/view"}
    sql, params = cursor.execute.call_args.args
    assert "SELECT DISTINCT d.external_id" in sql
    assert "LIMIT 2" in sql
    assert "regexp_replace(btrim(d.title), '\\.[^.]+$', '') = %s" in sql
    assert "ILIKE" not in sql
    assert params == ["shared_100%_material"]


def test_strict_uses_supplied_rls_connection_without_reconnecting() -> None:
    title = "shared_material.pdf"
    conn, cursor = _connection([_file(title, "FILE_A")])
    client = PgVectorClient(dsn="postgresql://stub")

    with patch.object(client, "connection") as reconnect:
        client.resolve_file_urls_by_titles(conn, [title], strict=True)

    reconnect.assert_not_called()
    sql, params = cursor.execute.call_args.args
    assert "FROM documents d" in sql
    assert "d.source_type = 'gdrive'" in sql
    assert "d.metadata->>'suppressed' IS DISTINCT FROM 'true'" in sql
    assert params == [[title]]


@pytest.mark.parametrize("kwargs", [{}, {"strict": False}])
def test_legacy_exact_title_keeps_first_url(kwargs: dict[str, bool]) -> None:
    title = "shared_material.pdf"
    conn, cursor = _connection([_file(title, "FILE_A"), _file(title, "FILE_B")])

    assert PgVectorClient(dsn="postgresql://stub").resolve_file_urls_by_titles(
        conn, [title], **kwargs
    ) == {title: "https://drive.google.com/file/d/FILE_A/view"}
    assert cursor.execute.call_count == 1


def test_legacy_gdrive_uri_remains_unresolved() -> None:
    title = "shared_material.pdf"
    conn, _ = _connection([_file(title, "FILE_A", "gdrive://FILE_A")], [])

    assert PgVectorClient(dsn="postgresql://stub").resolve_file_urls_by_titles(conn, [title]) == {}


def test_legacy_prefix_keeps_original_url_and_pattern() -> None:
    title = "shared_100%_material.pdf"
    uri = "https://drive.google.com/open?id=FILE_A"
    conn, cursor = _connection([], [_file("shared_100%_material.pptx", "FILE_A", uri)])

    assert PgVectorClient(dsn="postgresql://stub").resolve_file_urls_by_titles(conn, [title]) == {
        title: uri
    }
    sql, params = cursor.execute.call_args.args
    assert "SELECT DISTINCT" not in sql
    assert "ESCAPE" not in sql
    assert params == ["shared_100%_material%"]


@pytest.mark.parametrize("exact", [True, False])
def test_strict_invalid_file_id_is_unresolved(exact: bool) -> None:
    title = "shared_material.pdf"
    invalid = _file(title, "../invalid")
    conn, cursor = _connection([invalid] if exact else [], [invalid])

    assert (
        PgVectorClient(dsn="postgresql://stub").resolve_file_urls_by_titles(
            conn, [title], strict=True
        )
        == {}
    )
    assert cursor.execute.call_count == (1 if exact else 2)
