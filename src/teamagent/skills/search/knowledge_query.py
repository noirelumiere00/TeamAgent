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


def extract_knowledge_filters(query: str) -> dict[str, str] | None:
    """クエリから資料種別の絞り込みフィルタを抽出する。

    返り値:
        {"cls_doc_type": "提案書"} のようなフィルタ dict。該当語が無ければ None
        （= 呼び出し側は通常の意味検索にフォールバック）。
    """
    if not query:
        return None
    for keywords, doc_type in _DOC_TYPE_KEYWORDS:
        if any(kw in query for kw in keywords):
            return {"cls_doc_type": doc_type}
    return None
