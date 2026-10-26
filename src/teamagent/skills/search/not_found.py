"""金庫の検索結果が「該当なし」かを決める判定と、そのときの回答文（純関数）。

背景（2026-10-06・小俣さんの指摘）:
  評価セット 50 問のうち「該当なし」が正解の 7 問（23〜25・47〜50）が、7 問とも何かを
  返していた（0/7）。本番は SEARCH_MIN_RELEVANCE=0.4 ＋ FALLBACK=0.05 なので、0.4 未満の
  ヒットしか無い問いでも 0.05 以上が「低信頼」として救出され、要約器がそれを根拠に
  もっともらしい回答を書く。警告ヘッダ（result_guard）は付くが、本文は「有る」顔になる。

対策は LLM に任せず、retrieval の実数値とヒットのメタだけで「該当なし」をコードで決め、
該当なしのときは要約器を呼ばずに定型文「金庫に該当する資料は見つかりませんでした
（近いもの: …）」を返す（無いものを有るように答えない）。

判定（どれか 1 つで「該当なし」）:
  1. ヒット 0 件。
  2. top1 の関連度が ``score_threshold`` 未満（rerank relevance 0〜1）。
  3. 利用者が既知の取引先を名指ししたのに、**どの**ヒットもその取引先に当たらない
     （本文・題名・取引先メタ・エンティティの広い一致。1 件でも当たれば「有り」）。
  4. 問いの固有名詞・主題語（カタカナ 3 字以上・英字 3 字以上の一般語以外）が
     **どの**ヒットにも出てこない、かつ top1 が ``subject_check_below`` 未満
     （確信の高いヒットは語の表記ゆれで落とさない）。

判定を誤ったときの害が小さい側へ倒す: 3・4 は「1 件でも当たれば有り」で、判定不能は「有り」。

本モジュールは ``os.environ`` を読まない・DB を引かない（env の解決は skill 側）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.ingest.industry_taxonomy import (
    INDUSTRY_KEYWORDS,
    normalize_industry,
)
from teamagent.skills.search.client_match import (
    _hit_matches_client,
    aliases,
    hit_entities,
    names_overlap,
)
from teamagent.util.grapheme_cut import truncate_graphemes

#: 該当なしの回答の書き出し（固定文言・テストと下流の判定がこの文字列を見る）。
NOT_FOUND_HEAD = "金庫に該当する資料は見つかりませんでした"

#: 「近いもの」に挙げる件数と、1 件あたりの表示字数。
_NEAR_MAX = 3
_NEAR_LABEL_CHARS = 40

# 固有名詞・主題語の候補（NFKC 後）。英字は 3 字以上、カタカナは 3 字以上。
_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9&.\-]{2,30}|[ァ-ヴー]{3,20}")

#: 主題語として扱わない一般語（casefold 済み）。ここにある語しか無い問いは 4 の判定をしない。
#: 足すのは「どの資料にも書かれていなくても問いとしては成立する」一般語だけ。
_TERM_STOPWORDS: frozenset[str] = frozenset(
    {
        # 英字（3 字以上のものだけ意味がある）
        "tiktok", "sns", "tvcm", "ugc", "tto", "vseo", "aico", "aila", "instagram", "youtube",
        "line", "btob", "btoc", "b2b", "b2c", "kpi", "kgi", "cpa", "cpm", "cpc", "ctr", "cvr",
        "roas", "roi", "d2c", "bot", "faq", "pdf", "ppt", "pptx", "xlsx", "excel", "word",
        "slack", "drive", "google", "mtg", "newstv", "vector", "url", "web", "seo", "sem",
        "ooh", "csr", "shorts", "reels", "live", "vlog", "the", "and", "for",
        # カタカナ（業務の一般語）
        "ショート", "インフルエンサー", "キャンペーン", "プロモーション", "クライアント",
        "プラン", "コンテンツ", "メディア", "ブランド", "レポート", "メーカー", "テレビ",
        "フィードバック", "ヒアリング", "テンプレート", "テンプレ", "フロー", "パターン",
        "ケース", "マーケティング", "マーケ", "ターゲット", "タイアップ", "アカウント",
        "フォロワー", "エンゲージメント", "リーチ", "インプレッション", "データ", "サービス",
        "プロジェクト", "スケジュール", "スライド", "ファイル", "リスト", "ランキング",
        "トレンド", "ユーザー", "ファン", "コメント", "シェア", "プレゼン", "ミーティング",
        "リクルーティング", "リクルート", "イベント", "セミナー", "サンプル", "パッケージ",
        "ボトル", "ペットボトル",
        "カテゴリ", "カテゴリー", "ジャンル", "チャンネル", "ハッシュタグ", "クリエイター",
        "クリエイティブ", "オリエン", "コンペ", "プランニング", "ソリューション", "ノウハウ",
        "ポイント", "メリット", "デメリット", "コスト", "ボリューム", "ペルソナ", "インサイト",
        "ストーリー", "シリーズ", "タイトル", "サムネ", "サムネイル", "テロップ", "ナレーション",
        "ディレクション", "ディレクター", "プロデューサー", "スタッフ", "チーム", "メンバー",
        "オンライン", "オフライン", "ウェブ", "サイト", "ページ", "アプリ", "ツール", "システム",
        "ルール", "ガイドライン", "レギュレーション", "エビデンス", "ケイパ", "フェーズ",
        "ステータス", "ステップ", "グループ", "ニュース", "リール", "ストーリーズ", "ライブ",
        "ドラマ", "ショートドラマ", "バズる", "ポジティブ", "ネガティブ", "ベクトル",
        "アイラ", "アイコ", "ニュースティービー",
        # 10-06 評価 31（全部リストアップして）で偽の「無い」になった動詞的な一般語
        "リストアップ", "ピックアップ", "アップデート", "アップ",
        "チェック", "レビュー", "サマリー",
    }
)  # fmt: skip


@dataclass(frozen=True)
class FoundDecision:
    """「該当あり／なし」の判定結果。``reason`` はログ用の固定語。

    reason: ``ok`` / ``no_hits`` / ``low_score`` / ``client_mismatch`` / ``subject_mismatch``
    """

    found: bool
    reason: str
    terms: tuple[str, ...] = field(default=())


def query_subject_terms(query: str) -> list[str]:
    """問いの固有名詞・主題語（カタカナ 3 字以上・英字 3 字以上で一般語でないもの）。

    漢字の語は拾わない（一般語と区別できない。「東芝」「半導体」は score 側で判定する）。
    """
    text = unicodedata.normalize("NFKC", query or "")
    out: list[str] = []
    seen: set[str] = set()
    for m in _TERM_RE.finditer(text):
        term = m.group(0).strip(".-&")
        key = term.casefold()
        if len(term) < 3 or key in _TERM_STOPWORDS or key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def case_query_target(query: str, asked_industry: str | None = None) -> str | None:
    """明示された「X の事例」の対象。省略された追問や一般的な実績検索は絞らない。"""
    text = unicodedata.normalize("NFKC", query or "").strip()
    match = re.match(
        r"^(.+?)(?:に関する|についての|関連の|向けの|の)"
        r"(?:(?:ショート動画|動画|SNS|施策|活用|成功|提案)[の・ ]*)*(?:事例|施策実績|実績)",
        text,
        re.IGNORECASE,
    )
    if not match:
        match = re.match(r"^([^の\s]+?)\s*事例", text, re.IGNORECASE)
    if not match:
        # OC は今回の依頼、過去の依頼の順で送る。省略した一覧だけ直近の対象を補う。
        lines = text.splitlines()
        if len(lines) > 1 and re.fullmatch(
            r"(?:一覧|リスト(?:アップ)?|列挙)(?:で|を|に)?"
            r"(?:出して|見せて|して|ください|下さい)?[。！？!?\s]*",
            lines[0],
        ):
            return next(
                (target for line in reversed(lines[1:]) if (target := case_query_target(line))),
                None,
            )
        return None
    target = match[1].strip(" 「」『』\"'")
    # ルーターの業種推定より、利用者が指定した元の対象語を優先する。
    industry_target = re.sub(r"(?:メーカー|業界|業種|企業|会社|業)$", "", target)
    industry = normalize_industry(target) or normalize_industry(industry_target)
    if not industry and not aliases(target):
        industry = next(
            (
                name
                for name, keywords in INDUSTRY_KEYWORDS.items()
                if industry_target.casefold() in {keyword.casefold() for keyword in keywords}
            ),
            None,
        )
    if industry:
        return industry
    if target in {
        "過去",
        "これまで",
        "今回",
        "社内",
        "うち",
        "その",
        "それ",
        "施策",
        "提案",
        "動画",
        "ショート動画",
        "SNS",
        "成功",
    }:
        return None
    target = re.sub(r"(?:の施策|施策|広告|活用|関連)$", "", target).strip()
    return target if len(target) >= 2 else None


def target_matches(text: str, target: str) -> bool:
    """対象語の直接一致（全半角・大小文字・既存の業種/取引先の別名を吸収）。"""
    if "の" in target:
        return all(target_matches(text, part) for part in target.split("の") if part)
    body = unicodedata.normalize("NFKC", text or "").casefold()
    industry = normalize_industry(target)
    forms = {target, *aliases(target), *(INDUSTRY_KEYWORDS.get(industry, ()) if industry else ())}
    for form in forms:
        needle = unicodedata.normalize("NFKC", form).casefold()
        if not needle:
            continue
        if needle.isascii():
            if re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", body):
                return True
        elif needle in body:
            return True
    if re.search(r"[\s()（）]", target):
        from teamagent.skills.search.campaign_files import SIMILARITY_MIN, campaign_similarity

        # 商品説明の省略・括弧差は、既存の施策名照合と同じ基準で吸収する。
        return campaign_similarity(target, text) >= SIMILARITY_MIN
    return False


def case_target_hits(
    hits: Sequence[SearchHit], target: str | None, *, soft_industry: bool = False
) -> list[SearchHit]:
    """事例の対象があるときだけ、同じ対象のヒットへ絞る。"""
    if not target:
        return list(hits)
    if normalize_industry(target):
        from teamagent.skills.search.campaign_files import scope_by_industry

        return scope_by_industry(hits, target, soft=soft_industry)
    return [hit for hit in hits if target_matches(_haystack(hit), target)]


def build_case_not_found_answer(
    target: str, *, unavailable: str = "", scope: str = "社内資料・施策実績・Slack"
) -> str:
    """一致事例 0 件の回答は無関係な事例・探し直しの依頼を付けずに確定する。"""
    scope += f"／{unavailable}" if unavailable else ""
    return f"{target}の事例は見つかりませんでした（探した範囲: {scope}）。"


def build_scoped_not_found_answer(target: str | None, *, scope: str, complete: bool) -> str:
    """検索未完了を不在と区別し、短文に無関係な候補を混ぜない。"""
    if not complete:
        subject = f"{target}の事例の有無" if target else "該当する資料の有無"
        return f"{subject}は検索が完了せず確認できませんでした（探した範囲: {scope}）。"
    if target:
        return build_case_not_found_answer(target, scope=scope)
    return f"{NOT_FOUND_HEAD}（探した範囲: {scope}）。"


def _haystack(hit: SearchHit) -> str:
    meta = getattr(hit, "metadata", None) or {}
    parts: list[str] = [str(getattr(hit, "content", "") or "")]
    for key in ("title", "file_name", "client_name", "cls_project", "channel_name", "project"):
        value = meta.get(key)
        if value:
            parts.append(str(value))
    parts.extend(hit_entities(hit))
    return unicodedata.normalize("NFKC", "\n".join(parts)).casefold()


def _term_in_hit(term: str, hit: SearchHit, haystack: str) -> bool:
    if term.casefold() in haystack:
        return True
    # 「キリンビバレッジ」と問われ、ヒットの取引先が「キリン」のような包含（逆向き）も拾う。
    meta = getattr(hit, "metadata", None) or {}
    names = [str(meta.get(k) or "") for k in ("client_name", "cls_project", "title")]
    names.extend(hit_entities(hit))
    return any(names_overlap(term, name) for name in names if name)


def _subject_mismatch(terms: Sequence[str], hits: Sequence[SearchHit]) -> bool:
    """主題語が 1 つ以上あり、そのどれもがどのヒットにも出てこないか。"""
    if not terms:
        return False
    for hit in hits:
        haystack = _haystack(hit)
        if any(_term_in_hit(term, hit, haystack) for term in terms):
            return False
    return True


def _client_mismatch(asked: str, hits: Sequence[SearchHit], aliases: Sequence[str]) -> bool:
    """名指しの取引先（と別名）に、どのヒットも当たらないか（広い一致・1 件でも当たれば False）。"""
    forms = [asked, *[a for a in aliases if a]]
    for hit in hits:
        if any(_hit_matches_client(hit, form) for form in forms):
            return False
    return True


def _top_score(hits: Sequence[SearchHit]) -> float:
    """主検索ヒットの最大関連度。

    先頭ではなく最大を見る（予算近接・取引先一致の並べ替えで先頭が最大とは限らない）。
    FB の取引先名で足した関連 Drive 資料（is_related_drive・score=1.0 固定）は数えない。
    """
    scores = [
        float(getattr(h, "score", 0.0) or 0.0)
        for h in hits
        if not (getattr(h, "metadata", None) or {}).get("is_related_drive")
    ]
    return max(scores) if scores else 0.0


def judge_found(
    query: str,
    hits: Sequence[SearchHit],
    *,
    score_threshold: float,
    subject_check_below: float,
    query_client: str | None = None,
    client_aliases: Sequence[str] = (),
    expand_subject_aliases: bool = False,
) -> FoundDecision:
    """金庫のヒットが問いに「該当する」と言えるかを決める（判定の詳細はモジュール docstring）。

    Args:
        score_threshold: top1 がこれ未満なら該当なし。0 以下で無効。
        subject_check_below: 主題語の照合は top1 がこれ未満のときだけ行う。0 以下で無効。
        query_client: 利用者が名指しした既知の取引先（明示 filter_client / 語彙の語境界一致）。
            自社名などの除外は呼び出し側で済ませて渡す。None なら 3 の判定をしない。
        client_aliases: ``query_client`` の別名（ブランド↔法人）。
    """
    if not hits:
        return FoundDecision(found=False, reason="no_hits")
    top_score = _top_score(hits)
    if score_threshold > 0.0 and top_score < score_threshold:
        return FoundDecision(found=False, reason="low_score")
    if query_client and _client_mismatch(query_client, hits, client_aliases):
        return FoundDecision(found=False, reason="client_mismatch")
    if subject_check_below > 0.0 and top_score < subject_check_below:
        terms = query_subject_terms(query)
        checked_terms = list(terms)
        if expand_subject_aliases:
            for term in terms:
                checked_terms.extend(sorted(aliases(term)))
        if _subject_mismatch(checked_terms, hits):
            return FoundDecision(found=False, reason="subject_mismatch", terms=tuple(terms))
    return FoundDecision(found=True, reason="ok")


def _near_label(hit: SearchHit) -> str:
    meta = getattr(hit, "metadata", None) or {}
    raw: Any = (
        meta.get("title")
        or meta.get("file_name")
        or (f"#{meta['channel_name']}" if meta.get("channel_name") else None)
        or meta.get("cls_project")
        or meta.get("client_name")
    )
    label = " ".join(str(raw or "").split())
    # 回答文の装飾を壊す記号（『』）は落とす。
    label = label.replace("『", "").replace("』", "")
    if not label:
        return ""
    cut = truncate_graphemes(label, _NEAR_LABEL_CHARS)
    return f"{cut}…" if len(cut) < len(label) else cut


def build_not_found_answer(hits: Sequence[SearchHit], *, max_near: int = _NEAR_MAX) -> str:
    """「金庫に該当する資料は見つかりませんでした（近いもの: 『A』『B』）。」を作る。

    近いものは関連度順に重複を除いて最大 ``max_near`` 件。
    資料名が 1 件も取れなければ括弧を付けない。
    """
    labels: list[str] = []
    for hit in hits:
        label = _near_label(hit)
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= max_near:
            break
    if not labels:
        return f"{NOT_FOUND_HEAD}。"
    near = "".join(f"『{label}』" for label in labels)
    return f"{NOT_FOUND_HEAD}（近いもの: {near}）。"


__all__ = [
    "NOT_FOUND_HEAD",
    "FoundDecision",
    "build_case_not_found_answer",
    "build_not_found_answer",
    "build_scoped_not_found_answer",
    "case_query_target",
    "case_target_hits",
    "judge_found",
    "query_subject_terms",
    "target_matches",
]
