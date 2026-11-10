"""資料の数値を選ぶのはモデル、単位換算・四則演算・回答はコード。"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from teamagent.prompts.loader import load_prompt
from teamagent.skills.search.schema import DocumentReference, SearchHitOut

logger = structlog.get_logger(__name__)
_INTENT = re.compile(
    r"単価|1\s*(?:再生|回|件|人|本|日).*?(?:あたり|当たり|当り)|CP[VM]|"
    # 「当たり前」「行き当たり」「心当たり」を拾わないよう、単位の直後の「あたり」だけ。
    r"(?:[0-9]|再生|回|件|人|本|日|円|表示|月|週|社|名|投稿|動画|フォロワー)\s*(?:あたり|当たり|当り)|"
    r"割ると|割って|割った|合計|総額|差(?:額|分|は|を|が|って|$|[？?。])|計算(?!書)",
    re.IGNORECASE,
)
_NUMBER = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"
_UNIT = r"円|再生|回|件|人|本|日|週間|週|か月|ヶ月|カ月|月|時間|表示|impressions?|imp"
_QUANTITY = re.compile(
    rf"(?<![\d.,\-+])(?P<number>{_NUMBER})\s*(?P<scale>億|万|千)?\s*(?P<unit>{_UNIT})"
    rf"(?:\s*[/／]\s*(?P<per_count>{_NUMBER})?\s*(?P<per_scale>万|千)?\s*"
    rf"(?P<per_unit>{_UNIT}))?",
    re.IGNORECASE,
)
_SCALES = {None: Decimal(1), "千": Decimal(1000), "万": Decimal(10_000), "億": Decimal(100_000_000)}
_MISSING = {
    "cost": "費用",
    "count": "保証視聴回数・対象件数",
    "period": "対象期間",
    "scope": "同じ施策・条件の数字",
    "operands": "計算に必要な数字",
}


def is_calculation(query: str) -> bool:
    text = unicodedata.normalize("NFKC", query)
    if re.search(
        r"(?:単価表|価格表|資料|提案書|ファイル)(?:を|は|って)?(?:探して|送って|添付して|どこ)",
        text,
    ) and not re.search(
        r"計算|割ると|割って|いくら|何円|(?:単価|合計|差)(?:を|は|って)?教えて", text
    ):
        return False
    return bool(_INTENT.search(text))


def _unit(text: str) -> str:
    return {
        "再生": "回",
        "週間": "週",
        "ヶ月": "月",
        "カ月": "月",
        "か月": "月",
        "表示": "表示",
        "imp": "表示",
        "impression": "表示",
        "impressions": "表示",
    }.get(text.lower(), text)


def _number(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    if value and not rounded:
        return format(value, ".4g")
    return format(rounded, ",f").rstrip("0").rstrip(".")


@dataclass(frozen=True)
class Quantity:
    id: str
    raw: str
    value: Decimal
    unit: str
    per_unit: str | None
    per_count: Decimal
    source: SearchHitOut
    is_rate: bool = False


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["divide", "sum", "difference", "stated", "missing"]
    operands: list[str] = Field(default_factory=list, max_length=20)
    stated: str | None = None
    missing: list[Literal["cost", "count", "period", "scope", "operands"]] = Field(
        default_factory=list
    )


def quantities(hits: list[SearchHitOut]) -> dict[str, Quantity]:
    items: dict[str, Quantity] = {}
    seen: set[tuple[int, str]] = set()
    for hit in hits:
        if hit.is_low_confidence or not hit.url or (hit.chunk_id, hit.content) in seen:
            continue
        seen.add((hit.chunk_id, hit.content))
        text = unicodedata.normalize("NFKC", hit.content)
        for match in _QUANTITY.finditer(text):
            before, after = text[: match.start()], text[match.end() :]
            # 範囲・上限・下限を確定値に読み替えない。
            if re.search(r"[\d〜~～−–-]\s*$", before) or re.match(
                r"\s*(?:[〜~～−–-]|以上|以下|未満|超)", after
            ):
                continue
            unit = _unit(match["unit"])
            if unit == "回" and re.search(
                r"(?:表示回数|表示数|インプレッション(?:数)?)\s*[:：]?\s*$", before
            ):
                unit = "表示"
            per_unit = _unit(match["per_unit"]) if match["per_unit"] else None
            per_count = (
                Decimal((match["per_count"] or "1").replace(",", "")) * _SCALES[match["per_scale"]]
            )
            if unit == "円" and not per_unit:
                prefix = re.search(
                    rf"(?P<count>{_NUMBER})\s*(?P<scale>万|千)?\s*(?P<unit>{_UNIT})"
                    r"\s*(?:あたり|当たり|当り)\s*(?:は|が|[:：=])?\s*$",
                    before,
                )
                metric = re.search(r"(CPV|CPM)\s*(?:は|が|[:：=])?\s*$", before, re.IGNORECASE)
                if not metric:
                    metric = re.match(r"\s*[（(]?\s*(CPV|CPM)\b", after, re.IGNORECASE)
                if prefix:
                    per_unit = _unit(prefix["unit"])
                    per_count = Decimal(prefix["count"].replace(",", "")) * _SCALES[prefix["scale"]]
                elif metric:
                    per_unit = "表示" if metric[1].upper() == "CPM" else "回"
                    per_count = Decimal(1000 if metric[1].upper() == "CPM" else 1)
            if per_count <= 0:
                continue
            key = f"n{len(items) + 1}"
            items[key] = Quantity(
                key,
                match[0],
                Decimal(match["number"].replace(",", "")) * _SCALES[match["scale"]],
                unit,
                per_unit,
                per_count,
                hit,
                bool(
                    per_unit
                    or (unit == "円" and re.search(r"単価\s*(?:は|が|[:：=])?\s*$", before))
                ),
            )
    return items


def _missing(keys: Sequence[str]) -> str:
    return "・".join(dict.fromkeys(_MISSING[key] for key in keys or ["operands"])) + (
        "を資料から特定できないため、計算できません。"
    )


def render(query: str, plan: Plan, items: dict[str, Quantity]) -> tuple[str, list[Quantity]]:
    operands = [items[key] for key in plan.operands]
    stated = items[plan.stated] if plan.stated else None
    if len(set(plan.operands)) != len(operands):
        raise ValueError("duplicate_operand")
    if stated and not stated.is_rate:
        raise ValueError("not_a_stated_rate")
    used: list[Quantity] = []
    result: Decimal | None = None
    expression = ""
    unit = ""
    label = "計算結果"
    cpm = bool(re.search(r"CPM", query, re.IGNORECASE))
    cpv = bool(re.search(r"CPV", query, re.IGNORECASE))
    scale = Decimal(1000 if cpm else 1)
    per_unit: str | None = None
    if plan.operation == "divide" and len(operands) == 2:
        first, second = operands
        if first.is_rate or second.is_rate or second.value <= 0:
            raise ValueError("invalid_division")
        if cpm and (first.unit != "円" or second.unit != "表示"):
            raise ValueError("cpm_requires_impressions")
        if cpv and (first.unit != "円" or second.unit != "回"):
            raise ValueError("cpv_requires_views")
        result = first.value / second.value * scale
        per_unit = second.unit
        unit = first.unit
        label = f"{_number(scale)}{('再生' if second.unit == '回' else second.unit)}あたり"
        expression = f"{first.raw} ÷ {second.raw}" + (" × 1,000" if cpm else "")
    elif plan.operation in {"sum", "difference"} and len(operands) >= 2:
        if len({(q.unit, q.is_rate) for q in operands}) != 1 or operands[0].is_rate:
            raise ValueError("incompatible_units")
        if plan.operation == "difference" and len(operands) != 2:
            raise ValueError("difference_requires_two")
        result = (
            sum((q.value for q in operands), Decimal(0))
            if plan.operation == "sum"
            else operands[0].value - operands[1].value
        )
        label = "合計" if plan.operation == "sum" else "差（先に挙げた値から後の値を引いた値）"
        unit = operands[0].unit
        expression = (" + " if plan.operation == "sum" else " − ").join(q.raw for q in operands)
    elif plan.operation not in {"missing", "stated"}:
        raise ValueError("missing_operands")
    lines: list[str] = []
    if stated:
        stated_per = stated.per_unit or per_unit
        if (cpm and stated_per != "表示") or (cpv and stated_per != "回"):
            raise ValueError("incompatible_stated_metric")
        if result is not None and (
            unit != stated.unit or (stated.per_unit and per_unit != stated.per_unit)
        ):
            raise ValueError("incompatible_stated_rate")
        stated_value = stated.value / stated.per_count * scale
        stated_label = (
            f"{_number(scale)}{('再生' if stated_per == '回' else stated_per)}あたり"
            if stated_per
            else ""
        )
        lines.append(f"資料記載の単価は{stated_label}{_number(stated_value)}円です。")
        used.append(stated)
        if result is not None and result != stated_value:
            lines[0] += f"計算値は{label}約{_number(result)}{unit}で、記載値と異なります。"
    elif result is not None:
        lines.append(f"{label}{'は' if per_unit is None else '約'}{_number(result)}{unit}です。")
    if result is not None:
        lines.append(
            f"計算式: {expression} = {_number(result)}{unit}"
            + (f"/{_number(scale)}{per_unit}" if per_unit else "")
        )
        used.extend(operands)
    elif not stated:
        return _missing(plan.missing), []
    for q in used:
        title = q.source.title or q.source.file_name or "資料名不明"
        page = f"p.{q.source.page_num}" if q.source.page_num else "ページ不明"
        lines.append(f"出典（{q.raw}）: {title} {page} {q.source.url}")
    return "\n".join(lines), used


def answer(
    query: str, hits: list[SearchHitOut], bedrock: Any, request_id: str
) -> tuple[str, float, list[DocumentReference]]:
    items = quantities(hits)
    if not items:
        missing = (
            ["cost", "count"] if re.search(r"単価|CP[VM]|あたり", query, re.I) else ["operands"]
        )
        return _missing(missing), 0.0, []
    sources = {q.source.chunk_id: q.source for q in items.values()}
    message = json.dumps(
        {
            "question": query,
            "sources": [
                {"chunk_id": h.chunk_id, "title": h.title or h.file_name, "text": h.content}
                for h in sources.values()
            ],
            "quantities": [
                {
                    "id": q.id,
                    "chunk_id": q.source.chunk_id,
                    "raw": q.raw,
                    "is_rate": q.is_rate,
                    "per_unit": q.per_unit,
                }
                for q in items.values()
            ],
        },
        ensure_ascii=False,
    )
    cost = 0.0
    try:
        response = bedrock.converse(
            messages=[{"role": "user", "content": [{"text": message}]}],
            system=load_prompt("search", "numeric", "system"),
            request_id=request_id,
            max_tokens=800,
            cache_system=True,
        )
        cost = response.usage.cost_usd
        plan = Plan.model_validate_json(response.text)
        text, used = render(query, plan, items)
    except (ValidationError, ValueError, KeyError, DecimalException):
        logger.info("search_numeric_unresolved", request_id=request_id)
        return _missing(["operands", "scope"]), cost, []
    refs = [q.source.reference for q in used if q.source.reference is not None]
    return text, cost, list({ref.url: ref for ref in refs}.values())
