"""古い営業FBを現在の状態に読み替えた本番失敗の回帰テスト（実サービス 0）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills.search import timeline_status as timeline
from tests.skills.search.test_search_dates_exposure import _build, _fake_bedrock


def hit(n: int = 1, **meta: Any) -> SearchHit:
    return SearchHit(
        n,
        "詳細施策の詰めの段階",
        0.9,
        {
            "client_name": "取引先 A",
            "campaign": "500万視聴トライアル",
            "title": "営業FB",
            "is_sales_fb": True,
            "occurred_at": "2026-06-15",
            "deal_phase": "詰めの段階",
            **meta,
        },
    )


def campaign(**meta: Any) -> SearchHit:
    defaults = {
        "is_sales_fb": False,
        "title": "施策実績",
        "campaign_aggregate": "true",
        "video_count": "6",
        "first_posted_on": "2026-08-01",
        "last_posted_on": "2026-08-15",
        "occurred_at": None,
        "deal_phase": None,
    }
    return hit(2, **{**defaults, **meta})


@pytest.fixture(autouse=True)
def flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_TIMELINE_STATUS", "1")


def test_posted_beats_old_fb_and_new_proposal_without_double_counting() -> None:
    rows = [hit(), campaign(), campaign(), hit(3, occurred_at="2026-10-14", cls_phase="提案")]
    assert [s.text for s in timeline.resolve_statuses(rows)] == [
        "実施済み（投稿 6 本・最終投稿 2026-08-15）"
    ]


def test_latest_record_not_rank_or_phase_order() -> None:
    rows = [hit(occurred_at="2026-09-01", deal_phase="終了"), hit(3, deal_phase="受注")]
    for inputs in (rows, list(reversed(rows))):
        assert timeline.resolve_statuses(inputs)[0].text.startswith("2026-09-01 時点では終了")


def test_fb_only_is_historical_and_unknown_latest_is_not_replaced() -> None:
    assert timeline.resolve_statuses([hit()])[0].text == (
        "2026-06-15 時点では詰めの段階（現在の状態は資料からは確認できません）"
    )
    latest = hit(3, occurred_at="2026-10-01", deal_phase="", title="最新メモ")
    assert timeline.resolve_statuses([hit(), latest])[0].text == (
        "状態は資料からは確認できません（最新の記録: 2026-10-01 の最新メモ）"
    )


@pytest.mark.parametrize(
    "meta",
    [
        {"campaign": "別施策"},
        {"client_name": "別取引先"},
        {"campaign": None},
    ],
)
def test_different_or_unknown_campaign_never_inherits_completion(meta: dict[str, Any]) -> None:
    statuses = timeline.resolve_statuses([hit(**meta), campaign()])
    assert len(statuses) == 2
    assert "実施済み" not in statuses[0].text


def test_missing_or_invalid_dates_never_use_ingestion_or_body() -> None:
    unknown = hit(occurred_at=None, updated_at="2026-02-30", ingested_at="2026-10-15")
    assert timeline.date_header(unknown) == ", 日付不明"
    assert "最新の記録: 日付不明" in timeline.resolve_statuses([unknown])[0].text
    assert "最終投稿 日付不明" in timeline.resolve_statuses([campaign(last_posted_on="")])[0].text


def test_jst_boundary_and_typed_headers() -> None:
    assert timeline.day("2026-10-14T15:01:00Z") == "2026-10-15"
    assert timeline.date_header(hit()) == ", 投稿日: 2026-06-15"
    assert timeline.date_header(campaign()) == ", 最終投稿日: 2026-08-15, 投稿開始日: 2026-08-01"
    assert (
        timeline.date_header(hit(is_sales_fb=False, occurred_at=None, updated_at="2026-08-21"))
        == ", 更新日: 2026-08-21"
    )
    assert (
        timeline.date_header(hit(occurred_at=None, created_at="2026-08-20"))
        == ", 作成日: 2026-08-20"
    )
    assert (
        timeline.date_header(hit(occurred_at=None, source_uri="slack://C1/0"))
        == ", 投稿日: 1970-01-01"
    )


def test_summary_input_and_conditional_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    bedrock = _fake_bedrock()
    skill = _build(MagicMock(), bedrock)
    skill._summarize("過去の提案資料と施策実績", [hit(), campaign()], "timeline")
    args = bedrock.converse.call_args.kwargs
    text = args["messages"][0]["content"][0]["text"]
    assert f"今日は {datetime.now(timeline.JST).date()}（JST）" in text
    assert "実施済み（投稿 6 本・最終投稿 2026-08-15）" in text
    assert "投稿日: 2026-06-15" in text and "最終投稿日: 2026-08-15" in text
    assert "YYYY-MM-DD 時点では" in args["system"]
    assert "現在は詰めの段階" in args["system"]  # 禁止する例を含む契約
    skill._summarize("FB", [hit(occurred_at=None)], "timeline")
    assert "日付不明" in bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
    monkeypatch.delenv("SEARCH_TIMELINE_STATUS")
    skill._summarize("FB", [hit()], "timeline")
    baseline = bedrock.converse.call_args.kwargs
    monkeypatch.setenv("SEARCH_TIMELINE_STATUS", "0")
    skill._summarize("FB", [hit()], "timeline")
    assert baseline == bedrock.converse.call_args.kwargs
    assert "今日は" not in baseline["messages"][0]["content"][0]["text"]
    assert load_prompt("search", "timeline", "system") not in baseline["system"]


def test_adapter_projection_reaches_status_and_off_is_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pg = PgVectorClient(dsn="postgresql://stub")
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        {
            "chunk_id": 1,
            "content": "本文",
            "score": 0.9,
            "timeline_metadata": campaign().metadata,
            "timeline_date": None,
        }
    ]
    rows = pg.search_similar_new_schema(conn, [0.1] * 1024)
    assert timeline.resolve_statuses(rows)[0].text == "実施済み（投稿 6 本・最終投稿 2026-08-15）"
    assert "d.metadata AS timeline_metadata" in cur.execute.call_args.args[0]
    monkeypatch.delenv("SEARCH_TIMELINE_STATUS")
    baseline = pg.search_similar_new_schema(conn, [0.1] * 1024)
    sql = cur.execute.call_args.args
    monkeypatch.setenv("SEARCH_TIMELINE_STATUS", "0")
    assert baseline == pg.search_similar_new_schema(conn, [0.1] * 1024)
    assert sql == cur.execute.call_args.args
    assert "timeline_metadata" not in sql[0]


def test_karte_projection_keeps_campaign_identity_and_public_shape() -> None:
    pg = PgVectorClient(dsn="postgresql://stub")
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        {
            "chunk_id": 3,
            "content": "FB",
            "occurred_at": "2026-09-01",
            "timeline_metadata": {"client_name": "取引先 A", "campaign": "500万視聴トライアル"},
            "client_name": "取引先 A",
            "deal_phase": "終了",
            "timeline_date": "2026-09-01",
        }
    ]
    rows = pg.list_client_timeline_recent(conn, "取引先 A")
    assert rows[0].metadata["campaign"] == "500万視聴トライアル"
    assert rows[0].metadata["occurred_at"] == "2026-09-01"
    assert timeline.resolve_statuses([hit(), *rows])[0].text.startswith("2026-09-01 時点では終了")


@pytest.mark.parametrize(
    "metadata",
    [
        {"is_sales_fb": False, "cls_doc_type": "提案書", "cls_phase": "受注"},
        {"deal_phase": "提案", "topic": "案件決定"},
        {"deal_phase": "受注"},
        {"deal_phase": "未受注", "cls_phase": "受注"},
    ],
)
def test_case_state_never_promotes_proposal_or_report_to_official(
    metadata: dict[str, Any],
) -> None:
    status = timeline.resolve_statuses([hit(**metadata)], case_state=True)[0]
    assert status.evidence is not None
    assert status.evidence.state == "unknown"
    assert status.evidence.resolution == "unknown"
    assert "正式状態は不明" in status.text
    assert "実施済み" not in status.text


def test_case_state_posts_do_not_complete_or_merge_a_new_proposal() -> None:
    statuses = timeline.resolve_statuses([campaign(), hit(3, deal_phase="提案")], case_state=True)
    assert len(statuses) == 2
    assert all(s.evidence is not None and s.evidence.state == "unknown" for s in statuses)
    assert all("実施済み" not in s.text for s in statuses)
