"""NUL バイト (0x00) サニタイズのユニットテスト。

PDF / Doc 抽出時に NUL バイトが混入することがあり、PostgreSQL の TEXT 列は
NUL を許容しないため DataError になる。Day 7 (2026-05-27) で防御策を導入。
"""

from __future__ import annotations

from teamagent.ingest.repository import _sanitize_metadata, _strip_nul


def test_strip_nul_removes_null_bytes() -> None:
    assert _strip_nul("hello\x00world") == "helloworld"


def test_strip_nul_preserves_normal_strings() -> None:
    assert _strip_nul("normal text") == "normal text"
    assert _strip_nul("日本語含み 全角") == "日本語含み 全角"


def test_strip_nul_passes_through_none() -> None:
    assert _strip_nul(None) is None


def test_strip_nul_handles_empty_string() -> None:
    assert _strip_nul("") == ""


def test_strip_nul_handles_only_null() -> None:
    assert _strip_nul("\x00\x00\x00") == ""


def test_sanitize_metadata_strips_nul_in_string_values() -> None:
    meta = {"key1": "value\x00null", "key2": "clean"}
    out = _sanitize_metadata(meta)
    assert out["key1"] == "valuenull"
    assert out["key2"] == "clean"


def test_sanitize_metadata_recurses_into_nested_dicts() -> None:
    meta = {"outer": {"inner": "value\x00null"}}
    out = _sanitize_metadata(meta)
    assert out["outer"]["inner"] == "valuenull"


def test_sanitize_metadata_handles_lists() -> None:
    meta = {"tags": ["clean", "with\x00null", "also clean"]}
    out = _sanitize_metadata(meta)
    assert out["tags"] == ["clean", "withnull", "also clean"]


def test_sanitize_metadata_preserves_non_string_values() -> None:
    meta = {"count": 42, "ratio": 0.5, "active": True, "ts": None}
    out = _sanitize_metadata(meta)
    assert out == meta


def test_sanitize_metadata_handles_empty_dict() -> None:
    assert _sanitize_metadata({}) == {}
