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

import structlog

logger = structlog.get_logger(__name__)


def extract_pdf_pages(data: bytes, *, min_chars: int = 0) -> list[tuple[int, str]]:
    """PDF バイナリをページごとのテキストに分解する。

    戻り値: [(page_num_1_indexed, text), ...]
    テキスト未抽出のページ（画像のみ等）は除外する。

    Args:
        data: PDF バイナリ。
        min_chars: 最小文字数ガード。``> 0`` のとき、空白圧縮後のテキストが
            ``min_chars`` 未満のページを空扱いで除外する。スキャン PDF
            （pypdf がテキスト 0／極小を返す）を「中身あり」と誤認しないため。
            既定 0 は現行挙動（空文字だけ除外）。後方互換。

    Notes:
        暗号化 PDF は ``reader.is_encrypted`` を見て空パスワードで decrypt を試み、
        失敗時は ``pdf_encrypted`` 構造化ログを出す（corrupt とは区別する）。
        decrypt は無条件で試行し、抽出自体の例外は現行同様 上位に投げる
        （pipeline 側で握る）。

        ``min_chars > 0`` でページが落ちた場合、歩留まり観測用に
        ``low_text_yield`` 構造化ログを出す（戻り値の形は現状維持）。
    """
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))

    # --- J: 暗号化シグナル（無条件・log だけ） ---
    # is_encrypted が True なら空 PW で decrypt を試行。失敗しても抽出は続行し
    # （ページが読めなければ後段で例外→上位が握る）、専用ログで corrupt と区別する。
    if getattr(reader, "is_encrypted", False):
        try:
            # pypdf の decrypt は 0=FAILED / 1=USER PW / 2=OWNER PW を返す。
            result = reader.decrypt("")
        except Exception:
            # decrypt 自体が例外（AES 等のアルゴリズム未対応など）でも、
            # ここでは log だけ出して抽出を続行する（例外は後段／上位に委ねる）。
            logger.warning("pdf_encrypted", reason="decrypt_raised", empty_password=True)
        else:
            if not result:
                logger.warning("pdf_encrypted", reason="decrypt_failed", empty_password=True)
            else:
                logger.info("pdf_encrypted", reason="decrypt_ok", empty_password=True)

    pages: list[tuple[int, str]] = []
    total_pages = 0
    low_text_pages = 0
    for i, page in enumerate(reader.pages, start=1):
        total_pages += 1
        text = page.extract_text() or ""
        # 連続空白を 1 スペースに圧縮
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        # --- D: 最小文字数ガード ---
        if min_chars > 0 and len(text) < min_chars:
            low_text_pages += 1
            continue
        pages.append((i, text))

    # --- D: 低テキスト歩留まりの観測用ログ（戻り値の形は維持） ---
    if min_chars > 0 and low_text_pages > 0:
        logger.info(
            "low_text_yield",
            min_chars=min_chars,
            total_pages=total_pages,
            low_text_pages=low_text_pages,
            kept_pages=len(pages),
        )

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
