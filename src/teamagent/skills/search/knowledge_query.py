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
