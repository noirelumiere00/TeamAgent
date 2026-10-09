"""通常資料と、題・数字を重複させた変異の前検。"""

from __future__ import annotations

import pytest

from teamagent.skills._deck.lint import duplication_problems
from teamagent.skills._deck.spec import DeckBuilder, DeckBuildError, line_fill
from teamagent.skills.search_surface_check.deck import build_surface_deck
from tests.skills.search_surface_check.deck_fixtures import NOW, REPORT_ID, surface


def _builder() -> DeckBuilder:
    deck = build_surface_deck(
        [surface(15)], client_name=None, measured_epoch=NOW, report_id=REPORT_ID
    )
    return DeckBuilder(properties=deck.spec.properties, slides=list(deck.spec.slides))


@pytest.mark.parametrize("kind", ["title", "number"])
def test_duplicate_mutations_stop_builder(kind: str) -> None:
    builder = _builder()
    assert not duplication_problems(builder.slides)
    before = list(builder.slides)
    conclusion = next(s for s in builder.slides if s.slide_id == "SS-04")
    numbers = next(s for s in builder.slides if s.slide_id == "SS-06")
    box = "title" if kind == "title" else "small_number_1"
    copied = next(f for f in numbers.fills if f.box == box)
    addition = (
        copied
        if kind == "title"
        else line_fill("body", "再生の中央値 " + copied.paragraphs[0].runs[0].text, "SS-04｜変異")
    )
    revised = conclusion.model_copy(
        update={"fills": (*(f for f in conclusion.fills if f.box != addition.box), addition)}
    )
    builder.slides[builder.slides.index(conclusion)] = revised
    with pytest.raises(DeckBuildError, match="重複"):
        builder.build()
    builder.slides = before
    assert builder.build()


def test_same_numbers_on_different_surfaces_are_allowed() -> None:
    deck = build_surface_deck(
        [surface(15, keyword=k) for k in ("カレー", "スパイス")],
        client_name=None,
        measured_epoch=NOW,
        report_id=REPORT_ID,
    )
    assert not duplication_problems(deck.spec.slides)


def test_number_changed_without_title_is_rejected() -> None:
    builder = _builder()
    slide = next(s for s in builder.slides if s.slide_id == "SS-06")
    original = list(builder.slides)
    builder.slides[builder.slides.index(slide)] = slide.model_copy(
        update={
            "fills": tuple(
                line_fill("big_number", "99 本", "SS-06｜変異") if f.box == "big_number" else f
                for f in slide.fills
            )
        }
    )
    with pytest.raises(DeckBuildError, match="数字の不一致"):
        builder.build()
    builder.slides = original
    assert builder.build()
