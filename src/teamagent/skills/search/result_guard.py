"""検索結果の「顔つき」をサーバ側で決定論的に補正するヘッダ生成（純関数）。

背景（本番実測 2026-08）:
  - 質問に直接一致する資料が無くても、要約器は残った低スコアヒットを根拠に
    自信のある口調で書く。営業には「これが答え」に見える。
  - 「A 社の提案書ある?」に対し top1 が B 社の資料でも、要約器は B 社名を明示しない
    ことがあり、**関係ないクライアントが関連資料の顔で出る**。

対策は **LLM の作文に任せず**、retrieval の実数値（top1 スコア / ヒットの client_name）
だけを見てコードでヘッダ文字列を決める。プロンプトを足す方式は「守られないことがある」
のに対し、ここは守られないことが原理的に無い。

2026-09 便A-1（クライアント不一致警告の誤爆停止）:
  本番実測 10 件の警告が全件誤判定だった（「ユニークユーザー数」に語彙「ユニー」が
  当たる／「アース製薬」の資料が cls_project=ハビットプロ（同社ブランド）で別会社扱い 等）。
  直したのは「警告を減らす」方向だけ:
  - クエリ内クライアント検出に **語境界**（カタカナ・英数字のみ）を入れる
    （``find_client_mention(strict=True)``。boost / sort は現行の緩い substring のまま）。
  - ヒット側は cls_project だけでなく client_name / title / cls_entities の双方向一致と
    静的な別名辞書（ブランド↔法人）で「指定クライアントの資料か」を判定する。
    **本文（chunk content）は見ない**（競合比較ページで沈黙すると NDA 資料の越境経路になる）。
  - 判定不能は警告しない（fail-open）。本物の不一致（例「資生堂の提案書」で top1 が花王の
    資料）は今までどおり警告する。

本モジュールは純関数のみ（``os.environ`` を読まない・DB を引かない）。env の解決と
DB 由来のクライアント語彙の受け渡しは呼び出し側（``search/skill.py``）の責務。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.ingest.industry_taxonomy import normalize_industry
from teamagent.skills.search.client_match import (
    _LEGAL_SUFFIX_RE,
    _MIN_CLIENT_LEN,
    clients_match,
    hit_is_about_client,
    names_overlap,
    normalize_client,
)

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

# 純 ASCII の語彙は 3 文字以上でないと照合しない（「IR」「PR」「AI」が「IR関連」に当たる）。
# 英数字の 2 文字は取引先名でなく一般略語であることの方が圧倒的に多い（本番実測 09-03）。
_MIN_ASCII_CLIENT_LEN = 3

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

# ブランド↔法人の別名辞書（**静的 seed のみ**・対称）。
#
# 本番実測（2026-09-02〜03）で「同じ取引先なのに別会社扱い」になった対:
#   - 「（アース製薬）の社内資料」→ top1 cls_project=ハビットプロ（アース製薬のブランド）
#   - 「ホーユー株式会社」→ top1 cls_project=SOMARCA（ホーユーのブランド）
#   - 「エリスショーツの提案」→ top1 cls_project=大王製紙株式会社（エリスは同社ブランド）
# DB（title / cls_entities の共起）から自動生成はしない: 一般社員の投稿 1 件で
# 競合ペア（花王↔資生堂）が別名に入り、全員の警告を無効化できてしまうため。
# 運用追加はこのタプルへ明示的に足し、テストで固定する（gsheet overrides と同じ流儀）。
_ALIAS_SEED: tuple[tuple[str, str], ...] = (
    ("アース製薬", "ハビットプロ"),
    ("ホーユー", "SOMARCA"),
    ("エリス", "大王製紙"),
    ("花王", "花王グループカスタマーマーケティング"),
)


def is_self_org_name(name: str | None) -> bool:
    """``name`` が自社・自社プロダクト名か（＝クライアント指定として扱わない）。

    ``clients_match`` と同じ正規化で比較する（法人格・記号・空白を落として casefold）。
    """
    normalized = normalize_client(name)
    if not normalized:
        return False
    return any(normalized == normalize_client(own) for own in _SELF_ORG_NAMES)


def aliases(asked: str | None) -> set[str]:
    """``asked`` の別名（ブランド↔法人）。seed のキー照合は名寄せ後の双方向部分一致。

    「エリスショーツ」で「エリス」の対（大王製紙）が引ける。対称なので
    「ハビットプロ」から「アース製薬」も引ける。無ければ空集合。
    """
    if not asked:
        return set()
    out: set[str] = set()
    for a, b in _ALIAS_SEED:
        if names_overlap(asked, a):
            out.add(b)
        if names_overlap(asked, b):
            out.add(a)
    # asked そのもの（正規化後に同一）は「別名」ではない。
    asked_norm = normalize_client(asked)
    return {alias for alias in out if normalize_client(alias) != asked_norm}


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


# ── クエリ内クライアント検出（語境界つき）────────────────────────────────────


def _is_katakana(ch: str) -> bool:
    """カタカナ・長音「ー」・中黒「・」・半角カナ。連続していれば 1 語とみなす。"""
    return (
        "\u30a0" <= ch <= "\u30ff"  # カタカナ（ー・・ を含む）
        or "\u31f0" <= ch <= "\u31ff"  # 小書きカタカナ拡張
        or "\uff66" <= ch <= "\uff9f"  # 半角カナ
    )


def _is_alnum(ch: str) -> bool:
    """ASCII / 全角の英数字。漢字・かなは含めない（``str.isalnum`` は漢字も True になる）。"""
    if ch.isascii():
        return ch.isalnum()
    return (
        "\uff10" <= ch <= "\uff19"  # 全角数字
        or "\uff21" <= ch <= "\uff3a"  # 全角大文字
        or "\uff41" <= ch <= "\uff5a"  # 全角小文字
    )


def _fold(text: str) -> str:
    """大文字小文字を畳む。**文字数を変えない**（オフセットを境界判定に使うため）。"""
    return "".join(ch.lower() if len(ch.lower()) == 1 else ch for ch in text)


def _surfaces(name: str) -> list[str]:
    """語彙 1 件の照合表層（原文と、法人格を剥いだ形）。短すぎるものは除く。"""
    raw = name.strip()
    stripped = _LEGAL_SUFFIX_RE.sub("", raw).strip(" 　")
    out: list[str] = []
    for surface in (raw, stripped):
        if not surface or surface in out:
            continue
        if len(normalize_client(surface)) < _MIN_CLIENT_LEN:
            continue
        if surface.isascii() and len(surface) < _MIN_ASCII_CLIENT_LEN:
            continue
        out.append(surface)
    return out


def _boundary_ok(query: str, start: int, end: int, surface: str) -> bool:
    """一致 ``query[start:end]`` が語の途中で切れていないか（カタカナ・英数字のみ判定）。

    端の文字がカタカナなら隣接がカタカナでないこと、英数字なら隣接が英数字でないこと。
    漢字・かな・記号・句読点・行頭行末は境界とみなす（「花王様」「株式会社明治」は成立、
    「ユニークユーザー」に「ユニー」・「NGKX」に「NGK」・「IR関連」に「IR」は不成立）。
    """
    first, last = surface[0], surface[-1]
    if start > 0:
        left = query[start - 1]
        if _is_katakana(first) and _is_katakana(left):
            return False
        if _is_alnum(first) and _is_alnum(left):
            return False
    if end < len(query):
        right = query[end]
        if _is_katakana(last) and _is_katakana(right):
            return False
        if _is_alnum(last) and _is_alnum(right):
            return False
    return True


def _mention_at(query: str, surface: str) -> int | None:
    """語境界を満たす最初の出現位置。``str.find`` の反復（正規表現は組まない）。

    語彙は DB 由来の非特権入力（「(株)P&G+」「A[B]」「C++」）で、正規表現メタ文字を普通に
    含む。名前から正規表現を組むと 1 件の悪い名前で全検索が落ちる（ReDoS も成立する）。
    """
    pos = 0
    while True:
        start = query.find(surface, pos)
        if start < 0:
            return None
        end = start + len(surface)
        if _boundary_ok(query, start, end, surface):
            return start
        pos = start + 1


def find_client_mention(
    query: str, vocabulary: Sequence[str], *, strict: bool = False
) -> str | None:
    """クエリ文字列に含まれる既知クライアント名（最長一致）。無ければ None。

    Args:
        strict: True なら語境界つき（result_guard の「利用者が指定したクライアント」検出）。
            False は現行の緩い substring（client_boost / client_match_sort が使う。
            「エリスショーツ」で「エリス」の資料を寄せる再現率を落とさない）。

    照合中に何が起きても None（fail-open＝警告もブーストも出さない側）。
    """
    try:
        if not strict:
            matched = [n for n in vocabulary if n and n in query]
            return max(matched, key=len) if matched else None
        folded_query = _fold(query)
        best: tuple[int, str] | None = None
        for name in vocabulary:
            if not name:
                continue
            for surface in _surfaces(name):
                if _mention_at(folded_query, _fold(surface)) is None:
                    continue
                if best is None or len(surface) > best[0]:
                    best = (len(surface), name)
        return best[1] if best else None
    except Exception:
        return None


def detect_query_client(query: str, vocabulary: Sequence[str]) -> str | None:
    """クエリ文字列に含まれる既知クライアント名（語境界つき最長一致）。無ければ None。

    ``find_client_mention(strict=True)`` の薄いラッパ（公開名の維持）。
    """
    return find_client_mention(query, vocabulary, strict=True)


# ── ヒット側判定（「asked の資料か」）────────────────────────────────────────


def explain_client_guard(
    asked: str, top: SearchHit, *, use_entities: bool = True
) -> tuple[str, bool]:
    """top1 が ``asked`` の資料かを判定し ``(matched_via, warned)`` を返す。

    matched_via（ログ用 enum・文字列は固定語のみ）:
      ``undetermined``（asked / top のクライアント名が短すぎて判定不能）・
      ``unknown_hit``（top1 に取引先メタが無い）・``client_name`` / ``cls_project`` /
      ``title`` / ``entities`` / ``alias``（一致した根拠）・``none``（一致なし＝警告）。
    判定不能は必ず warned=False（fail-open）。本文（content）は見ない。
    """
    if len(normalize_client(asked)) < _MIN_CLIENT_LEN:
        return ("undetermined", False)
    top_client = hit_client_name(top)
    if not top_client:
        return ("unknown_hit", False)
    if len(normalize_client(top_client)) < _MIN_CLIENT_LEN:
        return ("undetermined", False)
    via = hit_is_about_client(top, asked, aliases=aliases(asked), use_entities=use_entities)
    if via:
        return (via, False)
    return ("none", True)


def build_result_header(
    *,
    query: str,
    hits: Sequence[SearchHit],
    weak_threshold: float,
    query_client: str | None = None,
    asked_industry: str | None = None,
    use_entities: bool = True,
    decision: dict[str, Any] | None = None,
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
        use_entities: ヒット側判定で cls_entities を見るか（env で切るための口）。
        decision: 渡されたら、クライアント判定の観測値（``asked_source`` / ``matched_via`` /
            ``warned``）を書き込む（呼び出し側のログ用。asked が無ければ何も書かない）。

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

    asked = query_client
    asked_source = "caller"
    if not asked:
        asked = detect_query_client(query, hit_client_vocabulary(hits))
        asked_source = "hits"
    # 自社・自社プロダクト名は「利用者が指定したクライアント」ではない。
    # ここで落とさないと、社内クエリのほぼ全部で誤警告が出る（2026-08-28 実測）。
    if asked and is_self_org_name(asked):
        if decision is not None:
            decision.update(asked_source=asked_source, matched_via="self_org", warned=False)
        asked = None
    if asked:
        matched_via, warned = explain_client_guard(asked, top, use_entities=use_entities)
        if decision is not None:
            decision.update(asked_source=asked_source, matched_via=matched_via, warned=warned)
        if warned:
            lines.append(_CLIENT_MISMATCH_TEMPLATE.format(hit=hit_client_name(top)))

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
    "aliases",
    "build_result_header",
    "clients_match",
    "detect_query_client",
    "explain_client_guard",
    "find_client_mention",
    "hit_client_name",
    "hit_client_vocabulary",
    "is_self_org_name",
    "normalize_client",
    "prefix_header",
]
