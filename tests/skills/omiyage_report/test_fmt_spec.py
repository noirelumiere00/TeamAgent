"""FMT deck spec（omiyage_fmt_v1）の読み込み・スキーマ検証。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from teamagent.skills.omiyage_report.fmt.spec import (
    FmtDeckSpec,
    FmtSpecError,
    fmt_spec_path,
    load_fmt_spec,
)

from .fmt_fixtures import BEN1_CONSTRAINT, CTA


def _raw_spec() -> dict[str, Any]:
    return json.loads(fmt_spec_path().read_text(encoding="utf-8"))


def test_bundled_spec_loads_and_pins_fixed_texts() -> None:
    spec = load_fmt_spec()
    assert spec.meta.name == "fmt_tiktok_search_deck"
    assert (spec.meta.canvas.width_px, spec.meta.canvas.height_px) == (1920, 1080)
    assert spec.min_font_px == 24
    # CTA はローカルSkill正本の固定文言と完全一致（開示ゲートの根拠）
    assert spec.cta_text == CTA
    # 便1制約行は 2026-08-24 計測経路裁定（telop計測済み）が spec JSON の旧文言を
    # 上書きする: ゲートの正は共用定数（BEN1_CONSTRAINT = VOICE_UNMEASURED_NOTE）で、
    # spec 側の旧文言と食い違うこと自体が仕様（差し替え時に spec を更新したら消える）
    assert spec.ben1_required_text != BEN1_CONSTRAINT
    assert "テロップ" in spec.ben1_required_text  # 旧文言（telop未計測時代）のまま
    assert spec.color("paper") == "#F6F4EF"
    assert spec.tokens.colors.brand_accents.brand_a.value == "#A24765"
    assert "provided_thumbnail" in spec.image_rules.image_kind.enum
    assert spec.image_rules.embed_budget.per_image_max_kb == 120
    assert spec.image_rules.embed_budget.deck_total_max_kb == 600


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda raw: raw["tokens"]["colors"].pop("paper"), "paper"),
        (lambda raw: raw["meta"]["canvas"].__setitem__("min_font_size_px", 20), "min_font"),
        (
            lambda raw: raw["data_requirements"]["ben1_deck_composition"].__setitem__(
                "slide_types_used", ["A", "B", "G"]
            ),
            "A/B/C/D/E/H",
        ),
        (lambda raw: raw["slide_types"]["H"]["cta"].__setitem__("text", ""), "cta"),
        (
            lambda raw: raw["image_rules"]["fit_policy"].__setitem__("mode", "cover"),
            "contain",
        ),
        (
            lambda raw: raw["image_rules"]["source_url"].__setitem__("required", False),
            "source_url",
        ),
    ],
)
def test_spec_schema_rejects_broken_specs(mutator: Any, match: str) -> None:
    raw = _raw_spec()
    mutator(raw)
    with pytest.raises((ValueError, KeyError), match=match):
        FmtDeckSpec.model_validate(raw)


def test_spec_name_is_sanitized() -> None:
    with pytest.raises(FmtSpecError):
        load_fmt_spec("../evil")


def test_repo_root_spec_copy_stays_in_sync() -> None:
    """裁定の複製先（リポ直下 assets/）とパッケージ同梱コピーのドリフト禁止。"""
    packaged = fmt_spec_path()
    repo_root_copy = packaged.parents[4] / "assets" / "deck_specs" / packaged.name
    if not repo_root_copy.is_file():
        pytest.skip("repo-root assets copy not present")
    assert repo_root_copy.read_bytes() == packaged.read_bytes()
