"""架空の取引先・投稿・台帳だけで年をまたぐ案件履歴を検証する。"""

import re
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.adapters.slack_user_reader import SlackUserReader
from teamagent.skills.base import SkillContext
from teamagent.skills.search.project_history import (
    CHANNEL,
    merge_projects,
    project_from_hit,
    run_history,
    wants_history,
    year_bounds,
)
from teamagent.skills.search.schema import SearchInput
from teamagent.skills.search.skill import SearchSkill
from teamagent.skills.slack_search.schema import SlackSearchInput
from teamagent.skills.slack_search.skill import SlackSearchSkill
from teamagent.skills.slack_summary.period import JST


def _ts(day: str) -> str:
    return str(datetime.fromisoformat(day).replace(tzinfo=JST).timestamp())


def _body(year: int, *, shooting: bool = True, company: str = "株式会社架空フーズ") -> str:
    return (
        f"*クライアント名*:\n{company}\n案件名: KKシリーズ{year}\n"
        f"種類: 動画制作\n案件ID: FAKE-{year}\n決定日: {year}-01-05\n"
        + (f"撮影日: {year}/2/10\n" if shooting else "撮影日: 未定\n")
    )


def _setup(query: str = "架空フーズの撮影日 2022〜2025") -> tuple[Any, ...]:
    pg = MagicMock()
    pg.connection.return_value.__enter__.return_value = MagicMock()
    pg.list_client_names.return_value = ["架空フーズ"]
    pg.list_project_history.return_value = [
        SearchHit(
            i,
            _body(y, shooting=y != 2024),
            1.0,
            {
                "source_type": "gsheets",
                "topic": "案件決定",
                "source_uri": f"https://docs.google.com/spreadsheets/d/fiction#range=A{i}",
            },
        )
        for i, y in enumerate([2026, 2025, 2024, 2023, 2022], start=1)
    ]
    api = MagicMock()
    rows = [
        {
            "ts": _ts(f"{y}-01-05"),
            "text": _body(y, shooting=y != 2024),
            "user": "UFAKE001",
            "permalink": f"https://fiction.slack.com/archives/{CHANNEL}/p{y}",
            "channel": {"id": CHANNEL, "name": "fiction", "is_private": False, "is_mpim": False},
        }
        for y in [2025, 2024, 2023, 2022]
    ]
    api.search_messages = AsyncMock(
        return_value={
            "ok": True,
            "messages": {"matches": rows, "total": len(rows)},
        }
    )
    reader = SlackUserReader("xoxp-fiction", client=api)
    store = MagicMock()
    store.get.return_value = SimpleNamespace(access_token="xoxp-fiction")
    ctx = SkillContext(
        metadata={
            "user_email": "fiction@example.invalid",
            "channel_id": "DFAKE001",
            "request_text": query,
            "user_groups": ["fiction"],
            "user_role": "member",
        }
    )
    return pg, api, reader, store, ctx


@pytest.mark.parametrize("entry", ["search", "slack_search"])
def test_project_history_cross_year_sorted_deduplicated_and_no_guessed_shooting(entry: str) -> None:
    query = "架空フーズの撮影日 2022〜2025"
    pg, api, reader, store, ctx = _setup(query)
    llm = MagicMock()
    if entry == "search":
        result = SearchSkill(
            bedrock=llm, pgvector=pg, slack_store=store, slack_reader_factory=lambda token: reader
        ).run(SearchInput(query=query), ctx)
        message = result.answer
        assert result.answer_format == "list" and result.found
    else:
        result = SlackSearchSkill(
            store, bedrock=llm, pgvector=pg, reader_factory=lambda token: reader
        ).run(SlackSearchInput(query=query), ctx)
        message = result.message
        assert result.match_count == 4
    assert message.splitlines()[0] == "2022年1月〜2025年12月を見ました・4 件"
    assert [message.count(f"KKシリーズ{y}") for y in range(2022, 2026)] == [1, 1, 1, 1]
    assert "2026" not in message
    assert (
        message.index("KKシリーズ2022")
        < message.index("KKシリーズ2023")
        < message.index("KKシリーズ2024")
        < message.index("KKシリーズ2025")
    )
    assert "2024-01-05・撮影日の記載なし" in message
    assert "2025-02-10（撮影日）" in message and "動画制作" in message
    assert "https://docs.google.com/" in message and "https://fiction.slack.com/" in message
    assert api.search_messages.await_args.kwargs["query"] == f'in:{CHANNEL} "架空フーズ"'
    assert pg.connection.call_args.kwargs == {
        "app_role": "teamagent_app",
        "user_email": "fiction@example.invalid",
        "user_groups": ["fiction"],
        "user_role": "member",
    }
    llm.converse.assert_not_called()


@pytest.mark.parametrize(
    "period,count", [("2023", 1), ("2022年〜2024年", 3), ("過去全部", 5), ("", 5)]
)
def test_project_history_periods(period: str, count: int) -> None:
    query = f"架空フーズ 案件 履歴 {period}"
    pg, _, reader, store, ctx = _setup(query)
    out = run_history(query, ctx, pgvector=pg, slack_store=store, reader_factory=lambda _: reader)
    assert out and not out.error and out.count == count
    if period in ("", "過去全部"):
        assert "全期間（開始年・終了年の制限なし）" in out.message
        assert out.message.startswith("2022年2月〜2026年2月を見ました・5 件")


def test_project_history_uses_shooting_year_not_posting_year_and_no_updated_at_guess() -> None:
    pg, _, _reader, store, ctx = _setup("架空フーズの撮影日 2022")
    pg.list_project_history.return_value = [
        SearchHit(
            1, _body(2022).replace("2022-01-05", "2021-12-25"), 1, {"source_type": "gsheets"}
        ),
        SearchHit(
            2,
            "クライアント名: 架空フーズ\n案件名: 日付未登録",
            1,
            {
                "source_type": "gsheets",
                "updated_at": "2022-03-01",
            },
        ),
        SearchHit(3, _body(2022, company="株式会社架空飲料"), 1, {"source_type": "gsheets"}),
    ]
    store.get.return_value = None
    out = run_history("架空フーズ 撮影日 2022", ctx, pgvector=pg, slack_store=store)
    assert out and out.count == 1 and "KKシリーズ2022" in out.message
    assert "日付の記載がない 1 件" in out.message
    assert "未連携" in out.message and "確認未完了" in out.message


def test_project_history_failures_are_not_empty_successes() -> None:
    pg, api, reader, store, ctx = _setup()
    api.search_messages = AsyncMock(return_value={"ok": False, "error": "ratelimited"})
    out = run_history(
        "架空フーズ 撮影日", ctx, pgvector=pg, slack_store=store, reader_factory=lambda _: reader
    )
    assert out and "確認は未完了" in out.message and out.count == 4
    pg.list_project_history.side_effect = RuntimeError("fiction failure")
    out = run_history("架空フーズ 撮影日", ctx, pgvector=pg)
    assert out and out.error == "search_failed" and "0件という意味ではありません" in out.message


def test_project_history_public_surface_hides_private_ingested_and_live_posts() -> None:
    pg, api, reader, store, ctx = _setup()
    ctx.metadata["channel_id"] = "CFAKE001"
    pg.list_project_history.return_value = [
        SearchHit(
            1,
            _body(2022),
            1,
            {
                "source_type": "slack",
                "source_uri": f"slack://{CHANNEL}/{_ts('2022-01-05')}",
            },
        )
    ]
    rows = api.search_messages.return_value["messages"]["matches"]
    for row in rows:
        row["channel"]["is_private"] = True
    out = run_history(
        "架空フーズ 撮影日", ctx, pgvector=pg, slack_store=store, reader_factory=lambda _: reader
    )
    assert out and out.count == 0 and "KKシリーズ" not in out.message
    assert "DMで確認" in out.message


def test_project_history_slack_more_than_500_is_fully_read() -> None:
    pg, api, reader, store, ctx = _setup("架空フーズ 撮影日 過去全部")
    pg.list_project_history.return_value = []
    rows = [
        {
            "ts": str(float(_ts("2022-01-01")) + i),
            "text": f"クライアント名: 架空フーズ\n案件名: 架空案件{i}\n撮影日: 2022/2/10",
            "channel": {"id": CHANNEL, "name": "fiction", "is_private": False, "is_mpim": False},
        }
        for i in range(503)
    ]

    async def search(**kwargs: Any) -> dict[str, Any]:
        start = (kwargs["page"] - 1) * kwargs["count"]
        return {
            "ok": True,
            "messages": {"matches": rows[start : start + kwargs["count"]], "total": len(rows)},
        }

    api.search_messages = AsyncMock(side_effect=search)
    out = run_history(
        "架空フーズ 撮影日", ctx, pgvector=pg, slack_store=store, reader_factory=lambda _: reader
    )
    assert out and out.count == 503 and "503 件" in out.message
    assert api.search_messages.await_count == 6


def test_project_history_adapter_reads_all_chunks_without_vector_or_recency_limit() -> None:
    conn, cur = MagicMock(), MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = [
        {
            "chunk_id": 1,
            "content": _body(2022),
            "doc_metadata": {"topic": "案件決定"},
            "source_type": "gsheets",
            "source_uri": "https://docs.google.com/spreadsheets/d/fiction",
        }
    ]
    hits = PgVectorClient("postgresql://fiction").list_project_history(conn)
    sql = cur.execute.call_args.args[0]
    assert "LIMIT" not in sql and "modified_at" not in sql and "embedding" not in sql
    assert "string_agg(c.content" in sql and "ORDER BY c.chunk_idx" in sql
    assert CHANNEL in sql and "d.source_type = 'gsheets'" in sql and "topic' = '案件決定'" in sql
    assert hits[0].metadata["topic"] == "案件決定" and "2022" in hits[0].content


@pytest.mark.parametrize(
    "text",
    [
        "架空フーズ 撮影日 2022〜2025",
        "架空フーズ 案件",
        "架空フーズ KKシリーズ 撮影日",
        "架空フーズの案件はいつ？",
        "架空フーズの案件履歴",
        "架空フーズはいつ？",
        "架空フーズ 2022〜2025",
    ],
)
def test_project_history_intent(text: str) -> None:
    assert wants_history(text)


def test_project_history_year_range_validation() -> None:
    assert year_bounds("2022〜2025") == (date(2022, 1, 1), date(2025, 12, 31))
    assert year_bounds("過去全部") is None
    with pytest.raises(ValueError):
        year_bounds("2025〜2022")


def test_project_history_unlabelled_slack_headings_and_empty_shooting_field() -> None:
    body = (
        "*クライアント名／商材名*\n株式会社架空フーズ KKシリーズ\n"
        "*撮影日*\n\n*決定日*\n2022年3月4日\n*種別*\n動画制作"
    )
    p = project_from_hit(SearchHit(1, body, 1, {}), "架空フーズ")
    assert p and p.shooting is None and p.decided == date(2022, 3, 4)
    assert p.name == "株式会社架空フーズ KKシリーズ" and p.kind == "動画制作"


def test_project_history_without_ids_deduplicates_same_case_but_keeps_later_shooting() -> None:
    bodies = [_body(2022), _body(2022), _body(2023).replace("KKシリーズ2023", "KKシリーズ2022")]
    projects = [
        project_from_hit(SearchHit(i, re.sub(r"案件ID:.*\n", "", body), 1, {}), "架空フーズ")
        for i, body in enumerate(bodies)
    ]
    merged = merge_projects([p for p in projects if p])
    assert len(merged) == 2 and [p.day for p in merged] == [date(2022, 2, 10), date(2023, 2, 10)]
