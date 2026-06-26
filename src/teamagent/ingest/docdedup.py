"""資料まるごと重複排除: ほぼ同一の document ペアの片方を「非正本」として隠す。

営業資料は「PDF 版」と「PPTX 版」、「v1」と「v1（再送）」のように、**同じ内容の
別ファイル**が複数取り込まれることが多い。検索でもグラフでも同じ資料が二重に出ると
ノイズになる。本モジュールはコーパス全体を走査し、文書全体の正規化テキストが
**ほぼ同一**な document をクラスタ化し、各クラスタで本文量が最大の 1 件だけを正本と
して残し、他を「非正本」として ``documents.metadata`` に印を付ける。

設計（全 agent 共通契約）:
- 重複の印 = ``documents.metadata`` (JSONB) の **非正本（隠す方）** に
  ``suppressed``=true と ``duplicate_of``=正本の document_id（uuid 文字列）。
  ``metadata`` は cls_* 等が使う既存 JSONB 列なので **DB migration 不要**。
- 「基本同一」判定 = 文書全体の正規化テキスト（lower + 連続空白を 1 個に圧縮）の
  **文字 n-gram shingle 集合**の Jaccard >= ``jaccard_threshold``。日本語は語間空白が
  無く単語分割できない（PDF/PPTX 抽出ともに 1 巨大トークン化）ため、単語ではなく**文字**
  n-gram を使う（言語非依存・CJK near-dup の定石）。PDF と PPTX で抽出器が違って空白・改行・
  軽い順序差があっても局所の文字列はほぼ一致するので、md5 完全一致ではなく shingle Jaccard を
  使う（表記揺れ・空白差・軽い順序差に強い）。文書数は数百なので
  O(n^2) ペア比較で十分だが、MinHash 署名（64 perm）で Jaccard を近似して軽量化する。
- 正本（残す方）= 本文量が最大の doc（全 chunk content 長の合計が最大）。同点は
  document_id 昇順タイブレーク（決定的）。「どちらを残すか」はユーザー的にどちらでも
  よく、濃い方を既定にする。
- 冪等＆自己修正: 毎回まず既存の ``suppressed`` / ``duplicate_of`` 印を全 doc から
  クリアしてから再計算して付け直す。再取込のたびに再評価され、重複でなくなった doc
  からは印が自動で外れる（boilerplate と同じ作法）。

本関数は env を読まない純 I/O（1 コネクションを受け取る）。env ゲート
（``DOC_DEDUP_DETECT`` / ``DOC_DEDUP_JACCARD``）の判定は呼び出し側（pipeline.py）が行う。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# MinHash の置換（permutation）数。多いほど Jaccard 推定が正確だが重い。
# 64 perm の標準誤差は約 1/sqrt(64)=0.125 で、しきい値 0.7 前後の判定には十分。
_NUM_PERM = 64

# shingle の窓幅。**文字** n-gram（日本語対応のため単語ではなく文字単位）。
# 日本語の抽出テキストは語間空白が無く「単語分割」が成立しない（1 doc=1 巨大トークン化して
# Jaccard が割れる）ため、文字 n-gram で shingle する。英語等の空白あり言語でも機能する。
_SHINGLE_N = 5

# H2: OOM/timeout 回避の既定上限。呼び出し側（pipeline.py）が env で上書きする。
# - _DEFAULT_MAX_DOCS: 1 回の dedup で読む document 数の上限（全 doc 全文を Python に
#   ロードするので、無制限だと巨大コーパスで OOM する）。決定性のため id 昇順で先頭から取る。
# - _DEFAULT_MAX_CHARS: 1 doc あたりの正規化テキスト最大文字数。これを超える分は truncate
#   してから shingle 化する（per-doc 無制限 shingle で O(文字数) のメモリが爆発するのを防ぐ）。
_DEFAULT_MAX_DOCS = 5000
_DEFAULT_MAX_CHARS = 500_000

# MinHash のハッシュ空間（2^61-1 のメルセンヌ素数より十分大きい 2^64 範囲）。
_MAX_HASH = (1 << 64) - 1
# 64 個の (a, b) 係数を固定 seed から決めて決定的にする（プロセス間で不変）。
# h_i(x) = (a_i * x + b_i) mod (2^64) を使い、各 perm の最小値で署名を作る。
_MERSENNE_PRIME = (1 << 61) - 1


def _normalize_text(text: str) -> str:
    """文書全体テキストを lower + 連続空白 1 個圧縮で正規化する（決定的）。"""
    return re.sub(r"\s+", " ", text.strip().lower())


def _char_shingles(
    normalized: str, n: int = _SHINGLE_N, *, max_chars: int = _DEFAULT_MAX_CHARS
) -> frozenset[str]:
    """正規化済みテキストから**文字** n-gram の集合を作る（決定的）。

    日本語は語間空白が無いので単語分割できない（PDF/PPTX 抽出ともに 1 巨大トークン化）。
    文字 n-gram なら言語非依存で機能し、PDF と PPTX で抽出器が違って空白・改行・軽い順序差が
    あっても、局所の文字列はほぼ一致するため shingle 集合の Jaccard は高くなる（CJK near-dup
    の定石）。文字数が n 未満なら全体を 1 要素として扱う。空なら空集合。

    H2（per-doc truncate）: 1 doc あたり ``max_chars`` 文字を超える分は先頭から truncate
    してから shingle 化する。巨大 doc の per-doc 無制限 shingle で O(文字数) のメモリ・時間が
    爆発するのを防ぐ。先頭固定で切るので決定性は保たれる（``max_chars<=0`` なら無制限＝従来）。
    """
    s = normalized
    if not s:
        return frozenset()
    if max_chars > 0 and len(s) > max_chars:
        s = s[:max_chars]
    if len(s) <= n:
        return frozenset([s])
    return frozenset(s[i : i + n] for i in range(len(s) - n + 1))


def _hash_shingle(shingle: str) -> int:
    """shingle を 64bit 整数へ決定的にハッシュする（md5 の先頭 8byte）。"""
    digest = hashlib.md5(shingle.encode("utf-8"), usedforsecurity=False).digest()
    return int.from_bytes(digest[:8], "big")


def _perm_coeffs() -> list[tuple[int, int]]:
    """MinHash 用の (a, b) 係数を固定 seed から決定的に作る。

    各 perm i について md5("minhash-coeff-{i}") から a_i, b_i を引く。プロセスや
    実行をまたいで不変なので、署名（ひいてはクラスタリング結果）は完全に決定的。
    """
    coeffs: list[tuple[int, int]] = []
    for i in range(_NUM_PERM):
        seed_a = hashlib.md5(f"minhash-a-{i}".encode(), usedforsecurity=False).digest()
        seed_b = hashlib.md5(f"minhash-b-{i}".encode(), usedforsecurity=False).digest()
        a = int.from_bytes(seed_a[:8], "big") % _MERSENNE_PRIME or 1
        b = int.from_bytes(seed_b[:8], "big") % _MERSENNE_PRIME
        coeffs.append((a, b))
    return coeffs


_COEFFS = _perm_coeffs()


def _minhash_signature(shingles: frozenset[str]) -> tuple[int, ...]:
    """shingle 集合の MinHash 署名（長さ _NUM_PERM）を作る。

    空集合は全 perm で _MAX_HASH の署名（他のどの非空署名とも一致 0 に近い）。
    """
    if not shingles:
        return tuple([_MAX_HASH] * _NUM_PERM)
    hashed = [_hash_shingle(s) for s in shingles]
    sig: list[int] = []
    for a, b in _COEFFS:
        sig.append(min(((a * h + b) % _MERSENNE_PRIME) for h in hashed))
    return tuple(sig)


def _signature_similarity(sig_a: tuple[int, ...], sig_b: tuple[int, ...]) -> float:
    """2 つの MinHash 署名から Jaccard を推定する（一致した perm の割合）。"""
    if not sig_a or not sig_b:
        return 0.0
    matches = sum(1 for x, y in zip(sig_a, sig_b, strict=True) if x == y)
    return matches / len(sig_a)


class _UnionFind:
    """document index 用の決定的 union-find（クラスタ化）。"""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # 経路圧縮（決定性には影響しない）。
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            # 小さい root を親にして決定的にする。
            if rx < ry:
                self._parent[ry] = rx
            else:
                self._parent[rx] = ry


def _fetch_documents(conn: Any, *, max_docs: int = _DEFAULT_MAX_DOCS) -> list[dict[str, Any]]:
    """document の (id, 正規化全文, 本文量, 既存印) を集める（上限 ``max_docs``）。

    chunks を document_id でまとめ、各 doc の全 chunk content を結合した全文を作る。
    本文量 = 全 chunk content 長の合計（正本選定のタイブレーク前提）。
    既存の suppressed / duplicate_of は冪等比較のために読む。

    H2（doc 数上限）: 全 doc 全文を Python にロードするので、無制限だと巨大コーパスで
    OOM する。``max_docs > 0`` なら ``id`` 昇順で先頭 ``max_docs`` 件だけ取る（id 昇順
    なので決定的）。``max_docs <= 0`` なら無制限（従来挙動・後方互換）。
    """
    # chunk content を document ごとに結合（chunk_idx 順で決定的）、本文量を合算。
    # documents を LEFT JOIN して、chunk が 1 つも無い doc も拾う（本文量 0）。
    # 列/テーブルは固定リテラル・LIMIT 値は placeholder bind → bandit B608 非該当。
    base_sql = """
        SELECT
            d.id::text AS document_id,
            COALESCE(
                string_agg(c.content, ' ' ORDER BY c.chunk_idx ASC),
                ''
            ) AS full_text,
            COALESCE(SUM(length(c.content)), 0) AS content_len,
            (d.metadata->>'suppressed') AS suppressed,
            (d.metadata->>'duplicate_of') AS duplicate_of
        FROM documents d
        LEFT JOIN chunks c ON c.document_id = d.id
        GROUP BY d.id, d.metadata
        ORDER BY d.id ASC
    """  # nosec B608  # 固定 SQL・LIMIT のみ placeholder bind
    rows: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        if max_docs > 0:
            cur.execute(base_sql + " LIMIT %s", (max_docs,))
        else:
            cur.execute(base_sql)
        for r in cur.fetchall():
            rows.append(dict(r) if not isinstance(r, dict) else r)
    return rows


# 非正本に印を付ける（suppressed=true + duplicate_of=正本id）。
# jsonb_set を 2 段重ねで両キーを 1 UPDATE で書く。値は placeholder bind。
_SET_SUPPRESSED_SQL = """
    UPDATE documents
    SET metadata = jsonb_set(
        jsonb_set(
            COALESCE(metadata, '{}'::jsonb),
            '{suppressed}', 'true'::jsonb, true
        ),
        '{duplicate_of}', to_jsonb(%s::text), true
    )
    WHERE id = %s::uuid
"""  # nosec B608  # 固定 SQL・正本id/対象id は placeholder bind

# 印を外す（suppressed と duplicate_of キーを両方落とす）。
_CLEAR_SUPPRESSED_SQL = """
    UPDATE documents
    SET metadata = (metadata - 'suppressed') - 'duplicate_of'
    WHERE id = %s::uuid
"""  # nosec B608  # 固定 SQL・対象id は placeholder bind


def mark_duplicate_documents(
    conn: Any,
    *,
    jaccard_threshold: float,
    max_docs: int = _DEFAULT_MAX_DOCS,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> int:
    """コーパス全体の document に重複排除の印（``metadata.suppressed`` 等）を付け直す。

    各 document の正規化全文から単語 3-gram shingle → MinHash 署名を作り、署名間
    Jaccard >= ``jaccard_threshold`` の doc ペアを「基本同一」とみなして union-find で
    クラスタ化する。各クラスタで本文量（全 chunk content 長合計）最大の doc を正本と
    し（同点は document_id 昇順）、他の doc の ``metadata`` に ``suppressed``=true と
    ``duplicate_of``=正本id を付与する。

    冪等＆自己修正: まず全 doc の既存印（suppressed / duplicate_of）と「今回の結論」を
    比較し、(1) 今回も同じ正本を指す非正本は無変更（rowcount に数えない）、(2) 今回
    正本になった or 重複でなくなった doc からは印を除去、(3) 新たに非正本になった or
    指す正本が変わった doc には印を付け直す、という差分更新を行う。再取込のたびに
    再評価され、重複が解消された doc からは自動で印が外れる。

    Args:
        conn: psycopg コネクション（admin role で documents を UPDATE できること）。
            本関数はトランザクション境界を持たない＝呼び出し側の ``connection()``
            コンテキストマネージャが commit / rollback を担う。
        jaccard_threshold: 「基本同一」とみなす MinHash Jaccard 推定のしきい値。
        max_docs: 1 回の dedup で読む document 数の上限（H2・OOM 回避）。``>0`` なら
            ``id`` 昇順で先頭 ``max_docs`` 件だけ対象（決定的）。``<=0`` で無制限（従来）。
        max_chars: 1 doc あたりの shingle 化前 truncate 文字数（H2・巨大 doc の OOM 回避）。
            ``<=0`` で無制限（従来）。

    Returns:
        実際に変化した（印を付けた／外した／正本を貼り替えた）document 数（ログ用）。
    """
    # しきい値レンジ外は fail-safe（誤って全 doc を 1 クラスタに畳んだり、逆に全解除
    # したりしない）。0 以下なら全ペアが重複扱いになり危険、1 超なら一致し得ない。
    if not (0.0 < jaccard_threshold <= 1.0):
        logger.warning("docdedup_threshold_invalid", jaccard_threshold=jaccard_threshold)
        return 0

    docs = _fetch_documents(conn, max_docs=max_docs)
    n = len(docs)
    if n < 2:
        # 1 件以下なら重複はあり得ない。既存印があれば自己修正で外す（冪等）。
        affected = 0
        with conn.cursor() as cur:
            for d in docs:
                if d.get("suppressed") is not None or d.get("duplicate_of") is not None:
                    cur.execute(_CLEAR_SUPPRESSED_SQL, (d["document_id"],))
                    affected += 1 if (cur.rowcount or 0) > 0 else 0
        logger.info("docdedup_marked", jaccard_threshold=jaccard_threshold, affected=affected)
        return affected

    # 1) 各 doc の MinHash 署名と本文量を作る（docs は document_id 昇順で固定済み）。
    signatures: list[tuple[int, ...]] = []
    for d in docs:
        normalized = _normalize_text(d["full_text"] or "")
        signatures.append(_minhash_signature(_char_shingles(normalized, max_chars=max_chars)))

    # 2) 全ペアで署名 Jaccard 推定 >= しきい値なら union（O(n^2)・n は数百で十分）。
    #    本文が空（署名が全 _MAX_HASH）の doc 同士は推定 1.0 になり得るが、両者とも
    #    本文ゼロ＝中身が無いので「重複」と束ねても害は無い（正本判定で 1 つ残る）。
    uf = _UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            if _signature_similarity(signatures[i], signatures[j]) >= jaccard_threshold:
                uf.union(i, j)

    # 3) クラスタごとに正本（本文量最大・同点は document_id 昇順）を選ぶ。
    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(uf.find(i), []).append(i)

    # desired[document_id] = 指すべき正本 document_id（None なら印を付けない＝正本/単独）。
    desired: dict[str, str | None] = {}
    for members in clusters.values():
        # docs は document_id 昇順なので、index 昇順 ≒ document_id 昇順。
        # 本文量降順 → document_id 昇順（タイブレーク）で先頭が正本。
        canonical_idx = min(
            members, key=lambda idx: (-int(docs[idx]["content_len"]), docs[idx]["document_id"])
        )
        canonical_id = docs[canonical_idx]["document_id"]
        for idx in members:
            doc_id = docs[idx]["document_id"]
            if idx == canonical_idx or len(members) == 1:
                desired[doc_id] = None
            else:
                desired[doc_id] = canonical_id

    # 4) 差分更新（冪等）。現状印 vs desired を比べ、変化した doc だけ UPDATE する。
    affected = 0
    with conn.cursor() as cur:
        for d in docs:
            doc_id = d["document_id"]
            cur_suppressed = (d.get("suppressed") or "").lower() == "true"
            cur_dup_of = d.get("duplicate_of")
            want = desired.get(doc_id)
            if want is None:
                # 正本 or 単独 → 印が付いていれば外す。
                if cur_suppressed or cur_dup_of is not None:
                    cur.execute(_CLEAR_SUPPRESSED_SQL, (doc_id,))
                    affected += 1 if (cur.rowcount or 0) > 0 else 0
            else:
                # 非正本 → suppressed=true かつ duplicate_of=want でなければ書き直す。
                if not (cur_suppressed and cur_dup_of == want):
                    cur.execute(_SET_SUPPRESSED_SQL, (want, doc_id))
                    affected += 1 if (cur.rowcount or 0) > 0 else 0

    logger.info(
        "docdedup_marked",
        jaccard_threshold=jaccard_threshold,
        documents=n,
        clusters=len(clusters),
        max_docs=max_docs,
        max_chars=max_chars,
        affected=affected,
    )
    return affected
