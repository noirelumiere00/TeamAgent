"""ナレッジ Q&A クエリから資料種別フィルタを抽出する（DB 非依存・純ロジック）。

「○○業界の提案事例を教えて」「△△案件の議事録ある？」のような聞き方から、
ingest 自動分類（``teamagent.ingest.classify``）が付与した ``cls_doc_type`` で
絞り込むためのフィルタを取り出す。案件名・業界は既存の client boost /
filter_industry が担うため、ここでは資料種別だけを扱う。

保守的設計: 明確な資料種別の語があるときだけフィルタを返す。無ければ None
（呼び出し側は通常の意味検索にフォールバック）。
"""

from __future__ import annotations

import re
import unicodedata

from teamagent.ingest.industry_taxonomy import match_industry_keyword

# 資料種別キーワード → cls_doc_type 正規値（classify._DOC_TYPES と一致させる）。
# 具体的・複合語を先に評価する（「提案事例」を「提案書」へ寄せる）。
_DOC_TYPE_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("提案事例", "提案書", "提案資料", "提案の事例", "提案例"), "提案書"),
    (("議事録", "打ち合わせメモ", "打合せメモ", "ミーティングメモ", "MTGメモ"), "議事録"),
    (("報告書", "レポート"), "報告書"),
    (("価格表", "料金表", "価格リスト"), "価格表"),
    (("契約書", "契約条件"), "契約"),
)


# 業界キーワード表は teamagent.ingest.industry_taxonomy が唯一の真実源。
# ここに別の表を持つと、まさに今回直している「語彙が層ごとに分かれる」問題を再生産する。
# （旧実装はここに 11 語の独自表を持っており、値も "メーカー" のように非正準だった）


# 商談フェーズキーワード → cls_phase 正規値（classify._PHASES と一致）。過剰絞り込みを
# 避けるため、フェーズが明確な語だけ拾う（doc_type と衝突する「提案」単独は含めない）。
_PHASE_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("受注", "成約"), "受注"),
    (("失注", "見送り", "ロスト"), "失注"),
    (("見積フェーズ", "見積段階"), "見積"),
    (("ヒアリング", "初回接触"), "ヒアリング"),
)


# 施策タイプキーワード → cls_solution 正規値（classify._SOLUTIONS の代表語彙と一致）。
# 具体的・複合語を先に評価する。
_SOLUTION_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("インフルエンサー", "インフルエンサーマーケ", "タイアップ", "起用"), "インフルエンサー"),
    (("動画広告", "動画施策", "ショート動画", "リール", "TikTok広告"), "動画広告"),
    (("SNS運用", "SNS運営", "アカウント運用", "SNS投稿"), "SNS運用"),
    (("SEO", "検索対策", "検索面"), "SEO"),
    (("Web制作", "サイト制作", "LP制作", "ホームページ制作"), "Web制作"),
    (("広告運用", "運用型広告", "リスティング", "Web広告"), "広告運用"),
    (("イベント", "展示会", "ポップアップ", "体験会"), "イベント"),
)


# 予算帯を示す定性キーワード → cls_budget 正規バンド（classify._BUDGETS と一致）。
# 金額そのものは _budget_from_amount で別途数値判定する。
_BUDGET_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("高予算", "大型予算"), "500万〜"),
    (("低予算", "小予算"), "〜100万"),
    (("中予算", "数百万"), "100〜500万"),
)

# 「予算 / 予算感」を伴う金額表現（例: 予算100万くらい / 予算は300万円）を拾う正規表現。
# 万単位の数値をバンドへ写像する（〜100万 / 100〜500万 / 500万〜）。
_BUDGET_AMOUNT_RE = re.compile(r"予算[はが]?\s*[約]?\s*(\d{1,5})\s*万")


# ターゲット / 客層キーワード → cls_target 正規値（短い代表語）。自由文だが検索の安定の
# ため代表語に寄せる。
_TARGET_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("若年女性", "若い女性", "20代女性", "F1層"), "若年女性"),
    (("主婦", "ママ", "母親"), "主婦"),
    (("シニア", "高齢者", "中高年"), "シニア"),
    (("BtoB", "B2B", "法人向け", "企業向け"), "BtoB"),
    (("ファミリー", "家族", "子育て世帯"), "ファミリー"),
    (("Z世代", "ゼット世代", "若者", "10代"), "Z世代"),
)


def _budget_from_amount(query: str) -> str | None:
    """「予算100万くらい」等の金額表現から正規バンドを判定する。無ければ None。"""
    m = _BUDGET_AMOUNT_RE.search(query)
    if not m:
        return None
    man = int(m.group(1))  # 万単位
    if man < 100:
        return "〜100万"
    if man < 500:
        return "100〜500万"
    return "500万〜"


def extract_knowledge_filters(query: str) -> dict[str, str] | None:
    """クエリから資料種別・フェーズ・施策・予算・ターゲットの絞り込みを抽出する。

    返り値:
        {"cls_doc_type": "提案書", "cls_solution": "動画広告", "cls_budget": "〜100万"}
        のような複合 dict（該当キーのみ）。該当語が無ければ None
        （= 呼び出し側は通常の意味検索にフォールバック）。

    既存キー（cls_doc_type / cls_phase）の命名・返り値形は不変。新軸は追加キーのみで、
    呼び出し側（skill.py → pgvector metadata_filters）は任意キーを汎用的に通すため配線不要。
    """
    if not query:
        return None
    filters: dict[str, str] = {}
    for keywords, doc_type in _DOC_TYPE_KEYWORDS:
        if any(kw in query for kw in keywords):
            filters["cls_doc_type"] = doc_type
            break
    for keywords, phase in _PHASE_KEYWORDS:
        if any(kw in query for kw in keywords):
            filters["cls_phase"] = phase
            break
    for keywords, solution in _SOLUTION_KEYWORDS:
        if any(kw in query for kw in keywords):
            filters["cls_solution"] = solution
            break
    # 予算: 金額表現を優先し、無ければ定性語（高予算 等）で判定。読めなければ載せない。
    budget = _budget_from_amount(query)
    if budget is None:
        for keywords, band in _BUDGET_KEYWORDS:
            if any(kw in query for kw in keywords):
                budget = band
                break
    if budget is not None:
        filters["cls_budget"] = budget
    for keywords, target in _TARGET_KEYWORDS:
        if any(kw in query for kw in keywords):
            filters["cls_target"] = target
            break
    return filters or None


# ── 施策実績（ショート動画DBの案件ごとの実績文書）の意図判定（2026-09-29）─────────────
# 施策実績の文書（ingest.campaign_aggregate・campaign_aggregate="true"・cls_doc_type=施策実績）
# には cls_solution が無い。そのため「ショート動画施策の実績」を聞かれると
# _SOLUTION_KEYWORDS が cls_solution=動画広告 を付け、SQL の AND（キーが無い文書は除外）で
# 施策実績だけが検索から落ちる。加えて枚数の多い提案 PDF が rerank プールの枠を埋める。
# 検索側（skill._apply_campaign_floor）はこの判定が True のときだけ施策実績の床を張る。

# 資料そのものを探している語。これがあれば施策実績の床は張らない（提案書を探す人に
# 実績の集計文書を混ぜない）。
_MATERIAL_SEEKING_KEYWORDS: tuple[str, ...] = (
    "提案書",
    "提案資料",
    "提案事例",
    "提案の事例",
    "提案例",
    "議事録",
    "打ち合わせメモ",
    "価格表",
    "料金表",
    "契約書",
)

# 実績（本数・再生・伸び・結果）を聞いている語。英字は NFKC＋casefold で照合する。
_CAMPAIGN_RESULTS_KEYWORDS: tuple[str, ...] = (
    "施策実績",
    "実績",
    "再生",
    "伸び",
    "バズ",
    "投稿",
    "結果",
    "効果",
    "施策",
    "ショート動画",
    "動画",
    "TikTok",
    "リール",
    "ティックトック",
)

_CAMPAIGN_DOC_TYPE = "施策実績"


def _normalize_for_match(text: str) -> str:
    """全角英数・大文字小文字の揺れを吸収する（ＴｉｋＴｏｋ / tiktok も拾う）。"""
    return unicodedata.normalize("NFKC", text).casefold()


#: 案件決定（#proj-01 の新規案件投稿・案件決定 V2 シート）を求める聞き方の語。
_DEAL_KEYWORDS: tuple[str, ...] = (
    "受注",
    "案件決定",
    "新規案件",
    "決まった案件",
    "決定した案件",
    "決定案件",
    "発注",
    "同行",
    "経由",
    "代理店",
    "進行中の案件",
    "動いている案件",
)
#: 固有名として拾わない英字・カタカナ（一般語）。
_DEAL_TERM_STOPWORDS: frozenset[str] = frozenset(
    {
        "pr", "tiktok", "sns", "kw", "cm", "tv", "tvcm", "ugc", "tto", "vseo", "aico",
        "instagram", "youtube", "x", "line",
        "ショート", "ショート動画", "インフルエンサー", "キャンペーン", "プロモーション",
        "クライアント", "プラン", "コンテンツ", "メディア", "ブランド", "レポート",
    }
)  # fmt: skip
_DEAL_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9&.\-]{1,30}|[ァ-ヴー]{3,20}")
#: 漢字の社名・代理店名（博報堂・電通・大広 など）は「経由／との／の案件／の受注」の直前だけ拾う
#: （漢字は一般語と区別できないため、位置で絞る）。
_DEAL_KANJI_TERM_RE = re.compile(r"([一-龥々ヶ]{2,12}?)(?=経由|との|の案件|の受注|の決定|案件)")
_DEAL_KANJI_STOPWORDS: frozenset[str] = frozenset(
    {
        "今月", "先月", "来月", "今年", "去年", "最近", "新規", "過去", "全部", "全体", "社内",
        "弊社", "自社", "当社", "決定", "受注", "直近", "今週", "先週", "代理店", "大型", "既存",
        "経由", "案件", "取引", "実績", "決定案件", "新規案件",
    }
)  # fmt: skip


#: 社名・代理店名と並ぶと「その相手との案件」を聞いていると読む語（10-06 実機: 「ADKの案件教えて」で
#: 案件決定の投稿が候補に入らず、決定済み 3 件が出なかった）。単独では案件決定の意図にしない。
_DEAL_WITH_NAME_KEYWORDS: tuple[str, ...] = ("案件", "取引", "お仕事", "実績")


def is_deal_intent(query: str) -> bool:
    """決まった案件（受注・案件決定・代理店経由 等）を聞いているか。

    「ADKの案件教えて」のように、固有名（英字・カタカナの社名）と「案件／取引／実績」が
    並ぶ聞き方も含める（その相手との決定案件を候補に入れる。追加の照会は 1 本だけ）。
    """
    normalized = _normalize_for_match(query or "")
    if any(_normalize_for_match(kw) in normalized for kw in _DEAL_KEYWORDS):
        return True
    return any(kw in normalized for kw in _DEAL_WITH_NAME_KEYWORDS) and bool(
        deal_query_terms(query)
    )


def deal_query_terms(query: str, *, limit: int = 4) -> list[str]:
    """案件決定の本文から文字で探す固有名（英字の社名・カタカナの社名）を拾う。

    「ADK経由で受注した〜」の ADK のように、意味の近さ（埋め込み）では当たりにくい
    代理店名・社名を、案件決定の投稿の本文で直接探すため。一般語は除く。
    """
    out: list[str] = []
    text = unicodedata.normalize("NFKC", query or "")
    for m in _DEAL_TERM_RE.finditer(text):
        term = m.group(0).strip(".-")
        if len(term) < 2 or term.casefold() in _DEAL_TERM_STOPWORDS:
            continue
        if term.casefold() not in {t.casefold() for t in out}:
            out.append(term)
    for m in _DEAL_KANJI_TERM_RE.finditer(text):
        term = m.group(1)
        if term in _DEAL_KANJI_STOPWORDS or term in out:
            continue
        out.append(term)
    return out[:limit]


def is_campaign_results_intent(
    query: str, *, explicit_doc_type: str | None, has_client: bool
) -> bool:
    """施策実績（案件ごとの投稿本数・再生数・上位投稿）を求める聞き方かを判定する。

    判定順:
      1. 明示の資料種別が「施策実績」以外なら False（明示フィルタを優先する）。
      2. 資料そのものを探す語（提案書・議事録・価格表 等）があれば False。
      3. 実績を聞く語（再生・伸び・結果・施策・ショート動画・TikTok 等）があれば True。
      4. 取引先の指定があり、資料種別の明示が無ければ True（Aico が query を短く書き換えて
         実績の語が消えた場合への備え。候補に足すだけで順位は rerank が決める）。
      5. それ以外は False。

    「食品メーカーのショート動画事例」のような境界は True に倒す（実績も候補に入れて
    rerank に任せる。床は候補を足すだけで、無関係なら上位に出ない）。
    """
    if explicit_doc_type and explicit_doc_type != _CAMPAIGN_DOC_TYPE:
        return False
    if not query:
        return has_client and explicit_doc_type is None
    normalized = _normalize_for_match(query)
    if any(_normalize_for_match(kw) in normalized for kw in _MATERIAL_SEEKING_KEYWORDS):
        return False
    if any(_normalize_for_match(kw) in normalized for kw in _CAMPAIGN_RESULTS_KEYWORDS):
        return True
    return has_client and explicit_doc_type is None


def extract_query_industry(query: str) -> str | None:
    """クエリから業界（cls_industry 正準値）を抽出する。該当語が無ければ None。

    soft な filter_industry として使う（industry=値 OR NULL を許容）＋ 配信側の業界不一致
    スキップにも使う。soft なので過剰除外はしない（未分類・別表記は通る）。

    🔴 **これは業界を名指しする語だけを拾う高速路である。**
    「ヨーグルト」「乳製品」のような商材語はここでは None が返る。
    商材語 → 業界の変換は LLM ルーター（``USE_LLM_ROUTER=true``）の役割で、
    ここに商材語を足して解決しようとしてはいけない（次の商材で同じ事故が起きる）。
    """
    return match_industry_keyword(query)
