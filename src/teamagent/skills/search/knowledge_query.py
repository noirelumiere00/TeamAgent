"""ナレッジ Q&A クエリから資料種別フィルタを抽出する（DB 非依存・純ロジック）。

「○○業界の提案事例を教えて」「△△案件の議事録ある？」のような聞き方から、
ingest 自動分類（``teamagent.ingest.classify``）が付与した ``cls_doc_type`` で
絞り込むためのフィルタを取り出す。案件名・業界は既存の client boost /
filter_industry が担うため、ここでは資料種別だけを扱う。

保守的設計: 明確な資料種別の語があるときだけフィルタを返す。無ければ None
（呼び出し側は通常の意味検索にフォールバック）。
"""

from __future__ import annotations

# 資料種別キーワード → cls_doc_type 正規値（classify._DOC_TYPES と一致させる）。
# 具体的・複合語を先に評価する（「提案事例」を「提案書」へ寄せる）。
_DOC_TYPE_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("提案事例", "提案書", "提案資料", "提案の事例", "提案例"), "提案書"),
    (("議事録", "打ち合わせメモ", "打合せメモ", "ミーティングメモ", "MTGメモ"), "議事録"),
    (("報告書", "レポート"), "報告書"),
    (("価格表", "料金表", "価格リスト"), "価格表"),
    (("契約書", "契約条件"), "契約"),
)


# 業界キーワード → cls_industry 正規値（classify._CLASSIFY_SYSTEM_PROMPT の例語彙に寄せる）。
# 「飲料系」「飲食」等を業界フィルタに変換。filter_industry は soft（industry=値 OR NULL）で
# 渡すため、未分類 docs は除外されない＝過剰絞り込みしない安全設計。
_INDUSTRY_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("飲料", "ドリンク", "飲み物"), "飲料"),
    (("飲食", "レストラン", "居酒屋", "カフェ", "外食"), "飲食"),
    (("化粧品", "コスメ", "美容", "スキンケア"), "化粧品"),
    (("日用品", "トイレタリー"), "日用品"),
    (("食品", "食料品"), "食品"),
    (("小売", "EC", "通販", "店舗", "リテール"), "小売"),
    (("不動産", "賃貸", "マンション"), "不動産"),
    (("金融", "銀行", "保険", "証券"), "金融"),
    (("人材", "採用", "求人", "HR"), "人材"),
    (("メーカー", "製造", "工場"), "メーカー"),
    (("IT", "SaaS", "ソフトウェア", "システム"), "IT"),
)


# 商談フェーズキーワード → cls_phase 正規値（classify._PHASES と一致）。過剰絞り込みを
# 避けるため、フェーズが明確な語だけ拾う（doc_type と衝突する「提案」単独は含めない）。
_PHASE_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("受注", "成約"), "受注"),
    (("失注", "見送り", "ロスト"), "失注"),
    (("見積フェーズ", "見積段階"), "見積"),
    (("ヒアリング", "初回接触"), "ヒアリング"),
)


def extract_knowledge_filters(query: str) -> dict[str, str] | None:
    """クエリから資料種別（cls_doc_type）・商談フェーズ（cls_phase）の絞り込みを抽出する。

    返り値:
        {"cls_doc_type": "提案書", "cls_phase": "受注"} のような複合 dict（該当キーのみ）。
        該当語が無ければ None（= 呼び出し側は通常の意味検索にフォールバック）。
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
    return filters or None


def extract_query_industry(query: str) -> str | None:
    """クエリから業界（cls_industry 値）を抽出する。該当語が無ければ None。

    soft な filter_industry として使う（industry=値 OR NULL を許容）＋ 配信側の業界不一致
    スキップにも使う。自動分類が free-text のため exact 一致は保証されないが、soft なので
    過剰除外はしない（未分類・別表記は通る）。
    """
    if not query:
        return None
    for keywords, industry in _INDUSTRY_KEYWORDS:
        if any(kw in query for kw in keywords):
            return industry
    return None
