"""ingest/pdf_extract.py のユニットテスト。

pypdf を monkeypatch して PDF を扱わずに静的検証する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.ingest.pdf_extract import chunk_pages, extract_pdf_pages


# -----------------------------------------------------------
# chunk_pages — 純粋関数
# -----------------------------------------------------------
def test_chunk_pages_returns_one_chunk_when_text_under_size() -> None:
    """size 未満なら 1 chunk のままページ番号を付ける。"""
    pages = [(1, "短いテキスト"), (2, "もう一つ")]
    chunks = chunk_pages(pages, size=500, overlap=100)
    assert chunks == [(1, "短いテキスト"), (2, "もう一つ")]


def test_chunk_pages_splits_long_text_with_overlap() -> None:
    """500 文字超過時にチャンク化、隣接 chunk は overlap 文字重なる。"""
    text = "a" * 1200  # 1200 文字
    chunks = chunk_pages([(7, text)], size=500, overlap=100)
    # step = 500-100 = 400 → start=0,400,800,1200 → 3 chunks (最後は 1200 で打ち切り)
    assert len(chunks) == 3
    assert all(page == 7 for page, _ in chunks)
    assert all(len(c) <= 500 for _, c in chunks)


def test_chunk_pages_does_not_cross_page_boundaries() -> None:
    """ページごとに独立で chunk 化、複数ページを 1 chunk にまとめない。"""
    pages = [(1, "a" * 600), (2, "b" * 600)]
    chunks = chunk_pages(pages, size=500, overlap=100)
    page_nums = [p for p, _ in chunks]
    # page=1 の chunk が並んだあと page=2 が来る（混在しない）
    assert page_nums.count(1) >= 1
    assert page_nums.count(2) >= 1
    # 同じ page の chunk は連続している
    first_two = page_nums.index(2)
    assert all(p == 1 for p in page_nums[:first_two])
    assert all(p == 2 for p in page_nums[first_two:])


def test_chunk_pages_rejects_invalid_args() -> None:
    """不正な size / overlap で ValueError。"""
    with pytest.raises(ValueError):
        chunk_pages([(1, "x")], size=0, overlap=0)
    with pytest.raises(ValueError):
        chunk_pages([(1, "x")], size=10, overlap=10)  # overlap >= size
    with pytest.raises(ValueError):
        chunk_pages([(1, "x")], size=10, overlap=-1)


def test_chunk_pages_empty_input() -> None:
    """空入力なら空出力。"""
    assert chunk_pages([], size=500, overlap=100) == []


# -----------------------------------------------------------
# extract_pdf_pages — pypdf.PdfReader を monkeypatch
# -----------------------------------------------------------
def _make_fake_page(text: str) -> Any:
    fake = MagicMock()
    fake.extract_text.return_value = text
    return fake


def test_extract_pdf_pages_returns_per_page_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """pypdf.PdfReader が 3 ページを返すと、3 件のページタプルが返る。"""
    fake_reader = MagicMock()
    fake_reader.pages = [
        _make_fake_page("ページ 1   本文"),
        _make_fake_page("ページ 2 本文"),
        _make_fake_page("ページ 3"),
    ]
    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: fake_reader)

    pages = extract_pdf_pages(b"fake-pdf-bytes")
    assert pages == [
        (1, "ページ 1 本文"),  # 連続空白が 1 つに圧縮されている
        (2, "ページ 2 本文"),
        (3, "ページ 3"),
    ]


def test_extract_pdf_pages_skips_empty_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """テキスト未抽出のページ（画像のみ等）は結果から除外される。"""
    fake_reader = MagicMock()
    fake_reader.pages = [
        _make_fake_page("有効ページ"),
        _make_fake_page("   "),  # 空白のみ → 除外
        _make_fake_page(""),  # 空文字 → 除外
        _make_fake_page("最後のページ"),
    ]
    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: fake_reader)

    pages = extract_pdf_pages(b"fake")
    # 1-indexed なので 1 と 4 のみ
    assert pages == [(1, "有効ページ"), (4, "最後のページ")]
