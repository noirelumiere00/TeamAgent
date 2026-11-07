"""Synthetic analytics only: parsing, safe logs and nightly sync orchestration."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from teamagent.adapters.gsheets_client import GSheetsClient, TabRows
from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest import video_library_insights_sync as sync
from teamagent.ingest.video_library_url_repository import JST


def row(**overrides: str) -> dict[str, str]:
    values = dict.fromkeys(sync.REQUIRED_HEADERS, "")
    values.update(
        {
            "データ取得日時": "2026/06/11 19:00:00",
            "アカウント": "合成アカウント",
            "URL": "https://www.tiktok.com/@SAMPLE/video/123?test=1",
            "投稿日": "2026-06-05",
            "タイトル": "合成投稿 #PR #テスト",
            "動画尺": "0:31",
            "視聴数": "1,234",
            "いいね数": "23",
            "コメント数": "4",
            "シェア数": "5",
            "保存数": "6",
            "リーチ": "1,000",
            "完了率": "6.20%",
            "平均視聴時間(秒)": "7.5",
            "プロフィール閲覧回数": "10",
            "男性%": "29.24%",
            "女性%": "70.76%",
            "年齢1位": "25-34: 25.8%",
            "年齢2位": "18-24: 24.2%",
            "年齢3位": "55+: 20%",
            "0秒": "1234",
            "1秒": "1000",
            "3秒": "800",
        }
    )
    values.update(overrides)
    return values


def sheet(*rows: dict[str, str], headers: tuple[str, ...] = sync.REQUIRED_HEADERS) -> TabRows:
    return TabRows(
        sheet_id="synthetic",
        tab_name="シート1",
        headers=headers,
        rows=tuple(tuple(r.get(h, "") for h in headers) for r in rows),
        row_count=len(rows),
    )


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_LIBRARY_INSIGHTS_ENABLED", "1")
    monkeypatch.delenv("VIDEO_LIBRARY_ANALYTICS_SHEET_ID", raising=False)


@pytest.mark.parametrize("captured", ["2026/06/11 19:00:00", "2026/06/11 19:00"])
def test_parse_jst_percent_ages_and_retention_stops_at_first_gap(captured: str) -> None:
    parsed = sync.parse_row(row(データ取得日時=captured))
    assert parsed.captured_at == datetime(2026, 6, 11, 19, tzinfo=JST)
    assert parsed.public_metrics == [1234, 23, 4, 5, 6]
    assert parsed.reach == 1000 and parsed.profile_views == 10
    assert parsed.completion_rate == pytest.approx(0.062)
    assert parsed.male_ratio == pytest.approx(0.2924)
    assert parsed.female_ratio == pytest.approx(0.7076)
    assert parsed.avg_watch_seconds == 7.5
    assert [a.band for a in parsed.age_bands] == ["25-34", "18-24", "55+"]
    assert [a.ratio for a in parsed.age_bands] == pytest.approx([0.258, 0.242, 0.2])
    assert parsed.retention == [1234, 1000]
    video = parsed.video
    assert video.account_handle == "sample"
    assert video.url == "https://www.tiktok.com/@sample/video/123"
    assert video.posted_at == datetime(2026, 6, 5, tzinfo=JST)
    assert video.duration_seconds == 31 and video.origin == "own"
    assert video.caption == "合成投稿 #PR #テスト"
    assert video.is_pr and video.hashtags == ["PR", "テスト"]


@pytest.mark.parametrize("posted", ["6/16", "2026-02-30", "", "garbage"])
def test_broken_post_date_is_null(posted: str) -> None:
    assert sync.parse_row(row(投稿日=posted)).video.posted_at is None


@pytest.mark.parametrize(
    "duration,expected", [("", None), ("bad", None), ("0:60", None), ("2:03", 123)]
)
def test_duration(duration: str, expected: int | None) -> None:
    assert sync.parse_row(row(動画尺=duration)).video.duration_seconds == expected


@pytest.mark.parametrize("value", ["-1%", "101%", "NaN", "inf", "bad"])
def test_invalid_numbers_never_reach_database(value: str) -> None:
    parsed = sync.parse_row(row(完了率=value, リーチ=value))
    assert parsed.completion_rate is None and parsed.reach is None


def test_zero_metrics_empty_optional_values_and_nfkc_pr() -> None:
    parsed = sync.parse_row(
        row(**dict.fromkeys(sync.PRIVATE_COLUMNS, ""), タイトル="合成 #ＰＲ", 視聴数="0")
    )
    assert not parsed.has_insights and parsed.retention == [] and parsed.age_bands == []
    assert parsed.public_metrics[0] == 0 and parsed.video.is_pr
    assert sync._count("2147483648") is None
    assert sync._number("0.5", ratio=True) == 0.5


def test_run_maps_reordered_headers_and_counts_skips_without_raw_logs(enabled: None) -> None:
    pg = MagicMock(spec=PgVectorClient)
    conn = pg.connection.return_value.__enter__.return_value
    conn.execute.return_value.rowcount = 1
    conn.execute.return_value.fetchall.side_effect = [[{"handle": "sample"}], [{"video_id": "123"}]]
    sheets = MagicMock(spec=GSheetsClient)
    sheets.get_tab_rows.return_value = sheet(
        row(),
        row(URL="https://www.tiktok.com/@sample/video/456"),
        row(URL="https://www.tiktok.com/@foreign/video/789"),
        row(URL="A1:F12"),
        row(URL=""),
        row(データ取得日時="bad private input"),
        row(**dict.fromkeys(sync.PRIVATE_COLUMNS, "")),
        headers=tuple(reversed(sync.REQUIRED_HEADERS)),
    )
    with capture_logs() as logs:
        result = sync.run(pg, sheets, request_id="synthetic-sync")
    assert result.rows == 7 and result.videos == 2
    assert result.inserted_videos == 1 and result.insights == 2 and result.public_metrics == 3
    assert result.skipped == {
        "unregistered_external_handle": 1,
        "invalid_url": 2,
        "invalid_captured_at": 1,
        "no_insights": 1,
    }
    pg.connection.assert_called_once_with(
        app_role="teamagent_app", user_email=None, user_role="admin"
    )
    sheets.get_tab_rows.assert_called_once_with(sync.DEFAULT_SHEET_ID, "シート1", "synthetic-sync")
    calls = conn.execute.call_args_list
    for sql_call in calls:
        if len(sql_call.args) == 2:
            assert sql_call.args[0].count("%s") == len(sql_call.args[1])
    inserts = [c for c in calls if "INSERT INTO vl_videos" in c.args[0]]
    assert len(inserts) == 1 and inserts[0].args[1][0] == "456"
    insight = next(c for c in calls if "INSERT INTO vl_video_insights" in c.args[0])
    assert insight.args[1][2:7] == (1234, 23, 4, 5, 6)
    assert insight.args[1][-1] == [1234, 1000]
    public = next(c for c in calls if "INSERT INTO vl_public_metrics" in c.args[0])
    assert public.args[1][1] == datetime(2026, 6, 11, tzinfo=JST).date()
    assert public.args[1][-1].obj == {"source": "analytics_sheet"}
    summary = logs[-1]
    assert summary["token_usage"] == summary["cost_usd"] == 0
    assert summary["latency_ms"] >= 0 and summary["request_id"] == "synthetic-sync"
    for private in ["合成投稿", "https://", "1234", "bad private input", "A1:F12"]:
        assert private not in str(logs)


@pytest.mark.parametrize(
    "headers", [sync.REQUIRED_HEADERS[:-1], (*sync.REQUIRED_HEADERS, "URL"), ()]
)
def test_missing_or_duplicate_headers_fail_before_db(
    enabled: None, headers: tuple[str, ...]
) -> None:
    pg, sheets = MagicMock(spec=PgVectorClient), MagicMock(spec=GSheetsClient)
    sheets.get_tab_rows.return_value = sheet(headers=headers)
    with capture_logs() as logs, pytest.raises(ValueError, match="analytics_columns_missing"):
        sync.run(pg, sheets, request_id="missing-headers")
    pg.connection.assert_not_called()
    assert logs[-1]["event"] == "video_library_insights_columns_missing"


@pytest.mark.parametrize("reason", ["disabled", "sheet_unconfigured"])
def test_disabled_or_empty_sheet_skip_without_clients(
    monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    monkeypatch.setenv("VIDEO_LIBRARY_INSIGHTS_ENABLED", "0" if reason == "disabled" else "1")
    monkeypatch.setenv(
        "VIDEO_LIBRARY_ANALYTICS_SHEET_ID", "" if reason == "sheet_unconfigured" else "id"
    )
    pg, sheets = MagicMock(spec=PgVectorClient), MagicMock(spec=GSheetsClient)
    assert sync.run(pg, sheets, request_id="disabled").reason == reason
    pg.connection.assert_not_called()
    sheets.get_tab_rows.assert_not_called()
    monkeypatch.setattr(sync, "configure_logging", lambda: None)
    factory = MagicMock(side_effect=AssertionError("must not build clients"))
    monkeypatch.setattr(sync.PgVectorClient, "from_env", factory)
    monkeypatch.setattr(sync.GSheetsClient, "from_env", factory)
    assert sync.main([]) == 0
    factory.assert_not_called()


@pytest.mark.parametrize("outcome", ["success", "headers", "read_failure", "db_failure"])
def test_main_closes_pg_and_returns_sanitized_failure(
    enabled: None, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    monkeypatch.setattr(sync, "configure_logging", lambda: None)
    monkeypatch.setattr(sync, "expand_google_oauth_json", lambda: None)
    pg, sheets = MagicMock(spec=PgVectorClient), MagicMock(spec=GSheetsClient)
    monkeypatch.setattr(sync.PgVectorClient, "from_env", lambda: pg)
    monkeypatch.setattr(sync.GSheetsClient, "from_env", lambda: sheets)
    sheets.get_tab_rows.return_value = sheet() if outcome != "headers" else sheet(headers=())
    if outcome == "read_failure":
        sheets.get_tab_rows.side_effect = RuntimeError("private caption URL numbers")
    if outcome == "db_failure":
        pg.connection.side_effect = RuntimeError("private caption URL numbers")
    with capture_logs() as logs:
        assert sync.main([]) == (0 if outcome == "success" else 1)
    pg.close.assert_called_once()
    assert "private caption URL numbers" not in str(logs)
    if outcome != "success":
        assert logs[-1]["event"] == "video_library_insights_failed"


def test_blank_trailing_headers_like_the_real_sheet_are_accepted(enabled: None) -> None:
    # The real sheet has ~70 empty header cells right of 47秒; they must not count as duplicates.
    pg, sheets = MagicMock(spec=PgVectorClient), MagicMock(spec=GSheetsClient)
    sheets.get_tab_rows.return_value = sheet(headers=(*sync.REQUIRED_HEADERS, "", "", ""))
    assert sync.run(pg, sheets, request_id="blank-headers").rows == 0
