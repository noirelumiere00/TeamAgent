"""BU2 pricing and reach arithmetic shared by both proposal renderers."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BU2_INPUT_FIELDS = frozenset({"plans", "estimate_terms", "simulation", "hearing_items"})
PlanName = Literal["Trial", "A", "B", "C"]
Rate = Annotated[Decimal, Field(ge=0, le=1, max_digits=7, decimal_places=6)]


class BU2Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: PlanName
    count: int | None = Field(default=None, strict=True, ge=1, le=9999)
    views: int | None = Field(default=None, strict=True, ge=1, le=1_000_000_000)
    views_kind: Literal["保証", "想定"] = "想定"
    unit_price_yen: Decimal | None = Field(
        default=None, ge=0, le=1_000_000, max_digits=9, decimal_places=2
    )
    period: str = Field(default="", max_length=20)
    assumption: str = Field(min_length=1, max_length=120, description="料金条件の根拠または仮定")


class BU2EstimateTerms(BaseModel):
    model_config = ConfigDict(extra="forbid")

    net_pricing: bool | None = Field(default=None, strict=True)
    management_fee_rate: Rate | None = None
    production_cost_included: bool | None = Field(default=None, strict=True)


class BU2Simulation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    views: int = Field(strict=True, ge=1, le=1_000_000_000)
    contacts: Decimal = Field(ge=1, le=1000, max_digits=6, decimal_places=2)
    understanding_rate: Rate
    assumption: str = Field(min_length=1, max_length=140, description="接触回数・理解率の根拠/仮定")
    rounding: Literal["floor"] = "floor"


class BU2Content(BaseModel):
    """Optional structured input; empty values keep the original template usable."""

    plans: list[BU2Plan] = Field(default_factory=list, max_length=4)
    estimate_terms: BU2EstimateTerms = Field(default_factory=BU2EstimateTerms)
    simulation: BU2Simulation | None = None
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


def _content(data: Mapping[str, Any] | BU2Content) -> BU2Content:
    if isinstance(data, BU2Content):
        return data
    return BU2Content.model_validate({key: data[key] for key in BU2_INPUT_FIELDS if key in data})


def _missing(label: str) -> str:
    return f"要確認（データ未検出）: {label}"


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
    if simulation is None:
        for field in ("VIEWS", "CONTACTS", "REACH", "RATE", "RESULT", "ASSUMPTION"):
            values[f"PB-BU2-SIM-{field}"] = _missing("試算条件・根拠/仮定")
    else:
        reach = (simulation.views / simulation.contacts).to_integral_value(rounding=ROUND_FLOOR)
        result = (reach * simulation.understanding_rate).to_integral_value(rounding=ROUND_FLOOR)
        values.update(
            {
                "PB-BU2-SIM-VIEWS": f"{simulation.views:,}回（想定）",
                "PB-BU2-SIM-CONTACTS": f"{_number(simulation.contacts)}回接触（仮定）",
                "PB-BU2-SIM-REACH": f"{reach:,}人（推定）",
                "PB-BU2-SIM-RATE": f"{_number(simulation.understanding_rate * 100)}%（仮定）",
                "PB-BU2-SIM-RESULT": f"{result:,}人（試算）",
                "PB-BU2-SIM-ASSUMPTION": f"{simulation.assumption}\n※各段階で人数を切り捨て",
            }
        )
    for index in range(6):
        values[f"PB-BU2-HEARING-{index + 1}"] = (
            f"{index + 1}. {content.hearing_items[index]}"
            if index < len(content.hearing_items)
            else ""
            if len(content.hearing_items) >= 4
            else _missing(f"ヒアリング事項 {index + 1}（4〜6項目）")
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
    if len(content.hearing_items) >= 4:
        notes.update(
            {
                f"PB-BU2-HEARING-{index + 1}": (
                    f"入力{len(content.hearing_items)}件のため{index + 1}番枠未使用"
                )
                for index in range(len(content.hearing_items), 6)
            }
        )
    return notes
