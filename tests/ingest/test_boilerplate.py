"""ingest/boilerplate.py のテスト。

実 Postgres は使わず、``mark_boilerplate`` が発行する 2 本の UPDATE
（付与／除去）の**意味論**を Python で忠実にエミュレートする fake conn で検証する。
fake は本番 SQL と同じ正規化（lower → btrim → 連続空白圧縮 → md5）を Python で
再現し、``COUNT(DISTINCT document_id) >= min_docs`` でテンプレ指紋を決める。

検証する契約:
- 3 資料に同一テキスト → boilerplate=true が付く。
- 2 資料だけなら付かない（min_docs=3 既定）。
- 一度付いた印が、閾値割れ（資料が減る等）で除去される＝冪等＆自己修正。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from teamagent.ingest.boilerplate import mark_boilerplate


def _normalize(content: str) -> str:
    """指紋/長さ計算の前段（lower → strip → 連続空白圧縮）。本番 SQL の Python 再現。"""
    return re.sub(r"\s+", " ", content.strip().lower())


def _normalize_fp(content: str) -> str:
    """本番 SQL（md5(regexp_replace(lower(btrim(content)),'\\s+',' ','g'))）の Python 再現。"""
    return hashlib.md5(_normalize(content).encode("utf-8"), usedforsecurity=False).hexdigest()


class _FakeChunk:
    def __init__(self, document_id: str, content: str, metadata: dict[str, Any]) -> None:
        self.document_id = document_id
        self.content = content
        self.metadata = metadata


class _FakeCursor:
    """mark_boilerplate が発行する add / remove UPDATE の意味論をエミュレートする。

    実 SQL のパースはせず、SQL 文字列に ``jsonb_set`` が含まれれば付与、
    ``- 'boilerplate'`` が含まれれば除去、と判別して同じ結果を再現する。
    rowcount は「実際に変化した行数」を返す（本番 UPDATE の WHERE で無変更行を
    除外しているのと同じ意味）。

    指紋集計は本番 CTE と同じく、(M2) 正規化長 < min_chars の chunk と
    (M3) suppressed doc の chunk を母数から除外する。``SET LOCAL`` 等の本番外 SQL は
    no-op として受け流す（H1 配線テストは pipeline 側で行う）。
    """

    def __init__(self, chunks: list[_FakeChunk], suppressed_doc_ids: set[str]) -> None:
        self._chunks = chunks
        self._suppressed = suppressed_doc_ids
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def _boilerplate_fingerprints(self, min_docs: int, min_chars: int) -> set[str]:
        # M2: 正規化長 < min_chars の chunk は除外。M3: suppressed doc の chunk は除外。
        docs_per_fp: dict[str, set[str]] = {}
        for c in self._chunks:
            if len(_normalize(c.content)) < min_chars:
                continue
            if c.document_id in self._suppressed:
                continue
            docs_per_fp.setdefault(_normalize_fp(c.content), set()).add(c.document_id)
        return {fp for fp, docs in docs_per_fp.items() if len(docs) >= min_docs}

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        min_chars, min_docs = params
        bp_fps = self._boilerplate_fingerprints(int(min_docs), int(min_chars))
        changed = 0
        if "jsonb_set" in sql:
            # 付与: boilerplate 指紋に属し、まだ true でない chunk に印を付ける。
            for c in self._chunks:
                if _normalize_fp(c.content) in bp_fps and c.metadata.get("boilerplate") is not True:
                    c.metadata["boilerplate"] = True
                    changed += 1
        elif "- 'boilerplate'" in sql:
            # 除去: boilerplate キーがあるが、もはやテンプレ指紋でない chunk から外す。
            for c in self._chunks:
                if "boilerplate" in c.metadata and _normalize_fp(c.content) not in bp_fps:
                    del c.metadata["boilerplate"]
                    changed += 1
        else:  # pragma: no cover - 想定外 SQL
            raise AssertionError(f"unexpected SQL: {sql[:80]}")
        self.rowcount = changed


class _FakeConn:
    def __init__(
        self, chunks: list[_FakeChunk], suppressed_doc_ids: set[str] | None = None
    ) -> None:
        self.chunks = chunks
        self.suppressed_doc_ids = suppressed_doc_ids or set()

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.chunks, self.suppressed_doc_ids)


def _flagged(chunks: list[_FakeChunk]) -> list[_FakeChunk]:
    return [c for c in chunks if c.metadata.get("boilerplate") is True]


# -----------------------------------------------------------
# 3 資料に同一テキスト → flag される
# -----------------------------------------------------------
def test_same_text_in_three_docs_is_flagged() -> None:
    chunks = [
        _FakeChunk("doc-a", "会社概要：ベクトル株式会社", {}),
        _FakeChunk("doc-b", "会社概要：ベクトル株式会社", {}),
        _FakeChunk("doc-c", "会社概要：ベクトル株式会社", {}),
        _FakeChunk("doc-a", "本提案の独自施策はこちらです", {}),  # 1 doc のみ＝非テンプレ
    ]
    conn = _FakeConn(chunks)
    affected = mark_boilerplate(conn, min_docs=3, min_chars=0)

    assert affected == 3
    flagged = _flagged(chunks)
    assert len(flagged) == 3
    assert {c.document_id for c in flagged} == {"doc-a", "doc-b", "doc-c"}
    # 独自施策（1 doc のみ）は印が付かない。
    assert all(c.metadata.get("boilerplate") is not True for c in chunks if "独自施策" in c.content)


def test_normalization_ignores_whitespace_and_case() -> None:
    """大文字小文字・前後空白・連続空白の差は同一テンプレとして畳まれる。"""
    chunks = [
        _FakeChunk("doc-a", "Contact: sales@vec.co.jp", {}),
        _FakeChunk("doc-b", "  contact:   SALES@vec.co.jp  ", {}),
        _FakeChunk("doc-c", "CONTACT: sales@vec.co.jp", {}),
    ]
    conn = _FakeConn(chunks)
    affected = mark_boilerplate(conn, min_docs=3, min_chars=0)
    assert affected == 3
    assert len(_flagged(chunks)) == 3


# -----------------------------------------------------------
# 2 資料だけなら付かない（閾値 min_docs=3）
# -----------------------------------------------------------
def test_same_text_in_two_docs_not_flagged() -> None:
    chunks = [
        _FakeChunk("doc-a", "免責事項：本資料は参考情報です", {}),
        _FakeChunk("doc-b", "免責事項：本資料は参考情報です", {}),
    ]
    conn = _FakeConn(chunks)
    affected = mark_boilerplate(conn, min_docs=3, min_chars=0)

    assert affected == 0
    assert _flagged(chunks) == []


def test_same_doc_repeated_does_not_count_as_multiple_docs() -> None:
    """同一 document 内に同テキストが N 回出ても COUNT(DISTINCT document_id)=1 で非テンプレ。"""
    chunks = [
        _FakeChunk("doc-a", "繰り返しテンプレ行", {}),
        _FakeChunk("doc-a", "繰り返しテンプレ行", {}),
        _FakeChunk("doc-a", "繰り返しテンプレ行", {}),
    ]
    conn = _FakeConn(chunks)
    affected = mark_boilerplate(conn, min_docs=3, min_chars=0)
    assert affected == 0
    assert _flagged(chunks) == []


# -----------------------------------------------------------
# 閾値割れで除去される＝冪等＆自己修正
# -----------------------------------------------------------
def test_idempotent_no_change_on_second_run() -> None:
    """同じコーパスで 2 回走らせても 2 回目は affected=0（無変更）＝冪等。"""
    chunks = [
        _FakeChunk("doc-a", "共通フッター", {}),
        _FakeChunk("doc-b", "共通フッター", {}),
        _FakeChunk("doc-c", "共通フッター", {}),
    ]
    conn = _FakeConn(chunks)
    assert mark_boilerplate(conn, min_docs=3, min_chars=0) == 3
    # 2 回目は既に全部 true なので付与も除去も発生しない。
    assert mark_boilerplate(conn, min_docs=3, min_chars=0) == 0
    assert len(_flagged(chunks)) == 3


def test_below_threshold_removes_existing_flag() -> None:
    """資料が減ってテンプレでなくなった箇所からは印が自動で外れる（自己修正）。"""
    # 既に boilerplate=true が付いているが、今は 2 doc にしか出ない（min_docs=3 を割る）。
    chunks = [
        _FakeChunk("doc-a", "旧テンプレ行", {"boilerplate": True, "page_num": 1}),
        _FakeChunk("doc-b", "旧テンプレ行", {"boilerplate": True}),
    ]
    conn = _FakeConn(chunks)
    affected = mark_boilerplate(conn, min_docs=3, min_chars=0)

    assert affected == 2  # 2 行から除去
    assert _flagged(chunks) == []
    # 他の metadata キーは保持される（boilerplate キーだけ落ちる）。
    assert chunks[0].metadata == {"page_num": 1}


def test_invalid_min_docs_is_noop() -> None:
    """min_docs<1 は fail-safe で何もしない（誤って全解除しない）。"""
    chunks = [
        _FakeChunk("doc-a", "守るべき印", {"boilerplate": True}),
    ]
    conn = _FakeConn(chunks)
    assert mark_boilerplate(conn, min_docs=0, min_chars=0) == 0
    # 印はそのまま残る。
    assert chunks[0].metadata.get("boilerplate") is True


# -----------------------------------------------------------
# M2: 短い全文一致 chunk（ページ番号等）はテンプレ判定の対象外
# -----------------------------------------------------------
def test_short_chunk_below_min_chars_not_flagged() -> None:
    """正規化長 < min_chars の短い chunk は、複数 doc に並んでもテンプレ化しない（M2）。

    ページ番号や連番のような短い全文一致を boilerplate にすると、その短文しか持たない
    chunk（＝唯一の回答源）を検索から消してしまう。最小文字数ガードでこれを防ぐ。
    """
    chunks = [
        _FakeChunk("doc-a", "12", {}),  # 2 文字・3 doc に出るが短すぎる
        _FakeChunk("doc-b", "12", {}),
        _FakeChunk("doc-c", "12", {}),
    ]
    conn = _FakeConn(chunks)
    # 既定相当の min_chars=40。短い chunk は集計母数から外れる → 1 件も flag されない。
    affected = mark_boilerplate(conn, min_docs=3, min_chars=40)
    assert affected == 0
    assert _flagged(chunks) == []


def test_long_chunk_at_or_above_min_chars_still_flagged() -> None:
    """min_chars 以上の長い全文一致は従来どおりテンプレ判定される（M2 が長文を巻き込まない）。"""
    long_line = "本資料の無断転載・複製を固く禁じます。お問い合わせは営業担当までご連絡ください。"
    assert len(long_line) >= 40
    chunks = [
        _FakeChunk("doc-a", long_line, {}),
        _FakeChunk("doc-b", long_line, {}),
        _FakeChunk("doc-c", long_line, {}),
    ]
    conn = _FakeConn(chunks)
    affected = mark_boilerplate(conn, min_docs=3, min_chars=40)
    assert affected == 3
    assert len(_flagged(chunks)) == 3


# -----------------------------------------------------------
# M3: suppressed（非正本）doc は DISTINCT document_id の母数から外す
# -----------------------------------------------------------
def test_suppressed_docs_not_counted_toward_distinct() -> None:
    """重複コピー（suppressed）で水増しされた DISTINCT document_id を数えない（M3）。

    同一テキストが 3 doc に出ても、うち 2 つが dedup で suppressed なら実質 1 doc 分。
    min_docs=3 を満たさず boilerplate にならない（本文を誤って消さない）。
    """
    text = "この一文は本来この案件にしか出てこない固有の提案メッセージで長さも十分にある内容です"
    assert len(text) >= 40
    chunks = [
        _FakeChunk("doc-canonical", text, {}),
        _FakeChunk("doc-dup1", text, {}),
        _FakeChunk("doc-dup2", text, {}),
    ]
    # doc-dup1 / doc-dup2 は docdedup で suppressed 確定済み（同一 run で dedup→boilerplate 順）。
    conn = _FakeConn(chunks, suppressed_doc_ids={"doc-dup1", "doc-dup2"})
    affected = mark_boilerplate(conn, min_docs=3, min_chars=40)
    assert affected == 0
    assert _flagged(chunks) == []


def test_non_suppressed_distinct_docs_still_flagged() -> None:
    """suppressed を母数から外しても、非 suppressed が min_docs 件あれば従来どおり flag される。

    M3 は「指紋が boilerplate かどうかの判定（CTE の COUNT）」から suppressed を外すだけで、
    判定後の UPDATE 対象自体は絞らない。よって閾値を満たす指紋を持つ chunk は（たまたま
    suppressed doc にあっても）印が付く＝検索からは元々隠れているので害は無い。
    """
    text = "会社概要・所在地・代表者名などの定型情報がそのまま複数資料に転記されている定型文です"
    assert len(text) >= 40
    chunks = [
        _FakeChunk("doc-a", text, {}),
        _FakeChunk("doc-b", text, {}),
        _FakeChunk("doc-c", text, {}),
        _FakeChunk("doc-d", text, {}),  # suppressed: 母数には数えないが UPDATE 対象からは外さない
    ]
    conn = _FakeConn(chunks, suppressed_doc_ids={"doc-d"})
    affected = mark_boilerplate(conn, min_docs=3, min_chars=40)
    # 非 suppressed の doc-a/b/c だけで COUNT(DISTINCT)=3 を満たす → 指紋は boilerplate。
    # その指紋を持つ全 chunk（doc-d 含む）に印が付く。
    flagged_ids = {c.document_id for c in _flagged(chunks)}
    assert flagged_ids == {"doc-a", "doc-b", "doc-c", "doc-d"}
    assert affected == 4
