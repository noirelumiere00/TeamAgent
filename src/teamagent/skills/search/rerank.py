"""予算近接 re-rank（純関数・副作用なし・ネット不要・env を読まない）。

検索 Skill の取得後（Cohere rerank / min_relevance 適用の後）に1段だけ挟む並べ替え。
``ORDER BY`` は不変（バンド距離を SQL 第1キーにすると LIMIT がベクトル近傍を先に切り
良質ヒットが落ちるため）。絞り込みはせず、母数を痩せさせない。

ソートキー（安定ソート・3段）:
1. バンド距離 昇順（同帯=0 / 隣=1 / 2つ隣=2 / 不明・未付与・NULL=末尾）
2. is_low_confidence 0/1 昇順（exclusion_rescue 救出 hit を同帯内で末尾へ）
3. -score 降順（同予算感の中で関連度が高い順）

env-gate（``SEARCH_BUDGET_SORT``）と呼び出し判定は呼び側（skill.py）の責務。
本モジュールは os.environ を一切読まない（テスト容易・恒等性が明示的）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from teamagent.ingest.classify import _BUDGETS  # 単一の真実源

if TYPE_CHECKING:
    from teamagent.adapters.pgvector_client import SearchHit

# 並べ替え可能な3バンドのみ rank を持つ（'不明' は _BUDGETS に含まれるが順序対象外）。
_BUDGET_RANK: dict[str, int] = {
    band: i for i, band in enumerate(b for b in _BUDGETS if b != "不明")
}
_UNKNOWN = 99  # '不明' / 未付与 / NULL は末尾固定（NULLS LAST 流儀）


def budget_rank(band: str | None) -> int:
    """予算バンドを順序整数に変換する（不明・未知は末尾）。"""
    if not band:
        return _UNKNOWN
    return _BUDGET_RANK.get(band, _UNKNOWN)


def sort_by_budget_proximity(hits: list[SearchHit], target_band: str) -> list[SearchHit]:
    """target_band に近い予算順 → 同帯内は低信頼を末尾 → さらに関連度降順で並べ替える。

    絞らず並べ替えのみ。基準が並べ替え対象外（'不明'/不正値）なら no-op（恒等）。
    """
    tr = _BUDGET_RANK.get(target_band)
    if tr is None:
        return hits

    def key(h: SearchHit) -> tuple[int, int, float]:
        r = budget_rank(h.metadata.get("cls_budget"))
        band_dist = abs(r - tr) if r != _UNKNOWN else _UNKNOWN
        low_conf = 1 if h.metadata.get("is_low_confidence") else 0
        return (band_dist, low_conf, -float(h.score))

    return sorted(hits, key=key)  # Python sorted は安定ソート


def _hit_matches_client(h: SearchHit, client: str) -> bool:
    """hit が client（取引先/ブランド/コラボ名）に一致するかを広く判定する。

    2026-07-14 拡張（C・親クライアントで子コラボが出ない問題の即効対策）:
    従来は cls_project / client_name の単一メタだけを見ていたため、「サンマルクカフェ×
    祇園辻利コラボ」の資料が cls_project='祇園辻利' 側に分類されるとサンマルクカフェ検索で
    ブーストされず沈んだ。以下も一致対象に加える:
    - ``cls_entities``: Agent が抽出する取引先/代理店/ブランド/コラボ名の多値タグ（資料単位・
      名寄せ本体。まだ無い資料もあるので存在時のみ・list/CSV 両対応）
    - ``title``: 資料タイトル（DB フィルタ側は既に title を OR に含む・rerank と整合）
    - ``content``: chunk 本文に取引先名が出現（例: 本文が「サンマルクカフェ×祇園辻利」）

    メタ系は双方向部分一致、content は誤爆抑制のため片方向（needle in content）かつ 2 文字以上。
    """
    needle = client.strip()
    if not needle:
        return False

    def _bidi(s: str | None) -> bool:
        s = str(s or "").strip()
        return bool(s) and (needle in s or s in needle)

    # 単値メタ + タイトル（双方向部分一致）
    for k in ("cls_project", "client_name", "title"):
        if _bidi(h.metadata.get(k)):
            return True
    # 多値エンティティタグ（名寄せ本体・list または CSV）
    ents = h.metadata.get("cls_entities")
    if isinstance(ents, str):
        ents = [e for e in ents.split(",")]
    if isinstance(ents, list | tuple):
        if any(_bidi(e) for e in ents):
            return True
    # 本文出現（片方向・2 文字以上でノイズ抑制）
    if len(needle) >= 2 and needle in (h.content or ""):
        return True
    return False


def sort_by_client_match(hits: list[SearchHit], client: str) -> list[SearchHit]:
    """client に一致する hit（cls_project / client_name）を同点内で前出しする（絞らない）。

    ``sort_by_budget_proximity`` と同型の「絞らず 1 段だけ並べ替え」純関数。固有名詞クエリで
    汎用イントロ chunk が dense で実案件を上回る現象（bare entity 負け）を、rerank 後の
    最終 top_k 内で実案件 chunk が前に来るよう補正する。

    ソートキー（安定ソート・2 段）:
    1. client 一致 0/1 昇順（一致を前へ＝False(0) より True を先にしたいので ``not match``）
    2. -score 降順（一致グループ/非一致グループそれぞれ内は関連度が高い順）

    絞り込みは一切しない（母数を痩せさせない）。client が空 or 一致 0 件でも安定ソートで
    元の相対順序を保つため、取りこぼしても最悪「ランク後退なし」で安全。env-gate
    （``SEARCH_CLIENT_MATCH_SORT``）と呼び出し判定は呼び側（skill.py）の責務。
    """
    if not client or not client.strip():
        return hits

    def key(h: SearchHit) -> tuple[int, float]:
        match_rank = 0 if _hit_matches_client(h, client) else 1
        return (match_rank, -float(h.score))

    return sorted(hits, key=key)  # Python sorted は安定ソート
