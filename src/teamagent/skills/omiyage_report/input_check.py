"""お土産資料の「品質の門」（段 1・2026-09-25）: 入力と検索結果の点検。

9/17 の GABAN 版で起きたことを、資料を作る前に止める:

- 決定論（LLM なし・preflight から呼ぶ）
  - 一般キーワードにブランド名・競合名が入っている（「GABAN レシピ」は指名検索）
  - 対象ブランド自身を競合に入れている
- Bedrock（Haiku・1 回ずつ）
  - 競合の妥当性: 同じカテゴリの商品ブランドか。親会社・グループ会社、飲食店・小売、
    カテゴリ違いは外す（「House 食品」「カレーハウス CoCo 壱番屋」）
  - 関連性: 検索結果の動画が商材と関係あるか。同名の別作品（アニメのキャラクター等）や
    無関係な料理・日常動画を集計から外す（「GABAN」検索のワンピース動画）

LLM の点検はどちらも、失敗したら何も外さない（fail-open）。外した数と、点検できなかった
ことは監査記録と資料に書く。判定理由は固定の語彙だけを受け付け、LLM の自由文は使わない。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from teamagent.skills.omiyage_report.metrics import (
    PostRecord,
    contains_term,
    keyword_variants,
)
from teamagent.skills.omiyage_report.schema import MissingField

LLM_CHECKS_ENV: Final = "OMIYAGE_LLM_CHECKS"

# --- 決定論の点検 ----------------------------------------------------------------------


@dataclass(frozen=True)
class InputProblem:
    """見直してほしい入力 1 件（needs_input で営業に返す）。"""

    field: MissingField
    value: str
    reason: str


KEYWORD_HAS_BRAND: Final = (
    "ブランド名を含む語は指名検索になり、一般キーワードとして比べられません"
    "（例: 「GABAN レシピ」ではなく「スパイスカレー 作り方」）"
)
KEYWORD_HAS_COMPETITOR: Final = "競合名を含む語は指名検索になり、一般キーワードとして比べられません"
COMPETITOR_IS_BRAND: Final = "対象ブランド自身は競合にできません"


def find_input_problems(
    brand: str, competitors: Sequence[str], keywords: Sequence[str]
) -> list[InputProblem]:
    problems: list[InputProblem] = []
    # 1 文字の名前は部分一致が当たりすぎるので見ない（「B」は「競合B」の一部ではない）
    brand_variants = tuple(v for v in keyword_variants([brand]) if len(v.normalized) >= 2)
    competitor_variants = tuple(
        v for v in keyword_variants(list(competitors)) if len(v.normalized) >= 2
    )
    for keyword in keywords:
        if brand_variants and contains_term(keyword, brand_variants):
            problems.append(InputProblem("keywords", keyword, KEYWORD_HAS_BRAND))
        elif competitor_variants and contains_term(keyword, competitor_variants):
            problems.append(InputProblem("keywords", keyword, KEYWORD_HAS_COMPETITOR))
    for competitor in competitors:
        own_variants = tuple(v for v in keyword_variants([competitor]) if len(v.normalized) >= 2)
        if brand_variants and (
            contains_term(competitor, brand_variants)
            or (own_variants and contains_term(brand, own_variants))
        ):
            problems.append(InputProblem("competitors", competitor, COMPETITOR_IS_BRAND))
    return problems


# --- LLM の点検（共通） -------------------------------------------------------------------


def llm_checks_enabled() -> bool:
    """本番（BEDROCK_MODEL_ID がある環境）でだけ既定で動く。OMIYAGE_LLM_CHECKS=0 で止める。"""
    flag = os.environ.get(LLM_CHECKS_ENV, "").strip().lower()
    if flag in ("0", "false", "no"):
        return False
    if flag in ("1", "true", "yes"):
        return True
    return bool(os.environ.get("BEDROCK_MODEL_ID", "").strip())


_JSON_OBJECT_RE: Final = re.compile(r"\{.*\}", re.S)


def _parse_json_object(text: str) -> dict[str, object] | None:
    match = _JSON_OBJECT_RE.search(text or "")
    if match is None:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


TextCaller = Callable[[str, str], str]  # (prompt, request_id) -> 応答本文


def bedrock_text_caller(prompt: str, request_id: str) -> str:
    from teamagent.adapters.bedrock_client import BedrockClient

    client = BedrockClient.from_env(
        model_id_override=os.environ.get("OMIYAGE_CHECK_BEDROCK_MODEL_ID") or None
    )
    response = client.converse(
        [{"role": "user", "content": [{"text": prompt}]}],
        request_id,
        temperature=0.0,
        max_tokens=2048,
    )
    return response.text


# --- 競合の妥当性 ------------------------------------------------------------------------

COMPETITOR_REASONS: Final[dict[str, str]] = {
    "parent_or_group": "対象ブランドの親会社・グループ会社で、競合ではありません",
    "restaurant_or_retail": "飲食店・小売店で、商品ブランドの競合ではありません",
    "different_category": "商材のカテゴリが違い、競合として比べられません",
    "same_brand": COMPETITOR_IS_BRAND,
}

_COMPETITOR_PROMPT: Final = """あなたは広告会社のリサーチ担当です。
TikTok 上の露出を比べる資料を作るため、
「競合ブランド」の候補が本当に競合かを判定してください。

対象ブランド: {brand}
商材カテゴリ: {category}
競合候補: {competitors}

各候補について次のどれかを選ぶ:
- ok: 対象ブランドと同じカテゴリの商品ブランド（またはそのメーカー）で、店頭や検索で比べられる相手
- parent_or_group: 対象ブランドの親会社・子会社・同じグループ
  （例: ブランドを持っている会社そのもの）
- restaurant_or_retail: 飲食店・チェーン店・小売店など、商品ブランドではないもの
- different_category: 商材のカテゴリが明らかに違うもの
- same_brand: 対象ブランドそのもの

迷ったら ok にする。JSON だけで答える:
{{"verdicts": [{{"name": "<候補名そのまま>", "verdict": "<上の語のどれか>"}}]}}"""


@dataclass(frozen=True)
class CompetitorCheck:
    invalid: dict[str, str]  # 候補名 -> 理由コード（COMPETITOR_REASONS のキー）
    checked: bool  # False = 点検できなかった（fail-open で全員そのまま）


def check_competitors(
    brand: str,
    competitors: Sequence[str],
    category: str,
    request_id: str,
    *,
    caller: TextCaller = bedrock_text_caller,
) -> CompetitorCheck:
    if not competitors:
        return CompetitorCheck(invalid={}, checked=True)
    prompt = _COMPETITOR_PROMPT.format(
        brand=brand,
        category=category or "（未指定。ブランドから推定する）",
        competitors=" / ".join(competitors),
    )
    try:
        parsed = _parse_json_object(caller(prompt, request_id))
    except Exception:
        return CompetitorCheck(invalid={}, checked=False)
    verdicts = parsed.get("verdicts") if parsed else None
    if not isinstance(verdicts, list):
        return CompetitorCheck(invalid={}, checked=False)
    names = set(competitors)
    invalid: dict[str, str] = {}
    for item in verdicts:
        if not isinstance(item, dict):
            continue
        name, verdict = item.get("name"), item.get("verdict")
        # 入力に無い名前・語彙外の判定は捨てる（LLM の自由文を資料や返答に出さない）
        if isinstance(name, str) and name in names and verdict in COMPETITOR_REASONS:
            invalid[name] = str(verdict)
    return CompetitorCheck(invalid=invalid, checked=True)


# --- 関連性 -------------------------------------------------------------------------------

_RELEVANCE_PROMPT: Final = """あなたは広告会社のリサーチ担当です。
TikTok で「{query}」を検索した上位の動画が、
調べたい商材と関係あるかを判定してください。

調べたい商材: {brand}（カテゴリ: {category}）
競合: {competitors}
一般キーワード: {keywords}

次のような動画は unrelated（集計から外す）:
- ブランド名と同じ名前の別物（アニメ・ゲームのキャラクター、人名、地名など）の動画
- 商材のカテゴリと関係の無い内容（別ジャンルの料理・日常・ダンスなど）
商材・カテゴリ・競合のどれかに関係していれば related。迷ったら related にする。

動画（id / 説明文 / ハッシュタグ / 投稿者）:
{posts}

JSON だけで答える: {{"unrelated": ["<id>", ...]}}"""

_MAX_DESC: Final = 160


@dataclass(frozen=True)
class RelevanceCheck:
    excluded_ids: frozenset[str]
    checked: bool


def _post_line(post: PostRecord) -> str:
    desc = " ".join((post.caption or "").split())[:_MAX_DESC]
    tags = " ".join(f"#{t}" for t in post.hashtags[:8])
    return f"{post.video_id} / {desc} / {tags} / {post.nickname or post.author}"


def check_relevance(
    *,
    query: str,
    brand: str,
    category: str,
    competitors: Sequence[str],
    keywords: Sequence[str],
    posts: Sequence[PostRecord],
    request_id: str,
    caller: TextCaller = bedrock_text_caller,
) -> RelevanceCheck:
    if not posts:
        return RelevanceCheck(excluded_ids=frozenset(), checked=True)
    prompt = _RELEVANCE_PROMPT.format(
        query=query,
        brand=brand,
        category=category or "（未指定。ブランドとキーワードから推定する）",
        competitors=" / ".join(competitors) or "（なし）",
        keywords=" / ".join(keywords) or "（なし）",
        posts="\n".join(_post_line(post) for post in posts),
    )
    try:
        parsed = _parse_json_object(caller(prompt, request_id))
    except Exception:
        return RelevanceCheck(excluded_ids=frozenset(), checked=False)
    unrelated = parsed.get("unrelated") if parsed else None
    if not isinstance(unrelated, list):
        return RelevanceCheck(excluded_ids=frozenset(), checked=False)
    known = {post.video_id for post in posts}
    return RelevanceCheck(
        excluded_ids=frozenset(str(i) for i in unrelated if str(i) in known), checked=True
    )


__all__ = [
    "COMPETITOR_IS_BRAND",
    "COMPETITOR_REASONS",
    "KEYWORD_HAS_BRAND",
    "KEYWORD_HAS_COMPETITOR",
    "CompetitorCheck",
    "InputProblem",
    "RelevanceCheck",
    "bedrock_text_caller",
    "check_competitors",
    "check_relevance",
    "find_input_problems",
    "llm_checks_enabled",
]
