"""BU2 pricing and reach arithmetic shared by both proposal renderers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BU2_INPUT_FIELDS = frozenset({"plans", "estimate_terms", "simulation", "hearing_items"})
BU2_REQUEST_VALUES_ONLY = (
    "利用者が明示した値だけを入れる。出典は依頼文またはその会話で添付・指定した資料に限る。"
    "無ければ省略する（推測・相場・例の数値を入れない）。"
)
PlanName = Literal["Trial", "A", "B", "C"]
Rate = Annotated[Decimal, Field(ge=0, le=1, max_digits=7, decimal_places=6)]
_NUMERIC_TOKEN = re.compile(
    r"(?<![0-9A-Za-z.,万億兆千百十])"
    r"(?P<number>[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)"
    r"(?:\s*(?P<scale>万|億))?(?:\s*(?P<percent>%))?"
    r"(?![0-9A-Za-z.,万億兆千百十])"
)
_UNPARSED_NUMBER = re.compile(r"[0-9〇零一二三四五六七八九十百千万億兆]")


def _request_numbers(text: str) -> set[Decimal]:
    numbers = set()
    normalized = unicodedata.normalize("NFKC", text).replace("−", "-")
    with localcontext() as context:
        # 原文の長い小数を丸めて、別の指定値と一致させない。
        context.prec = len(normalized) + 12
        for match in _NUMERIC_TOKEN.finditer(normalized):
            number = Decimal(match["number"].replace(",", ""))
            number *= {None: 1, "万": 10_000, "億": 100_000_000}[match["scale"]]
            if match["percent"]:
                number /= 100
            numbers.add(number)
    return numbers


def _text_numbers_match(text: str, numbers: set[Decimal]) -> bool:
    normalized = unicodedata.normalize("NFKC", text)
    # 未対応の数表現を「数字なし」として通さない。
    return not _UNPARSED_NUMBER.search(_NUMERIC_TOKEN.sub("", normalized)) and (
        _request_numbers(normalized) <= numbers
    )


class BU2Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: PlanName
    count: int | None = Field(
        default=None, strict=True, ge=1, le=9999, description="本数。" + BU2_REQUEST_VALUES_ONLY
    )
    views: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        le=1_000_000_000,
        description="再生数。" + BU2_REQUEST_VALUES_ONLY,
    )
    views_kind: Literal["保証", "想定"] = Field(
        default="想定", description="保証の明示がある場合のみ保証を選ぶ。"
    )
    unit_price_yen: Decimal | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
        max_digits=9,
        decimal_places=2,
        description="再生単価（円/再生）。" + BU2_REQUEST_VALUES_ONLY,
    )
    period: str = Field(default="", max_length=20, description="期間。" + BU2_REQUEST_VALUES_ONLY)
    assumption: str = Field(
        min_length=1,
        max_length=120,
        description=(
            "利用者が明示した料金条件の出典（依頼文/指定資料）。"
            "仮定と書いて推測値を正当化しない。条件が無ければplansを省略する。"
            + BU2_REQUEST_VALUES_ONLY
        ),
    )


class BU2EstimateTerms(BaseModel):
    model_config = ConfigDict(extra="forbid")

    net_pricing: bool | None = Field(
        default=None, strict=True, description="ネット表記か。" + BU2_REQUEST_VALUES_ONLY
    )
    management_fee_rate: Rate | None = Field(
        default=None, description="管理費率（0〜1）。" + BU2_REQUEST_VALUES_ONLY
    )
    production_cost_included: bool | None = Field(
        default=None, strict=True, description="制作費込か。" + BU2_REQUEST_VALUES_ONLY
    )


class BU2Simulation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    views: int | None = Field(
        strict=True, ge=1, le=1_000_000_000, description="試算の再生数。" + BU2_REQUEST_VALUES_ONLY
    )
    contacts: Decimal | None = Field(
        ge=1,
        le=1000,
        max_digits=6,
        decimal_places=2,
        description="接触回数。社内標準の既定値は無い。" + BU2_REQUEST_VALUES_ONLY,
    )
    understanding_rate: Rate | None = Field(
        description="理解率（0〜1）。社内標準の既定値は無い。" + BU2_REQUEST_VALUES_ONLY
    )
    assumption: str = Field(
        min_length=1,
        max_length=140,
        description=(
            "利用者が指定した接触回数・理解率の出典（依頼文/指定資料）。"
            "仮定と書いて推測値を正当化しない。" + BU2_REQUEST_VALUES_ONLY
        ),
    )
    rounding: Literal["floor"] = Field(default="floor", description="人数は各段階で切り捨て。")


class BU2Content(BaseModel):
    """Optional structured input; empty values keep the original template usable."""

    plans: list[BU2Plan] = Field(
        default_factory=list,
        max_length=4,
        description=(
            "段階見積の本数・再生数・再生単価・期間。"
            "金額は入力された再生数×再生単価で自動計算する。" + BU2_REQUEST_VALUES_ONLY
        ),
    )
    estimate_terms: BU2EstimateTerms = Field(
        default_factory=BU2EstimateTerms, description="見積条件。" + BU2_REQUEST_VALUES_ONLY
    )
    simulation: BU2Simulation | None = Field(
        default=None,
        description=(
            "再生数・接触回数・理解率とその出典を利用者が指定した場合のみ試算する。"
            "一つでも未指定ならsimulation全体を省略する。" + BU2_REQUEST_VALUES_ONLY
        ),
    )
    hearing_items: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=list,
        max_length=6,
        description="次回までに伺いたいこと。未指定または4〜6項目。",
    )

    @field_validator("hearing_items")
    @classmethod
    def _hearing_items_are_bounded(cls, value: list[str]) -> list[str]:
        if value and (len(value) < 4 or any(not text.strip() or "\n" in text for text in value)):
            raise ValueError("hearing_items requires 4-6 nonblank single-line items")
        return [text.strip() for text in value]

    @model_validator(mode="after")
    def _unique_plan_names(self) -> BU2Content:
        if len({plan.name for plan in self.plans}) != len(self.plans):
            raise ValueError("plans must have unique names")
        return self

    def grounded_in_request(self, request_text: str) -> Self:
        """Drop unsupported quantities before any composer or asynchronous render boundary."""
        if not isinstance(request_text, str) or len(request_text) > 4000:
            request_text = ""
        source = unicodedata.normalize("NFKC", request_text)
        numbers = _request_numbers(source)
        result = self.model_copy(deep=True)
        for plan in result.plans:
            if plan.count not in numbers:
                plan.count = None
            if plan.views not in numbers:
                plan.views = None
            if plan.unit_price_yen not in numbers:
                plan.unit_price_yen = None
            period = unicodedata.normalize("NFKC", plan.period)
            if not _text_numbers_match(period, numbers) or (
                not _request_numbers(period) and period not in source
            ):
                plan.period = ""
            if not source or not _text_numbers_match(plan.assumption, numbers):
                plan.assumption = _missing("料金条件の根拠")
        if result.estimate_terms.management_fee_rate not in numbers:
            result.estimate_terms.management_fee_rate = None
        simulation = result.simulation
        if simulation is not None:
            if simulation.views not in numbers:
                simulation.views = None
            if simulation.contacts not in numbers:
                simulation.contacts = None
            if simulation.understanding_rate not in numbers:
                simulation.understanding_rate = None
            if not source or not _text_numbers_match(simulation.assumption, numbers):
                simulation.assumption = _missing("試算条件の根拠/仮定")
        return result


def _content(data: Mapping[str, Any] | BU2Content) -> BU2Content:
    if isinstance(data, BU2Content):
        return data
    return BU2Content.model_validate({key: data[key] for key in BU2_INPUT_FIELDS if key in data})


def _missing(label: str) -> str:
    return f"要確認（依頼に記載が無いため未記入）: {label}"


def _number(value: Decimal) -> str:
    return f"{value:,.6f}".rstrip("0").rstrip(".")


def bu2_auxiliary_values(data: Mapping[str, Any] | BU2Content) -> dict[str, str]:
    """Project only explicit inputs, leaving review markers for missing slots."""
    content = _content(data)
    plans = {plan.name: plan for plan in content.plans}
    values: dict[str, str] = {}
    for name in ("Trial", "A", "B", "C"):
        prefix = f"PB-BU2-PLAN-{name.upper()}-"
        plan = plans.get(name)
        values.update(
            {
                prefix + field: _missing(f"{name} {label}")
                for field, label in (
                    ("COUNT", "本数"),
                    ("VIEWS", "再生数・保証/想定区分"),
                    ("UNIT-PRICE", "再生単価"),
                    ("PERIOD", "期間"),
                    ("AMOUNT", "金額計算の条件"),
                )
            }
        )
        if plan is None:
            continue
        if plan.count is not None:
            values[prefix + "COUNT"] = f"{plan.count:,}本"
        if plan.views is not None:
            values[prefix + "VIEWS"] = f"{plan.views:,}回（{plan.views_kind}）"
        if plan.unit_price_yen is not None:
            values[prefix + "UNIT-PRICE"] = f"{_number(plan.unit_price_yen)}円/再生"
        if plan.period:
            values[prefix + "PERIOD"] = plan.period
        if plan.views is not None and plan.unit_price_yen is not None:
            amount = (plan.views * plan.unit_price_yen).quantize(Decimal(1), rounding=ROUND_HALF_UP)
            values[prefix + "AMOUNT"] = f"{amount:,} 円"
    terms = content.estimate_terms
    values["PB-BU2-ESTIMATE-NOTES"] = "\n".join(
        [
            _missing("ネット/グロス表記")
            if terms.net_pricing is None
            else "※ネット表記"
            if terms.net_pricing
            else "※グロス表記",
            _missing("管理費率")
            if terms.management_fee_rate is None
            else f"※管理費 {_number(terms.management_fee_rate * 100)}% 別途",
            _missing("制作費の含有")
            if terms.production_cost_included is None
            else "※制作費込"
            if terms.production_cost_included
            else "※制作費別",
            "※税・管理費別",
        ]
    )
    simulation = content.simulation
    for field in ("VIEWS", "CONTACTS", "REACH", "RATE", "RESULT", "ASSUMPTION"):
        values[f"PB-BU2-SIM-{field}"] = _missing("試算条件・根拠/仮定")
    if simulation is not None:
        if simulation.views is not None:
            values["PB-BU2-SIM-VIEWS"] = f"{simulation.views:,}回（想定）"
        if simulation.contacts is not None:
            values["PB-BU2-SIM-CONTACTS"] = f"{_number(simulation.contacts)}回接触（仮定）"
        if simulation.understanding_rate is not None:
            values["PB-BU2-SIM-RATE"] = f"{_number(simulation.understanding_rate * 100)}%（仮定）"
        if simulation.views is not None and simulation.contacts is not None:
            reach = (simulation.views / simulation.contacts).to_integral_value(rounding=ROUND_FLOOR)
            values["PB-BU2-SIM-REACH"] = f"{reach:,}人（推定）"
            if simulation.understanding_rate is not None:
                result = (reach * simulation.understanding_rate).to_integral_value(
                    rounding=ROUND_FLOOR
                )
                values["PB-BU2-SIM-RESULT"] = f"{result:,}人（試算）"
        values["PB-BU2-SIM-ASSUMPTION"] = f"{simulation.assumption}\n※各段階で人数を切り捨て"
    for index in range(6):
        values[f"PB-BU2-HEARING-{index + 1}"] = (
            f"{index + 1}. {content.hearing_items[index]}"
            if index < len(content.hearing_items)
            else _missing("ヒアリング事項（4〜6項目）")
            if not content.hearing_items and index == 0
            else ""
        )
    return values


def bu2_source_notes(data: Mapping[str, Any] | BU2Content) -> dict[str, str]:
    """Preserve explicit assumptions and explain intentionally unused hearing slots."""
    content = _content(data)
    notes = {
        f"PB-BU2-PLAN-{plan.name.upper()}-AMOUNT": (
            f"{plan.name} 計算根拠/仮定: {plan.assumption}\n"
            "金額=再生数×再生単価。税・管理費別、円単位四捨五入。"
        )
        for plan in content.plans
    }
    notes.update(
        {
            f"PB-BU2-HEARING-{index + 1}": (
                f"入力{len(content.hearing_items)}件のため{index + 1}番枠未使用"
            )
            for index in range(max(1, len(content.hearing_items)), 6)
        }
    )
    return notes
