"""ingest/pdf_extract.py のユニットテスト。

pypdf を monkeypatch して PDF を扱わずに静的検証する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.ingest.pdf_extract import (
    ChunkLimitExceededError,
    chunk_pages,
    extract_pdf_pages,
)


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


def test_chunk_pages_enforces_limit_while_building_output() -> None:
    with pytest.raises(ChunkLimitExceededError, match="exceeded 2"):
        chunk_pages(
            [(1, "a" * 10_000)],
            size=500,
            overlap=100,
            max_chunks=2,
        )


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


# -----------------------------------------------------------
# D: min_chars 最小文字数ガード
# -----------------------------------------------------------
def test_extract_pdf_pages_min_chars_default_is_backward_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """min_chars 既定 0 では極小テキストも残る（現行挙動）。"""
    fake_reader = MagicMock()
    fake_reader.is_encrypted = False
    fake_reader.pages = [
        _make_fake_page("a"),  # 1 文字
        _make_fake_page("十分に長い本文テキスト"),
    ]
    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: fake_reader)

    pages = extract_pdf_pages(b"fake")
    assert pages == [(1, "a"), (2, "十分に長い本文テキスト")]


def test_extract_pdf_pages_min_chars_drops_tiny_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """min_chars 以上の文字数のページだけ残り、極小ページは空扱いで落ちる。"""
    fake_reader = MagicMock()
    fake_reader.is_encrypted = False
    fake_reader.pages = [
        _make_fake_page("ab"),  # 2 文字 → min_chars=5 未満で除外
        _make_fake_page("有効な本文ページ"),  # 8 文字 → 残る
        _make_fake_page("xy"),  # 2 文字 → 除外
        _make_fake_page("もう一つの本文"),  # 7 文字 → 残る
    ]
    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: fake_reader)

    pages = extract_pdf_pages(b"fake", min_chars=5)
    # 1-indexed で page 2, 4 のみ残る
    assert pages == [(2, "有効な本文ページ"), (4, "もう一つの本文")]


def test_extract_pdf_pages_min_chars_boundary_inclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ちょうど min_chars 文字のページは残る（< min_chars だけ落とす）。"""
    fake_reader = MagicMock()
    fake_reader.is_encrypted = False
    fake_reader.pages = [
        _make_fake_page("abcd"),  # 4 文字 = min_chars → 残る
        _make_fake_page("abc"),  # 3 文字 < min_chars → 除外
    ]
    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: fake_reader)

    pages = extract_pdf_pages(b"fake", min_chars=4)
    assert pages == [(1, "abcd")]


def test_extract_pdf_pages_min_chars_logs_low_text_yield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """min_chars で落ちたページがあると low_text_yield 構造化ログを出す。"""
    captured: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _fake_info(*args: Any, **kwargs: Any) -> None:
        captured.append((args, kwargs))

    monkeypatch.setattr("teamagent.ingest.pdf_extract.logger.info", _fake_info)

    fake_reader = MagicMock()
    fake_reader.is_encrypted = False
    fake_reader.pages = [
        _make_fake_page("ab"),  # 落ちる
        _make_fake_page("有効な本文ページ"),  # 残る
    ]
    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: fake_reader)

    extract_pdf_pages(b"fake", min_chars=5)

    events = [a[0] for a, _ in captured]
    assert "low_text_yield" in events
    # low_text_yield の kwargs に歩留まり情報が乗る
    low = next(kw for (a, kw) in captured if a and a[0] == "low_text_yield")
    assert low["min_chars"] == 5
    assert low["low_text_pages"] == 1
    assert low["kept_pages"] == 1
    assert low["total_pages"] == 2


def test_extract_pdf_pages_min_chars_no_log_when_nothing_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全ページが min_chars 以上なら low_text_yield ログは出ない。"""
    captured: list[str] = []

    def _fake_info(event: str = "", *args: Any, **kwargs: Any) -> None:
        captured.append(event)

    monkeypatch.setattr("teamagent.ingest.pdf_extract.logger.info", _fake_info)

    fake_reader = MagicMock()
    fake_reader.is_encrypted = False
    fake_reader.pages = [
        _make_fake_page("十分に長い本文1"),
        _make_fake_page("十分に長い本文2"),
    ]
    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: fake_reader)

    extract_pdf_pages(b"fake", min_chars=3)
    assert "low_text_yield" not in captured


# -----------------------------------------------------------
# J: 暗号化シグナル（decrypt 試行 + ログ）
# -----------------------------------------------------------
def test_extract_pdf_pages_encrypted_attempts_decrypt_and_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """is_encrypted=True なら空 PW で decrypt を試み、失敗時 pdf_encrypted を warn。"""
    warnings: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _fake_warning(*args: Any, **kwargs: Any) -> None:
        warnings.append((args, kwargs))

    monkeypatch.setattr("teamagent.ingest.pdf_extract.logger.warning", _fake_warning)

    fake_reader = MagicMock()
    fake_reader.is_encrypted = True
    fake_reader.decrypt.return_value = 0  # FAILED（空 PW では開けない）
    fake_reader.pages = [_make_fake_page("復号できなかった本文")]
    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: fake_reader)

    extract_pdf_pages(b"fake")

    # 空 PW で decrypt が試行された
    fake_reader.decrypt.assert_called_once_with("")
    # pdf_encrypted で warn された（corrupt とは別イベント）
    events = [a[0] for a, _ in warnings if a]
    assert "pdf_encrypted" in events
    pe = next(kw for (a, kw) in warnings if a and a[0] == "pdf_encrypted")
    assert pe["reason"] == "decrypt_failed"
    assert pe["empty_password"] is True


def test_extract_pdf_pages_encrypted_decrypt_success_logs_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空 PW で decrypt 成功なら pdf_encrypted decrypt_ok を info ログし本文も取れる。"""
    infos: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _fake_info(*args: Any, **kwargs: Any) -> None:
        infos.append((args, kwargs))

    monkeypatch.setattr("teamagent.ingest.pdf_extract.logger.info", _fake_info)

    fake_reader = MagicMock()
    fake_reader.is_encrypted = True
    fake_reader.decrypt.return_value = 1  # USER PW で開けた
    fake_reader.pages = [_make_fake_page("復号できた本文")]
    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: fake_reader)

    pages = extract_pdf_pages(b"fake")

    fake_reader.decrypt.assert_called_once_with("")
    events = [a[0] for a, _ in infos if a]
    assert "pdf_encrypted" in events
    pe = next(kw for (a, kw) in infos if a and a[0] == "pdf_encrypted")
    assert pe["reason"] == "decrypt_ok"
    # 復号後にページ本文が取れている
    assert pages == [(1, "復号できた本文")]


def test_extract_pdf_pages_encrypted_decrypt_raises_logs_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decrypt 自体が例外でも pdf_encrypted を warn して抽出を続行する。"""
    warnings: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _fake_warning(*args: Any, **kwargs: Any) -> None:
        warnings.append((args, kwargs))

    monkeypatch.setattr("teamagent.ingest.pdf_extract.logger.warning", _fake_warning)

    fake_reader = MagicMock()
    fake_reader.is_encrypted = True
    fake_reader.decrypt.side_effect = NotImplementedError("AES unsupported")
    fake_reader.pages = [_make_fake_page("本文")]
    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: fake_reader)

    pages = extract_pdf_pages(b"fake")

    pe = next(kw for (a, kw) in warnings if a and a[0] == "pdf_encrypted")
    assert pe["reason"] == "decrypt_raised"
    # 抽出は続行され本文が取れる
    assert pages == [(1, "本文")]


def test_extract_pdf_pages_not_encrypted_skips_decrypt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """is_encrypted=False では decrypt を呼ばない（無駄な復号試行をしない）。"""
    fake_reader = MagicMock()
    fake_reader.is_encrypted = False
    fake_reader.pages = [_make_fake_page("通常本文")]
    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: fake_reader)

    pages = extract_pdf_pages(b"fake")

    fake_reader.decrypt.assert_not_called()
    assert pages == [(1, "通常本文")]


# -----------------------------------------------------------
# hard cap: max_pages / max_total_chars（decompression bomb 対策）
#   office_extract の zip-bomb 上限群に相当するガードが PDF 側には無かった。
#   既定（None）は無制限＝現行挙動と完全一致であることも固定する。
# -----------------------------------------------------------
def test_extract_pdf_pages_caps_default_to_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定は無制限（後方互換）。"""
    fake_reader = MagicMock()
    fake_reader.is_encrypted = False
    fake_reader.pages = [_make_fake_page(f"ページ{i}") for i in range(1, 51)]
    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: fake_reader)

    assert len(extract_pdf_pages(b"fake")) == 50


def test_extract_pdf_pages_max_pages_stops_scanning(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_pages 超過分は extract_text すら呼ばない（走査ごと打ち切る）。"""
    fake_reader = MagicMock()
    fake_reader.is_encrypted = False
    pages_in = [_make_fake_page(f"ページ{i}") for i in range(1, 11)]
    fake_reader.pages = pages_in
    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: fake_reader)

    out = extract_pdf_pages(b"fake", max_pages=3)
    assert [n for n, _ in out] == [1, 2, 3]
    pages_in[3].extract_text.assert_not_called()


def test_extract_pdf_pages_max_total_chars_truncates_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """総文字数 cap に到達したらその場で切り、以降のページは走査しない。"""
    fake_reader = MagicMock()
    fake_reader.is_encrypted = False
    pages_in = [_make_fake_page("あ" * 100) for _ in range(5)]
    fake_reader.pages = pages_in
    monkeypatch.setattr("pypdf.PdfReader", lambda _stream: fake_reader)

    out = extract_pdf_pages(b"fake", max_total_chars=250)
    assert sum(len(t) for _, t in out) == 250
    assert [n for n, _ in out] == [1, 2, 3]
    assert len(out[2][1]) == 50  # 3 ページ目は cap までで切られる
    pages_in[3].extract_text.assert_not_called()


def test_extract_pdf_pages_rejects_invalid_caps() -> None:
    with pytest.raises(ValueError):
        extract_pdf_pages(b"fake", max_pages=0)
    with pytest.raises(ValueError):
        extract_pdf_pages(b"fake", max_total_chars=0)
