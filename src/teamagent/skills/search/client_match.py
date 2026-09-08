"""クライアント名の照合（純関数・DB も env も読まない）。

``result_guard``（不一致警告の判定）と ``rerank``（client_match sort）の両方が使う
「この名前とこの名前は同じ取引先か」「このヒットは指定クライアントの資料か」を
1 か所に置く。循環 import を避けるため、名寄せの最小版（``normalize_client`` /
``clients_match``）もここに実体を置き、``result_guard`` は再エクスポートする。

設計の要点（2026-09 便A-1・クライアント不一致警告の誤爆停止）:
  - ``_hit_matches_client`` は **並べ替え用**（広く当てる・本文一致あり）。挙動不変。
  - ``hit_is_about_client`` は **警告抑止用**。cls_project / client_name / title /
    cls_entities の双方向一致＋別名だけで判定し、**本文（chunk content）は見ない**。
    500 字 chunk に競合社名が並ぶ比較ページで沈黙すると、別クライアントの NDA 資料が
    「指定クライアントの資料」の顔で渡る経路になるため。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from teamagent.adapters.pgvector_client import SearchHit

# 法人格・記号・空白は表記ゆれの主因なので照合前に落とす（名寄せの最小版）。
# ⚠️ ASCII 側（inc / corp / ltd / co.,ltd / k.k.）は **語境界つき**。境界なし＋IGNORECASE だと
# 「Vincent」→「Vent」「Prince」→「Pre」「Lincoln」→「Loln」「Scorpion」→「Sion」と社名の
# 中身を削る（両側正規化の clients_match では相殺されるが、DB 側を正規化しない ILIKE
# パターン生成（normalize_filter_client）と語彙表層（result_guard._surfaces）では退行になる）。
_LEGAL_SUFFIX_RE = re.compile(
    r"(株式会社|有限会社|合同会社|一般社団法人|公益社団法人|\(株\)|（株）|㈱|\(有\)|（有）|㈲"
    r"|(?<![A-Za-z])(?:co\.,?\s*ltd\.?|corporation|corp\.?|incorporated|inc\.?|k\.k\.|ltd\.?)"
    r"(?![A-Za-z]))",
    re.IGNORECASE,
)
# ⚠️ 長音記号「ー」は落とさない（「ユニー」と「ユニ」は別会社になりうる）。
_NOISE_RE = re.compile(r"[\s　・･,，.。/／\-‐－―_'\"“”’()（）\[\]【】]")

# 1 文字のクライアント名は誤爆（部分一致が何にでも当たる）ので照合対象にしない。
_MIN_CLIENT_LEN = 2

# LLM が filter_client に付けたまま渡してくる括弧・引用符（本番実測「（アース製薬）」）。
_BRACKETS_RE = re.compile(r"[「」『』（）()\[\]【】〈〉《》\"'“”‘’]")
# 末尾の敬称（本番実測「花王様」「花王向け」）。1 回剥いで足りる程度に留める。
_HONORIFIC_TAIL_RE = re.compile(r"(様|さま|さん|御中|殿|向け|宛)$")


def normalize_client(name: str | None) -> str:
    """クライアント名を照合用に正規化する（法人格・記号・空白を落として小文字化）。"""
    if not name:
        return ""
    out = _LEGAL_SUFFIX_RE.sub("", str(name))
    out = _NOISE_RE.sub("", out)
    return out.casefold()


def clients_match(a: str | None, b: str | None) -> bool:
    """2 つのクライアント名が「同じ取引先を指している」とみなせるか。

    正規化後にどちらかがもう一方を含めば一致（例「日本ガイシ」↔「日本ガイシ株式会社」）。
    判定不能（どちらかが空 / 短すぎる）は **True 側**へ倒す＝警告を出さない（fail-open）。
    誤警告は「合っているのに違うと言う」＝検索結果への信頼を壊すため、こちらが重い。
    """
    na, nb = normalize_client(a), normalize_client(b)
    if len(na) < _MIN_CLIENT_LEN or len(nb) < _MIN_CLIENT_LEN:
        return True
    return na in nb or nb in na


def names_overlap(a: str | None, b: str | None) -> bool:
    """``clients_match`` の **fail-open 無し**版（両方 2 文字以上で、どちらかが他方を含む）。

    別名辞書のキー照合やヒット側フィールドの照合に使う。ここで fail-open にすると
    「短い値に何でも当たる＝何でも一致」になり、一致した理由（matched_via）が嘘になる。
    判定不能の扱いは呼び出し側（``explain_client_guard``）が明示的に決める。
    """
    na, nb = normalize_client(a), normalize_client(b)
    if len(na) < _MIN_CLIENT_LEN or len(nb) < _MIN_CLIENT_LEN:
        return False
    return na in nb or nb in na


def normalize_filter_client(value: str | None) -> str | None:
    """LLM が渡す ``filter_client`` から括弧・敬称・法人格を剥がす（ILIKE パターン生成の前処理）。

    本番実測: 「（アース製薬）」「花王様」「ホーユー株式会社」がそのまま渡り、
    ``cls_project ILIKE '%（アース製薬）%'`` が 0 件 → fail-open 再検索で無関係ヒット →
    今度は「本物っぽい」不一致警告、という連鎖が起きていた。
    空白は残す（「日本 ガイシ」を「日本ガイシ」にはしない＝ILIKE の意味を変えない）。
    剥いだ結果が短すぎる（2 文字未満）なら元の値（strip のみ）を返す。
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    # 法人格 → 括弧 → 法人格 の順。括弧を先に剥ぐと「（株）」「(株)」の括弧だけが消えて
    # 「株」が残り、ILIKE '%日本ガイシ株%' が cls_project='日本ガイシ' に当たらない。
    # 括弧の後にもう 1 回当てるのは「（株式会社ホーユー）」のように括弧の内側に
    # 法人格が残る形のため（冪等なので 2 回で足りる）。
    out = _LEGAL_SUFFIX_RE.sub("", raw)
    out = _BRACKETS_RE.sub("", out)
    out = _LEGAL_SUFFIX_RE.sub("", out)
    out = out.strip(" 　")
    out = _HONORIFIC_TAIL_RE.sub("", out).strip(" 　")
    if len(out) < _MIN_CLIENT_LEN:
        return raw
    return out


def hit_entities(hit: SearchHit) -> list[str]:
    """``cls_entities``（list または CSV）を文字列リストに正規化する。無ければ []。"""
    meta = getattr(hit, "metadata", None) or {}
    ents = meta.get("cls_entities")
    if isinstance(ents, str):
        return [e.strip() for e in ents.split(",") if e.strip()]
    if isinstance(ents, list | tuple):
        return [str(e).strip() for e in ents if str(e).strip()]
    return []


def _hit_matches_client(h: SearchHit, client: str) -> bool:
    """hit が client（取引先/ブランド/コラボ名）に一致するかを広く判定する（**並べ替え用**）。

    2026-07-14 拡張（C・親クライアントで子コラボが出ない問題の即効対策）:
    従来は cls_project / client_name の単一メタだけを見ていたため、「サンマルクカフェ×
    祇園辻利コラボ」の資料が cls_project='祇園辻利' 側に分類されるとサンマルクカフェ検索で
    ブーストされず沈んだ。以下も一致対象に加える:
    - ``cls_entities``: Agent が抽出する取引先/代理店/ブランド/コラボ名の多値タグ（資料単位・
      名寄せ本体。まだ無い資料もあるので存在時のみ・list/CSV 両対応）
    - ``title``: 資料タイトル（DB フィルタ側は既に title を OR に含む・rerank と整合）
    - ``content``: chunk 本文に取引先名が出現（例: 本文が「サンマルクカフェ×祇園辻利」）

    メタ系は双方向部分一致、content は誤爆抑制のため片方向（needle in content）かつ 2 文字以上。
    ⚠️ 警告抑止（result_guard）には使わない。本文一致で沈黙させると NDA 資料の越境経路になる。
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


def hit_is_about_client(
    hit: SearchHit,
    asked: str,
    *,
    aliases: Iterable[str] = (),
    use_entities: bool = True,
) -> str | None:
    """このヒットが ``asked``（利用者が指定した取引先）の資料と言えるか（**警告抑止用**）。

    Returns:
        一致した根拠（``client_name`` / ``cls_project`` / ``title`` / ``entities`` /
        ``alias``）。どれにも当たらなければ None。

    判定は名寄せ後の双方向部分一致（``names_overlap``）。**本文（content）は見ない**。
    ``aliases`` は ``asked`` の別名（ブランド↔法人）で、当たったら根拠は ``alias``。
    ``use_entities=False`` なら cls_entities を見ない（env で切るための口）。
    """
    meta = getattr(hit, "metadata", None) or {}
    forms: list[tuple[str, str | None]] = [(asked, None)]
    forms.extend((alias, "alias") for alias in aliases if alias)
    for form, via_alias in forms:
        for key in ("client_name", "cls_project", "title"):
            if names_overlap(form, str(meta.get(key) or "")):
                return via_alias or key
        if use_entities and any(names_overlap(form, ent) for ent in hit_entities(hit)):
            return via_alias or "entities"
    return None


__all__ = [
    "_MIN_CLIENT_LEN",
    "_hit_matches_client",
    "clients_match",
    "hit_entities",
    "hit_is_about_client",
    "names_overlap",
    "normalize_client",
    "normalize_filter_client",
]
