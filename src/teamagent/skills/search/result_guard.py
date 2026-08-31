"""検索結果の「顔つき」をサーバ側で決定論的に補正するヘッダ生成（純関数）。

背景（本番実測 2026-08）:
  - 質問に直接一致する資料が無くても、要約器は残った低スコアヒットを根拠に
    自信のある口調で書く。営業には「これが答え」に見える。
  - 「A 社の提案書ある?」に対し top1 が B 社の資料でも、要約器は B 社名を明示しない
    ことがあり、**関係ないクライアントが関連資料の顔で出る**。

対策は **LLM の作文に任せず**、retrieval の実数値（top1 スコア / ヒットの client_name）
だけを見てコードでヘッダ文字列を決める。プロンプトを足す方式は「守られないことがある」
のに対し、ここは守られないことが原理的に無い。

本モジュールは純関数のみ（``os.environ`` を読まない・DB を引かない）。env の解決と
DB 由来のクライアント語彙の受け渡しは呼び出し側（``search/skill.py``）の責務。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.ingest.industry_taxonomy import normalize_industry

# ヒットはあるが、どれも質問に直接は答えていない（top1 スコアが閾値未満）。
WEAK_RESULT_NOTICE = (
    "⚠️ 質問に直接一致する資料は見つかりませんでした。以下は関連度の低い参考情報です。"
)

# クライアント名を含むクエリなのに、top1 が別クライアントの資料だった。
_CLIENT_MISMATCH_TEMPLATE = "⚠️ ご指定のクライアントの資料ではありません（ヒット: {hit}）。"

# 業種を絞ったのに、その業種に分類された資料が 1 件も無かった。
#
# 実測された事故（2026-08-28）: 「ヨーグルト 乳製品」の検索で **食品業種の資料は 0 件**
# だったにもかかわらず、玩具・鉄道・金融の提案書を根拠に
# 「ヨーグルト向け UGC 施策」の回答が生成された。中身自体は根拠のある記述だったが、
# **「該当業種の資料が無い」と言わずに答えを作った**点が危険で、
# 営業がそのまま提案に使うと出典の無い主張になる。
_INDUSTRY_MISS_TEMPLATE = (
    "⚠️ 「{industry}」に分類された資料は見つかりませんでした。"
    "以下は業種が未分類の資料をもとにした参考情報です。"
)

# 法人格・記号・空白は表記ゆれの主因なので照合前に落とす（名寄せの最小版）。
_LEGAL_SUFFIX_RE = re.compile(
    r"(株式会社|有限会社|合同会社|一般社団法人|公益社団法人|\(株\)|（株）|㈱|\(有\)|（有）|㈲"
    r"|co\.,?\s*ltd\.?|corporation|corp\.?|inc\.?|k\.k\.|ltd\.?)",
    re.IGNORECASE,
)
# ⚠️ 長音記号「ー」は落とさない（「ユニー」と「ユニ」は別会社になりうる）。
_NOISE_RE = re.compile(r"[\s　・･,，.。/／\-‐－―_'\"“”’()（）\[\]【】]")

# 1 文字のクライアント名は誤爆（部分一致が何にでも当たる）ので照合対象にしない。
_MIN_CLIENT_LEN = 2

# 自社・自社プロダクトの名前。**「利用者が指定したクライアント」として扱わない。**
#
# 実測された事故（2026-08-28）: 営業が
# 「NewsTV 事例動画 ショート動画 UGC ヨーグルト 乳製品」と検索したところ、
# クライアント語彙に自社プロダクト名 "NewsTV" が入っていたため
# query_client="NewsTV" と確定し、top1 が花王の資料だったことで
# 「⚠️ ご指定のクライアントの資料ではありません（ヒット: 花王…）」が
# **回答の一番上**に出た。利用者はクライアントを指定していないので、これは誤警告である。
#
# 社内クエリはほぼ必ず自社名を含むため、放置すると誤警告が常態化して
# 「この警告は無視してよい」と学習され、本物の不一致まで効かなくなる。
_SELF_ORG_NAMES: tuple[str, ...] = (
    "NewsTV",
    "ニュースTV",
    "ニュースティービー",
    "ベクトル",
    "Vector",
    "AiLa",
    "アイラ",
    "Aico",
    "アイコ",
)


def is_self_org_name(name: str | None) -> bool:
    """``name`` が自社・自社プロダクト名か（＝クライアント指定として扱わない）。

    ``clients_match`` と同じ正規化で比較する（法人格・記号・空白を落として casefold）。
    """
    normalized = normalize_client(name)
    if not normalized:
        return False
    return any(normalized == normalize_client(own) for own in _SELF_ORG_NAMES)


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


def hit_client_name(hit: SearchHit) -> str:
    """このヒットが属するクライアント名。``client_name``（営業 FB）→ ``cls_project`` の順。

    ``client_name`` は is_sales_fb の行にしか入らないため、Drive 資料は自動分類の
    ``cls_project``（全資料に付く取引先）で見る。
    """
    meta = getattr(hit, "metadata", None) or {}
    for key in ("client_name", "cls_project"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    return ""


def _hit_industry(hit: SearchHit) -> str | None:
    """このヒットの業種（正準値）。未分類・未知値は None。

    保存済みデータには ``旅行`` と ``旅行・観光`` のような表記ゆれが実在するため、
    比較前に必ず正規化する（再分類バッチを走らせずに揺れを吸収する唯一の手段）。
    """
    meta = getattr(hit, "metadata", None) or {}
    for key in ("industry", "cls_industry"):
        value = str(meta.get(key) or "").strip()
        if value:
            return normalize_industry(value)
    return None


def hit_client_vocabulary(hits: Sequence[SearchHit]) -> list[str]:
    """ヒット集合から観測されたクライアント名の一覧（辞書が引けないときの代替語彙）。"""
    seen: dict[str, None] = {}
    for hit in hits:
        name = hit_client_name(hit)
        if len(normalize_client(name)) >= _MIN_CLIENT_LEN:
            seen.setdefault(name, None)
    return list(seen)


def detect_query_client(query: str, vocabulary: Sequence[str]) -> str | None:
    """クエリ文字列に含まれる既知クライアント名（最長一致）。無ければ None。

    正規化してから部分一致を取るので「(株)ユニー」「ユニー株式会社」表記でも当たる。
    最長優先は短い部分名（例「ユニ」）の誤爆を避けるため。
    """
    normalized_query = normalize_client(query)
    if not normalized_query:
        return None
    matched = [
        name
        for name in vocabulary
        if len(normalize_client(name)) >= _MIN_CLIENT_LEN
        and normalize_client(name) in normalized_query
    ]
    if not matched:
        return None
    return max(matched, key=lambda n: len(normalize_client(n)))


def build_result_header(
    *,
    query: str,
    hits: Sequence[SearchHit],
    weak_threshold: float,
    query_client: str | None = None,
    asked_industry: str | None = None,
) -> str:
    """回答本文の先頭へ付ける警告ヘッダ（該当なしなら空文字）。

    Args:
        query: 利用者のクエリ原文（クライアント名の検出に使う）。
        hits: retrieval 確定後のヒット（先頭が top1）。
        weak_threshold: top1 スコアがこの値未満なら「関連度が低い」と表示する。
            0 以下なら弱ヒット判定そのものを行わない（実質の無効化スイッチ）。
        query_client: 呼び出し側が既に確定させたクエリ内クライアント名
            （明示 filter_client / DB のクライアント辞書一致）。None ならヒット集合から
            語彙を作って推定する。

    ヒットが 0 件のときは何も付けない（「該当する資料が見つかりませんでした」を
    要約側が既に返しており、警告を重ねる意味が無い）。
    """
    if not hits:
        return ""
    lines: list[str] = []

    top = hits[0]
    top_score = float(getattr(top, "score", 0.0) or 0.0)
    if weak_threshold > 0.0 and top_score < weak_threshold:
        lines.append(WEAK_RESULT_NOTICE)

    # 業種を絞ったのに、その業種の資料が 1 件も無い（soft フィルタなので
    # 業種未分類の資料だけが残っている）状態を明示する。
    # filter_industry は soft（industry = 値 OR NULL）なので「別業種が混ざる」ことは
    # 起きない。起きるのは「全部 NULL だった」であり、それを黙って要約させない。
    if asked_industry:
        wanted = normalize_industry(asked_industry) or asked_industry
        if not any(_hit_industry(hit) == wanted for hit in hits):
            lines.append(_INDUSTRY_MISS_TEMPLATE.format(industry=wanted))

    asked = query_client or detect_query_client(query, hit_client_vocabulary(hits))
    # 自社・自社プロダクト名は「利用者が指定したクライアント」ではない。
    # ここで落とさないと、社内クエリのほぼ全部で誤警告が出る（2026-08-28 実測）。
    if is_self_org_name(asked):
        asked = None
    if asked:
        top_client = hit_client_name(top)
        if top_client and not clients_match(asked, top_client):
            lines.append(_CLIENT_MISMATCH_TEMPLATE.format(hit=top_client))

    return "\n".join(lines)


def prefix_header(header: str, body: str) -> str:
    """ヘッダを本文の**先頭**へ結合する。ヘッダ無し / 本文無しならそれぞれ素通し。

    既に同じヘッダが付いている本文には重ねない（二段返しの第一報と後追いの
    両方から呼ばれても文言が二重にならないようにする）。
    """
    if not header:
        return body
    if not body:
        return header
    if body.startswith(header):
        return body
    return f"{header}\n\n{body}"


__all__ = [
    "WEAK_RESULT_NOTICE",
    "build_result_header",
    "clients_match",
    "detect_query_client",
    "hit_client_name",
    "hit_client_vocabulary",
    "is_self_org_name",
    "normalize_client",
    "prefix_header",
]
