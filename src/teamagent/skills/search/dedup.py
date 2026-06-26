"""検索結果の「資料の被り」対策（純関数モジュール）。

営業資料は表紙・会社紹介・料金フォーマット等のテンプレページを使い回すため、
検索すると (1) 同一資料がテンプレ部分で結果を埋め尽くす、(2) 複数資料から
ほぼ同一のテンプレチャンクが重複ヒットする、という問題が起きる。これを
**retrieval 側**で潰すための 2 つの純関数を提供する。

- ``collapse_near_duplicates``: content が near-identical なチャンクを 1 本に畳む
  （shingle n-gram Jaccard 類似度 >= しきい値）。score 最大の hit を残す。
- ``cap_per_document``: 同一 document_id（無ければ source_uri）のチャンクを
  上位 N 件までに制限する。

いずれも純関数（os.environ を読まない・副作用なし）で、入力順に依存しない
決定的な結果を返す。env 読み取りと有効/無効の判定は呼び出し側（skill）で行う。
"""

from __future__ import annotations

import re

from teamagent.adapters.pgvector_client import SearchHit

# near-dup 判定用の正規化：意味を持つ文字（英数・かな・漢字・全角英数）だけを残し、
# 空白・約物・装飾記号は全て除去する。テンプレページは句読点や空白・装飾記号だけが
# 微妙に違うことが多いため、それらをノイズとして無視して content の同一性を見る。
# 日本語なので小文字化はしない（英字の大文字小文字差は実害が小さく保留）。
_KEEP_RE = re.compile(
    "[^"
    r"0-9A-Za-z"  # 半角英数
    "぀-ヿ"  # ひらがな・カタカナ
    "㐀-䶿"  # CJK 拡張 A
    "一-鿿"  # CJK 統合漢字（基本）
    "Ａ-Ｚａ-ｚ"  # 全角英字
    "０-９"  # 全角数字
    "ｦ-ﾟ"  # 半角カナ
    "]+",
    re.UNICODE,
)


def _normalize(text: str) -> str:
    """空白・約物・装飾記号を除去し、意味のある文字列だけにする。決定的。

    near-dup 判定の前処理。テンプレの句読点差・空白差・装飾記号差を吸収する。
    """
    if not text:
        return ""
    return _KEEP_RE.sub("", text)


def _shingles(text: str, n: int = 3) -> frozenset[str]:
    """正規化済みテキストから文字 n-gram の集合を作る。

    日本語は単語境界が曖昧なので文字 n-gram を採る。テキストが n 文字未満なら
    全体を 1 要素として扱う（短いテンプレ見出しでも比較可能にする）。
    """
    if not text:
        return frozenset()
    if len(text) <= n:
        return frozenset([text])
    return frozenset(text[i : i + n] for i in range(len(text) - n + 1))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """2 つの shingle 集合の Jaccard 類似度。両方空なら 1.0、片方だけ空なら 0.0。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def collapse_near_duplicates(
    hits: list[SearchHit], *, jaccard_threshold: float = 0.9, shingle_n: int = 3
) -> list[SearchHit]:
    """content が near-identical なチャンクを 1 本に畳む。

    正規化（空白圧縮・記号トリム）した content の文字 n-gram Jaccard 類似度が
    ``jaccard_threshold`` 以上の hit 同士を重複とみなし、**score が最大の hit**
    を代表として残す（同点なら chunk_id が小さい方＝安定）。

    決定性：まず (score desc, chunk_id asc) で安定ソートしてから貪欲に代表を選ぶ
    ため、入力の並び順に依存しない。元の入力リストは変更しない。

    戻り値の hit 自体（score / content / metadata）は代表 hit のものをそのまま
    使う（畳んだ分のスコア合算等はしない＝relevance スケールを壊さない）。
    """
    if len(hits) <= 1:
        return list(hits)

    # 決定的な処理順：score 降順 → chunk_id 昇順。これで「残す代表」が一意に決まる。
    ordered = sorted(hits, key=lambda h: (-h.score, h.chunk_id))
    precomputed = [(_shingles(_normalize(h.content), shingle_n), h) for h in ordered]

    kept: list[SearchHit] = []
    kept_shingles: list[frozenset[str]] = []
    for sh, hit in precomputed:
        is_dup = False
        for ksh in kept_shingles:
            if _jaccard(sh, ksh) >= jaccard_threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(hit)
            kept_shingles.append(sh)
    return kept


def _doc_key(hit: SearchHit) -> str | None:
    """資料単位の識別子。document_id 優先、無ければ source_uri。両方無ければ None。

    None（識別子を持たない hit）は cap の対象外＝常に残す（取りこぼし防止）。
    """
    meta = hit.metadata or {}
    doc_id = meta.get("document_id")
    if doc_id is not None and str(doc_id) != "":
        return f"doc:{doc_id}"
    src = meta.get("source_uri")
    if src is not None and str(src) != "":
        return f"src:{src}"
    return None


def cap_per_document(hits: list[SearchHit], *, max_per_doc: int = 2) -> list[SearchHit]:
    """同一 document_id（無ければ source_uri）のチャンクを上位 N 件までに制限する。

    score 降順（同点は chunk_id 昇順）で上から数え、同一資料が N 件を超えたら
    超過分を落とす。1 資料が結果を独占しないため。識別子を持たない hit は
    常に残す。``max_per_doc <= 0`` なら何もしない（cap 無効）。元リストは変更しない。

    入力の相対順序は保ったまま落とす（落とさない hit の並び順は不変）。
    """
    if max_per_doc <= 0 or len(hits) <= 1:
        return list(hits)

    # 「どれを落とすか」は score 順で決めるが、戻り値は入力順を保つ。
    # → score 降順で counts を回し、超過した hit の id を drop 集合に入れる。
    ranked = sorted(enumerate(hits), key=lambda ih: (-ih[1].score, ih[1].chunk_id, ih[0]))
    counts: dict[str, int] = {}
    drop_indices: set[int] = set()
    for idx, hit in ranked:
        key = _doc_key(hit)
        if key is None:
            continue
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > max_per_doc:
            drop_indices.add(idx)
    return [h for i, h in enumerate(hits) if i not in drop_indices]
