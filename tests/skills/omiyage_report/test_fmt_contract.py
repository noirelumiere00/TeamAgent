"""input_contract（deck_meta + slide_plan）の検証と誠実性ゲート。"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from teamagent.skills.omiyage_report.fmt.contract import (
    DeckContent,
    FmtContractError,
    validate_deck_content,
)
from teamagent.skills.omiyage_report.fmt.spec import load_fmt_spec

from .fmt_fixtures import (
    BEN1_CONSTRAINT,
    make_card,
    make_deck_content,
    make_png_bytes,
)

SPEC = load_fmt_spec()

# 条件表 → ゴールデン（U1裁定の便1構成）。spec/裁定を差し替えたときはこの表が仕様書になる。
GOLDEN_BEN1_TYPES = ["A", "B", "C", "D", "C", "C", "D", "E", "H"]
GOLDEN_BEN1_Q_NUMBERS = [None, None, "現状", "Q1", "Q2", "Q3", "Q4", "Q5", "総括"]


def test_golden_ben1_composition_is_accepted() -> None:
    content = validate_deck_content(make_deck_content(), SPEC)
    assert isinstance(content, DeckContent)
    assert [slide.type for slide in content.slides] == GOLDEN_BEN1_TYPES
    assert [slide.q_number for slide in content.slides] == GOLDEN_BEN1_Q_NUMBERS


def _mutated(**meta_overrides: Any) -> dict[str, Any]:
    return make_deck_content(**meta_overrides)


def test_ben1_constraint_line_requires_exact_match() -> None:
    raw = _mutated(
        method_target_constraints=[
            "TikTok検索で上位表示データを取得",
            "一般KW 120本",
            BEN1_CONSTRAINT + "。",  # 完全一致でない
        ]
    )
    with pytest.raises(FmtContractError, match="完全一致"):
        validate_deck_content(raw, SPEC)


def test_ben1_constraint_line_must_exist() -> None:
    raw = _mutated(method_target_constraints=["TikTok検索で上位表示データを取得", "一般KW 120本"])
    with pytest.raises(FmtContractError, match="制約行"):
        validate_deck_content(raw, SPEC)


def test_slide_type_g_is_rejected_in_ben1() -> None:
    raw = make_deck_content()
    raw["slides"][3]["type"] = "G"
    with pytest.raises(FmtContractError):
        validate_deck_content(raw, SPEC)


def test_deck_must_end_with_summary() -> None:
    raw = make_deck_content()
    raw["slides"] = raw["slides"][:-1]  # H を落とす
    with pytest.raises(FmtContractError, match="summary"):
        validate_deck_content(raw, SPEC)


def test_empty_part_chapter_is_prohibited() -> None:
    raw = make_deck_content()
    # B の後に C/D/E が1枚も無い構成（A → B → H）
    raw["slides"] = [raw["slides"][0], raw["slides"][1], raw["slides"][-1]]
    with pytest.raises(FmtContractError, match="empty PART chapter"):
        validate_deck_content(raw, SPEC)


def test_e_accepts_partial_cards_but_caps_at_five() -> None:
    # 部分結果裁定（2026-08-24）: 取得できた枚数(1..5)は許容、0枚と6枚以上は拒否
    raw = make_deck_content()
    e_slide = next(slide for slide in raw["slides"] if slide["type"] == "E")
    e_slide["data"]["cards"] = e_slide["data"]["cards"][:4]
    validate_deck_content(raw, SPEC)

    empty = make_deck_content()
    e_empty = next(slide for slide in empty["slides"] if slide["type"] == "E")
    e_empty["data"]["cards"] = []
    with pytest.raises(FmtContractError):
        validate_deck_content(empty, SPEC)

    overfull = make_deck_content()
    e_over = next(slide for slide in overfull["slides"] if slide["type"] == "E")
    e_over["data"]["cards"] = e_over["data"]["cards"] + [e_over["data"]["cards"][0]]
    with pytest.raises(FmtContractError):
        validate_deck_content(overfull, SPEC)


def test_d_row_width_must_match_columns() -> None:
    raw = make_deck_content()
    d_slide = next(slide for slide in raw["slides"] if slide["type"] == "D")
    d_slide["data"]["rows"][0] = d_slide["data"]["rows"][0][:-1]
    with pytest.raises(FmtContractError, match="row width"):
        validate_deck_content(raw, SPEC)


def test_source_url_must_be_https() -> None:
    raw = make_deck_content()
    raw["slides"][0]["data"]["thumbnail_pair"][0]["source_url"] = "http://tiktok.com/@a/video/1"
    with pytest.raises(FmtContractError, match="HTTPS"):
        validate_deck_content(raw, SPEC)


def test_image_kind_is_required_enum() -> None:
    raw = make_deck_content()
    raw["slides"][0]["data"]["thumbnail_pair"][0]["image"]["image_kind"] = "guessed"
    with pytest.raises(FmtContractError):
        validate_deck_content(raw, SPEC)


def test_per_image_budget_is_enforced() -> None:
    raw = make_deck_content()
    per_image_max = SPEC.image_rules.embed_budget.per_image_max_kb * 1024
    oversized = make_png_bytes() + b"\x00" * per_image_max
    raw["slides"][0]["data"]["thumbnail_pair"][0]["image"]["data_uri"] = (
        "data:image/png;base64," + base64.b64encode(oversized).decode("ascii")
    )
    with pytest.raises(FmtContractError, match="per-image budget"):
        validate_deck_content(raw, SPEC)


def test_deck_total_budget_is_enforced() -> None:
    raw = make_deck_content()
    # 1枚あたりは予算内（約100KB）だが9枚合計で 600KB を超える
    padded = make_png_bytes() + b"\x00" * (100 * 1024)
    data_uri = "data:image/png;base64," + base64.b64encode(padded).decode("ascii")
    for slide in raw["slides"]:
        data = slide["data"]
        for key in ("thumbnail_pair", "cards"):
            for card in data.get(key) or []:
                card["image"]["data_uri"] = data_uri
        if data.get("example"):
            data["example"]["image"]["data_uri"] = data_uri
    with pytest.raises(FmtContractError, match="deck budget"):
        validate_deck_content(raw, SPEC)


def test_organic_word_requires_definition_note() -> None:
    raw = make_deck_content()
    d_slide = next(slide for slide in raw["slides"] if slide["type"] == "D")
    d_slide["tag"]["text"] = "オーガニック投稿の反応が高い。"
    with pytest.raises(FmtContractError, match="オーガニック"):
        validate_deck_content(raw, SPEC)

    d_slide["lead"] = "本資料では#PR等の表記が確認できない投稿を指す（広告出稿の有無は未確認）"
    validate_deck_content(raw, SPEC)  # 定義注記があれば通る


def test_heading_required_for_content_slides() -> None:
    raw = make_deck_content()
    raw["slides"][3]["heading"] = ""
    with pytest.raises(FmtContractError, match="heading"):
        validate_deck_content(raw, SPEC)


def test_example_card_is_allowed_for_c_and_d() -> None:
    raw = make_deck_content()
    c_slide = next(slide for slide in raw["slides"] if slide["type"] == "C")
    c_slide["data"]["example"] = make_card("7300000000000000099", caption="実例")
    content = validate_deck_content(raw, SPEC)
    assert any(getattr(slide.data, "example", None) is not None for slide in content.slides)
