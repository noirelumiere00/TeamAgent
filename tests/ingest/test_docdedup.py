"""ingest/docdedup.py のテスト。

実 Postgres は使わず、``mark_duplicate_documents`` が発行する 3 種類の SQL
（fetch SELECT / suppressed 付与 UPDATE / 印除去 UPDATE）の**意味論**を Python で
忠実にエミュレートする fake conn で検証する。

検証する契約:
- 同一本文の 2 doc → 本文量が多い方が正本、薄い方に suppressed + duplicate_of=正本。
- 抽出器違い（PDF / PPTX）で文字が微妙に違っても単語がほぼ一致すれば畳む。
- 閾値未満（別資料）は印がつかない。
- 冪等（2 回目 affected=0）。
- 重複解消（片方を別物に書き換え）で印が自動で外れる＝自己修正。
"""

from __future__ import annotations

from typing import Any

from teamagent.ingest.docdedup import mark_duplicate_documents


class _FakeDoc:
    """1 document の状態。``chunks`` は (chunk_idx, content) のリスト。"""

    def __init__(
        self,
        document_id: str,
        chunks: list[tuple[int, str]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.document_id = document_id
        self.chunks = chunks
        self.metadata: dict[str, Any] = dict(metadata or {})


class _FakeCursor:
    """mark_duplicate_documents が発行する 3 種類の SQL の意味論をエミュレートする。

    - "FROM documents" を含む SELECT → 各 doc の (id, full_text, content_len, 既存印) を返す。
    - "jsonb_set" を含む UPDATE → 対象 doc に suppressed=true + duplicate_of=正本id を付与。
    - "- 'suppressed'" を含む UPDATE → 対象 doc から両キーを除去。
    rowcount は「実際に変化した行数」（本番 UPDATE は WHERE id=... で 1 行ヒット）。
    """

    def __init__(self, docs: list[_FakeDoc]) -> None:
        self._docs = docs
        self.rowcount = 0
        self._last_rows: list[dict[str, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def _by_id(self, doc_id: str) -> _FakeDoc | None:
        for d in self._docs:
            if d.document_id == doc_id:
                return d
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        params = params or ()
        if "FROM documents" in sql and "SELECT" in sql:
            # fetch: 本番 string_agg(content ORDER BY chunk_idx) + SUM(length) を再現。
            # H2: SQL 末尾に "LIMIT %s" があれば params の末尾を上限件数として id 昇順で切る。
            ordered_docs = sorted(self._docs, key=lambda x: x.document_id)
            if "LIMIT" in sql and params:
                ordered_docs = ordered_docs[: int(params[-1])]
            rows: list[dict[str, Any]] = []
            for d in ordered_docs:
                ordered = sorted(d.chunks, key=lambda c: c[0])
                full_text = " ".join(content for _, content in ordered)
                content_len = sum(len(content) for _, content in ordered)
                rows.append(
                    {
                        "document_id": d.document_id,
                        "full_text": full_text,
                        "content_len": content_len,
                        "suppressed": d.metadata.get("suppressed"),
                        "duplicate_of": d.metadata.get("duplicate_of"),
                    }
                )
            self._last_rows = rows
            self.rowcount = len(rows)
            return
        if "jsonb_set" in sql:
            # 付与: params=(正本id, 対象id)。対象 doc に印を書く。
            canonical_id, target_id = params
            d = self._by_id(str(target_id))
            assert d is not None, f"target not found: {target_id}"
            d.metadata["suppressed"] = "true"
            d.metadata["duplicate_of"] = str(canonical_id)
            self.rowcount = 1  # UPDATE は id=... で常に 1 行ヒット
            return
        if "- 'suppressed'" in sql:
            # 除去: params=(対象id,)。両キーを落とす。
            (target_id,) = params
            d = self._by_id(str(target_id))
            assert d is not None, f"target not found: {target_id}"
            d.metadata.pop("suppressed", None)
            d.metadata.pop("duplicate_of", None)
            self.rowcount = 1
            return
        raise AssertionError(f"unexpected SQL: {sql[:80]}")

    def fetchall(self) -> list[dict[str, Any]]:
        return self._last_rows


class _FakeConn:
    def __init__(self, docs: list[_FakeDoc]) -> None:
        self.docs = docs

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.docs)


def _doc(doc_id: str, text: str, metadata: dict[str, Any] | None = None) -> _FakeDoc:
    """1 chunk の doc を作るヘルパ。"""
    return _FakeDoc(doc_id, [(0, text)], metadata)


def _suppressed(docs: list[_FakeDoc]) -> list[_FakeDoc]:
    return [d for d in docs if d.metadata.get("suppressed") == "true"]


# -----------------------------------------------------------
# 同一本文の 2 doc → 濃い方が正本・薄い方が suppressed
# -----------------------------------------------------------
def test_identical_docs_suppress_thinner_keep_richer() -> None:
    long_text = "界隈マーケティングの提案です " * 20
    short_text = "界隈マーケティングの提案です " * 18  # 同じ語彙だが少し短い＝本文量小
    docs = [
        _FakeDoc("doc-rich", [(0, long_text)]),
        _FakeDoc("doc-thin", [(0, short_text)]),
    ]
    conn = _FakeConn(docs)
    affected = mark_duplicate_documents(conn, jaccard_threshold=0.7)

    assert affected == 1
    suppressed = _suppressed(docs)
    assert len(suppressed) == 1
    assert suppressed[0].document_id == "doc-thin"
    assert suppressed[0].metadata["duplicate_of"] == "doc-rich"
    # 正本（濃い方）には印が付かない。
    rich = next(d for d in docs if d.document_id == "doc-rich")
    assert "suppressed" not in rich.metadata
    assert "duplicate_of" not in rich.metadata


def test_pdf_vs_pptx_word_overlap_collapses() -> None:
    """抽出器違いで空白・改行・記号差があっても、単語がほぼ一致すれば畳む。"""
    # PDF 抽出: 連続空白・改行が混じる。
    pdf_text = "御社\n\n御中  ご提案資料   2026年   春   キャンペーン   施策   概要"
    # PPTX 抽出: 同じ単語列だが空白の入り方が違う＋末尾に 1 単語だけ多い。
    pptx_text = "御社 御中 ご提案資料 2026年 春 キャンペーン 施策 概要 詳細"
    docs = [
        _FakeDoc("doc-a", [(0, pdf_text)]),
        _FakeDoc("doc-b", [(0, pptx_text)]),
    ]
    conn = _FakeConn(docs)
    affected = mark_duplicate_documents(conn, jaccard_threshold=0.5)
    assert affected == 1
    assert len(_suppressed(docs)) == 1


def test_japanese_no_space_pdf_vs_pptx_collapses() -> None:
    """日本語の連続テキスト（語間空白なし）でも near-dup を畳む（文字 n-gram 化の回帰防止）。

    実営業デッキの抽出テキストには語間空白が無い。単語分割ベースだと 1 doc=1 巨大トークンに
    なり Jaccard が割れて畳めない。文字 n-gram なら言語非依存で畳める。閾値は既定の 0.7。
    """
    base = (
        "ピーアールとショート動画を組み合わせた新しい販促サービスのご提案資料です"
        "本企画では認知拡大から購買転換までを一気通貫で設計しインフルエンサー起用と"
        "運用型広告を掛け合わせて費用対効果の最大化を目指します対象は若年層を中心とした"
        "ソーシャルメディア利用者でブランドの世界観を保ちながら自然な形で商品接触を増やします"
    )
    # PPTX 版（濃い＝正本）と PDF 版（薄い）。本文はほぼ同一・末尾の一句だけ違う・空白なし。
    pptx_text = base + "詳細スケジュールと費用感は別紙のとおりご確認をお願いいたします"
    pdf_text = base + "費用は別紙参照"
    docs = [
        _FakeDoc("deck-pptx", [(0, pptx_text)]),
        _FakeDoc("deck-pdf", [(0, pdf_text)]),
    ]
    conn = _FakeConn(docs)
    affected = mark_duplicate_documents(conn, jaccard_threshold=0.7)
    assert affected == 1
    suppressed = _suppressed(docs)
    assert len(suppressed) == 1
    # 濃い方(pptx)が正本、薄い方(pdf)が抑制される。
    assert suppressed[0].document_id == "deck-pdf"
    assert suppressed[0].metadata["duplicate_of"] == "deck-pptx"


# -----------------------------------------------------------
# 閾値未満（別資料）は印がつかない
# -----------------------------------------------------------
def test_distinct_docs_not_marked() -> None:
    docs = [
        _FakeDoc("doc-a", [(0, "飲食店向けの集客プランをご提案します")]),
        _FakeDoc("doc-b", [(0, "アパレルECのリピート施策を設計します")]),
    ]
    conn = _FakeConn(docs)
    affected = mark_duplicate_documents(conn, jaccard_threshold=0.7)
    assert affected == 0
    assert _suppressed(docs) == []


def test_content_length_breaks_ties_for_canonical() -> None:
    """本文量が同点なら document_id 昇順で正本（小さい id が残る）。"""
    same = "全く同じ 文章 です 提案 資料 共通 テンプレ 本文 同一"
    docs = [
        _FakeDoc("doc-zzz", [(0, same)]),
        _FakeDoc("doc-aaa", [(0, same)]),
    ]
    conn = _FakeConn(docs)
    affected = mark_duplicate_documents(conn, jaccard_threshold=0.7)
    assert affected == 1
    suppressed = _suppressed(docs)
    assert len(suppressed) == 1
    # 同点 → document_id 昇順 doc-aaa が正本、doc-zzz が非正本。
    assert suppressed[0].document_id == "doc-zzz"
    assert suppressed[0].metadata["duplicate_of"] == "doc-aaa"


# -----------------------------------------------------------
# 冪等（2 回目 affected=0）
# -----------------------------------------------------------
def test_idempotent_second_run_no_change() -> None:
    text = "繰り返し 取り込んでも 結論は 同じ 提案 資料 本文 内容 一致"
    docs = [
        _FakeDoc("doc-a", [(0, text + " 追記 詳細 補足")]),  # 本文量多 → 正本
        _FakeDoc("doc-b", [(0, text)]),
    ]
    conn = _FakeConn(docs)
    assert mark_duplicate_documents(conn, jaccard_threshold=0.7) == 1
    # 2 回目は desired と既存印が一致 → 何も書かない。
    assert mark_duplicate_documents(conn, jaccard_threshold=0.7) == 0
    suppressed = _suppressed(docs)
    assert len(suppressed) == 1
    assert suppressed[0].document_id == "doc-b"


# -----------------------------------------------------------
# 重複解消で印が自動で外れる＝自己修正
# -----------------------------------------------------------
def test_resolved_duplicate_clears_flag() -> None:
    """前回 suppressed だった doc が、今回は別物になったら印が外れる。"""
    docs = [
        _FakeDoc("doc-a", [(0, "共通 テンプレ 本文 同一 内容 提案 資料")]),
        # 既に前回 doc-a の重複として印が付いている状態を再現。
        _FakeDoc(
            "doc-b",
            [(0, "もはや 全然 違う 独自 施策 オリジナル 内容 に 差し替え 済み")],
            metadata={"suppressed": "true", "duplicate_of": "doc-a"},
        ),
    ]
    conn = _FakeConn(docs)
    affected = mark_duplicate_documents(conn, jaccard_threshold=0.7)

    assert affected == 1  # doc-b から印を除去
    assert _suppressed(docs) == []
    doc_b = next(d for d in docs if d.document_id == "doc-b")
    assert "duplicate_of" not in doc_b.metadata


def test_canonical_promoted_repoints_old_flag() -> None:
    """前回 doc-x を指していた非正本が、今回は別の正本（doc-long）に貼り替わる。

    doc-short と doc-long はほぼ同一本文（doc-long の方が長い＝正本）。doc-short には
    前回の stale な印（duplicate_of=doc-x）が残っているが、今回 doc-long を指すよう
    貼り替えられる（冪等の差分更新が「指す正本が変わった」も検出する）。
    """
    # 本文がほぼ一致するように、doc-long は doc-short の語彙をそのまま含み少しだけ長くする。
    short = "テンプレ 本文 同一 提案 資料 共通 部分 が とても 長い 内容 で 一致 する"
    long = short + " 末尾 に 少し だけ 追記"
    docs = [
        # 前回は doc-x が正本で doc-short が非正本だったが…
        _FakeDoc(
            "doc-short", [(0, short)], metadata={"suppressed": "true", "duplicate_of": "doc-x"}
        ),
        _FakeDoc("doc-long", [(0, long)]),
    ]
    conn = _FakeConn(docs)
    affected = mark_duplicate_documents(conn, jaccard_threshold=0.7)
    # doc-long が本文量最大 → 正本。doc-short は duplicate_of=doc-x → doc-long に貼り替え。
    suppressed = _suppressed(docs)
    assert len(suppressed) == 1
    assert suppressed[0].document_id == "doc-short"
    assert suppressed[0].metadata["duplicate_of"] == "doc-long"
    assert affected == 1


# -----------------------------------------------------------
# しきい値・件数の境界
# -----------------------------------------------------------
def test_invalid_threshold_is_noop() -> None:
    """jaccard_threshold がレンジ外（<=0 / >1）なら何もしない。"""
    docs = [_doc("doc-a", "守るべき 印", {"suppressed": "true", "duplicate_of": "doc-b"})]
    conn = _FakeConn(docs)
    assert mark_duplicate_documents(conn, jaccard_threshold=0.0) == 0
    assert mark_duplicate_documents(conn, jaccard_threshold=1.5) == 0
    # 印はそのまま残る（誤って全解除しない）。
    assert docs[0].metadata.get("suppressed") == "true"


def test_single_document_clears_stale_flag() -> None:
    """doc が 1 件以下でも、残った stale な印は自己修正で外す。"""
    docs = [_doc("doc-a", "ひとりだけ", {"suppressed": "true", "duplicate_of": "doc-gone"})]
    conn = _FakeConn(docs)
    affected = mark_duplicate_documents(conn, jaccard_threshold=0.7)
    assert affected == 1
    assert docs[0].metadata.get("suppressed") is None


def test_three_way_cluster_keeps_one_canonical() -> None:
    """3 doc が相互に重複 → 本文量最大の 1 件だけ残り、他 2 件が同じ正本を指す。"""
    base = "三 つ の 資料 が ほぼ 同一 の 本文 を 共有 する 提案 デッキ"
    docs = [
        _FakeDoc("doc-1", [(0, base)]),
        _FakeDoc("doc-2", [(0, base + " 追加")]),
        _FakeDoc("doc-3", [(0, base + " 追加 さらに 最も 長い 本文 量 で 正本 候補")]),
    ]
    conn = _FakeConn(docs)
    affected = mark_duplicate_documents(conn, jaccard_threshold=0.5)
    suppressed = _suppressed(docs)
    assert {d.document_id for d in suppressed} == {"doc-1", "doc-2"}
    assert all(d.metadata["duplicate_of"] == "doc-3" for d in suppressed)
    assert affected == 2


# -----------------------------------------------------------
# H2: doc 数上限・per-doc 文字数 truncate で OOM/timeout 回避（決定性維持）
# -----------------------------------------------------------
def test_max_docs_limits_documents_scanned() -> None:
    """max_docs を超える分は id 昇順で先頭だけ対象になり、上限外の重複は畳まれない（H2）。

    doc-a/doc-b が重複ペアだが、max_docs=1 なら id 昇順先頭の doc-a だけ読まれ、doc-b は
    そもそも対象外。n<2 扱いで重複判定は走らず、どちらにも印が付かない（決定的）。
    """
    same = "完全 に 同一 の 本文 を 持つ 重複 資料 ペア の テンプレ 提案"
    docs = [
        _FakeDoc("doc-a", [(0, same)]),
        _FakeDoc("doc-b", [(0, same)]),
    ]
    conn = _FakeConn(docs)
    affected = mark_duplicate_documents(conn, jaccard_threshold=0.7, max_docs=1)
    assert affected == 0
    assert _suppressed(docs) == []


def test_max_docs_unlimited_when_non_positive() -> None:
    """max_docs<=0 は無制限（後方互換）＝全 doc を読んで従来どおり畳む。"""
    same = "完全 に 同一 の 本文 を 持つ 重複 資料 ペア の テンプレ 提案"
    docs = [
        _FakeDoc("doc-a", [(0, same + " 追記 で 少し 長い 正本")]),
        _FakeDoc("doc-b", [(0, same)]),
    ]
    conn = _FakeConn(docs)
    affected = mark_duplicate_documents(conn, jaccard_threshold=0.7, max_docs=0)
    assert affected == 1
    assert {d.document_id for d in _suppressed(docs)} == {"doc-b"}


def test_char_shingles_truncates_and_is_deterministic() -> None:
    """巨大 doc は max_chars で先頭から truncate され、shingle 集合が決定的に縮む（H2）。"""
    from teamagent.ingest.docdedup import _SHINGLE_N, _char_shingles

    big = "あ" * 10 + "い" * 10  # 20 文字（多様な n-gram を持たせる）
    full = _char_shingles(big, max_chars=0)  # 無制限
    truncated = _char_shingles(big, max_chars=8)  # 先頭 8 文字だけ
    # truncate 後は先頭 8 文字相当の shingle のみ＝full の部分集合で、件数も減る。
    assert truncated <= full
    assert len(truncated) < len(full)
    # 決定性: 同じ入力・同じ上限なら毎回同一集合。
    assert _char_shingles(big, max_chars=8) == truncated
    # 先頭 8 文字（"ああああああああ"）の n-gram と一致する。
    assert truncated == _char_shingles("あ" * 8, n=_SHINGLE_N, max_chars=0)


def test_huge_doc_truncation_does_not_change_near_dup_decision() -> None:
    """巨大だが先頭が一致する 2 doc は、truncate しても near-dup として畳める（決定性＋安全）。"""
    head = "共通 する 長い 前置き の 本文 が 延々 と 続く 提案 資料 の 冒頭 部分 "
    a = head * 50 + "末尾 だけ A"
    b = head * 50 + "末尾 だけ B"
    docs = [
        _FakeDoc("doc-long-a", [(0, a + " さらに 長い 正本 側")]),
        _FakeDoc("doc-long-b", [(0, b)]),
    ]
    conn = _FakeConn(docs)
    # 先頭の共通部分だけで Jaccard は十分高く、max_chars で切っても結論は不変。
    affected = mark_duplicate_documents(conn, jaccard_threshold=0.7, max_chars=200)
    assert affected == 1
    assert {d.document_id for d in _suppressed(docs)} == {"doc-long-b"}
