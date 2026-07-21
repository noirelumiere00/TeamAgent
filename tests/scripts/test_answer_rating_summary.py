"""scripts/answer_rating_summary.py の純関数テスト（実DB不要）。"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PATH = PROJECT_ROOT / "scripts" / "answer_rating_summary.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("answer_rating_summary_under_test", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["answer_rating_summary_under_test"] = module
    spec.loader.exec_module(module)
    return module


summary_script = _load()
NOW = datetime(2026, 7, 21, tzinfo=UTC)


def _row(
    row_id: int,
    *,
    email: str = "alice@example.test",
    session_id: str | None = "session-1",
    answer_id: str | None = "answer-1",
    score: int | None = 4,
    created_at: datetime | None = None,
    query: str = "検索クエリ",
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "user_email": email,
        "search_session_id": session_id,
        "answer_id": answer_id,
        "target_type": "answer",
        "score": score,
        "created_at": created_at or NOW,
        "query": query,
        "note": note,
    }


def test_source_has_no_write_sql_keywords() -> None:
    source = PATH.read_text(encoding="utf-8")
    assert not re.search(r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER)\b", source, flags=re.IGNORECASE)


def test_resolve_keeps_latest_session_row_with_id_tiebreak() -> None:
    rows = [_row(10, score=1), _row(11, score=4)]
    resolved, session_replaced, answer_replaced = summary_script.resolve_ratings(rows)

    assert [row["id"] for row in resolved] == [11]
    assert session_replaced == 1
    assert answer_replaced == 0


def test_null_session_rows_are_not_folded() -> None:
    rows = [
        _row(1, session_id=None, answer_id="answer-1"),
        _row(2, session_id=None, answer_id="answer-2"),
    ]
    resolved, session_replaced, answer_replaced = summary_script.resolve_ratings(rows)

    assert [row["id"] for row in resolved] == [2, 1]
    assert session_replaced == 0
    assert answer_replaced == 0


def test_resolve_folds_same_user_and_answer_after_session_resolution() -> None:
    rows = [
        _row(1, session_id="old-session", created_at=NOW - timedelta(minutes=1)),
        _row(2, session_id="new-session", created_at=NOW),
    ]
    resolved, session_replaced, answer_replaced = summary_script.resolve_ratings(rows)

    assert [row["id"] for row in resolved] == [2]
    assert session_replaced == 0
    assert answer_replaced == 1


def test_score_null_and_non_answer_rows_are_excluded() -> None:
    not_an_answer = _row(2, score=1)
    not_an_answer["target_type"] = "document"
    resolved, _, _ = summary_script.resolve_ratings(
        [_row(1, score=None), not_an_answer, _row(3, score=3)]
    )

    assert [row["id"] for row in resolved] == [3]


def test_render_anonymizes_per_user_by_default_and_can_show_emails() -> None:
    rows = [
        _row(1, email="alice@example.test", note="要確認"),
        _row(2, email="bob@example.test", session_id="session-2", answer_id="answer-2"),
    ]
    summarized = summary_script.summarize_ratings(rows)

    anonymous = summary_script.render_markdown(summarized, 7)
    named = summary_script.render_markdown(summarized, 7, with_emails=True)

    assert "alice@example.test" not in anonymous
    assert "bob@example.test" not in anonymous
    assert "user1" in anonymous
    assert "alice@example.test" in named
    assert "bob@example.test" in named


def test_render_inactivity_judgement_below_and_at_thresholds() -> None:
    below = summary_script.render_markdown(summary_script.summarize_ratings([_row(1)]), 7)
    at_threshold_rows = [
        _row(
            row_id,
            email=f"user{row_id % 5}@example.test",
            session_id=f"session-{row_id}",
            answer_id=f"answer-{row_id}",
        )
        for row_id in range(10)
    ]
    at_threshold = summary_script.render_markdown(
        summary_script.summarize_ratings(at_threshold_rows), 7
    )

    assert "入力者数: 1 / 閾値 5 → 未達" in below
    assert "週間件数: 1 / 閾値 10 → 未達" in below
    assert "入力者数: 5 / 閾値 5 → 満たす" in at_threshold
    assert "週間件数: 10 / 閾値 10 → 満たす" in at_threshold


def test_markdown_cells_escape_html_without_changing_markdown_links() -> None:
    rendered = summary_script._markdown_cell("<img onerror=alert(1)>\r\n[text](url)")

    assert "&lt;img onerror=alert(1)&gt;" in rendered
    assert "\r" not in rendered
    assert "<br>[text](url)" in rendered


def test_inactivity_judgement_normalizes_count_for_non_week_period() -> None:
    rows = [
        _row(
            row_id,
            email=f"user{row_id % 5}@example.test",
            session_id=f"session-{row_id}",
            answer_id=f"answer-{row_id}",
        )
        for row_id in range(20)
    ]

    rendered = summary_script.render_markdown(summary_script.summarize_ratings(rows), 14)

    assert "週間換算件数: 10 (直近 14 日の入力 20 件、--days 14 のため週間閾値は換算)" in rendered
    assert "/ 閾値 10 → 満たす" in rendered
