"""PDF テキスト抽出 + チャンク化ユーティリティ。

ingest パイプライン用の純粋関数群。
pypdf を遅延 import（CI で OpenSSL 等が無い環境でもモジュール import は通すため）。

Usage:
    from teamagent.ingest.pdf_extract import extract_pdf_pages, chunk_pages

    pages = extract_pdf_pages(pdf_bytes)
    # → [(1, "page1 text..."), (2, "page2 text..."), ...]

    chunks = chunk_pages(pages, size=500, overlap=100)
    # → [(1, "chunk1"), (1, "chunk2"), (2, "chunk3"), ...]
"""

from __future__ import annotations

import re
from io import BytesIO


def extract_pdf_pages(data: bytes) -> list[tuple[int, str]]:
    """PDF バイナリをページごとのテキストに分解する。

    戻り値: [(page_num_1_indexed, text), ...]
    テキスト未抽出のページ（画像のみ等）は除外する。
    """
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    pages: list[tuple[int, str]] = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        # 連続空白を 1 スペースに圧縮
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            pages.append((i, text))
    return pages


def chunk_pages(
    pages: list[tuple[int, str]],
    size: int = 500,
    overlap: int = 100,
) -> list[tuple[int, str]]:
    """ページ単位のテキストを文字数ベースのチャンクに分割する。

    ページ境界は跨がない（chunk が複数ページに渡らない）。
    1 chunk のサイズは size 文字、隣接 chunk と overlap 文字オーバーラップ。

    戻り値: [(page_num, chunk_text), ...]
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be in [0, size)")

    out: list[tuple[int, str]] = []
    step = size - overlap
    for page_num, text in pages:
        if len(text) <= size:
            out.append((page_num, text))
            continue
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            chunk = text[start:end].strip()
            if chunk:
                out.append((page_num, chunk))
            if end == len(text):
                break
            start += step
    return out
