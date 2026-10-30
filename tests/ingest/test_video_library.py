"""P1 sheet failure modes, business rules and standalone CLI (no external I/O)."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import structlog
import structlog.testing

from teamagent.adapters.gsheets_client import SheetMetadata, SheetTab, TabRows
from teamagent.ingest import campaign_aggregate as ca
from teamagent.ingest import video_library_sync as sync
from teamagent.ingest.video_library import (
    METRIC_COLUMNS,
    Account,
    Campaign,
    Metrics,
    SyncBatch,
    Video,
    build_batch,
    derived_rates,
    resolve_business_category,
    video_identity,
)
from teamagent.ingest.video_library_repository import VideoLibraryRepository, _upsert

DAY = date(2026, 10, 29)
NOW = datetime(2026, 10, 29, tzinfo=UTC)


def tab(name: str, rows: list[dict[str, str]], headers: tuple[str, ...] = ()) -> TabRows:
    columns = headers or tuple(dict.fromkeys(key for row in rows for key in row))
    return TabRows(
        "sheet",
        name,
        columns,
        tuple(tuple(row.get(c, "") for c in columns) for row in rows),
        len(rows),
    )


def video_row(number: str = "1", **values: str) -> dict[str, str]:
    return {
        ca.URL_COL: f"https://www.tiktok.com/@vcl/video/{number}",
        "動画ID(抽出)": number,
        ca.ACCOUNT_COL: "メディア表示名",
        ca.ADVERTISER_COL: "#N/A",
        ca.CAMPAIGN_COL: "-",
        **values,
    }


def database(rows: list[dict[str, str]]) -> TabRows:
    return tab(
        "データベース",
        rows,
        tuple(
            dict.fromkeys(
                [
                    ca.URL_COL,
                    "動画ID(抽出)",
                    ca.ACCOUNT_COL,
                    ca.ADVERTISER_COL,
                    ca.CAMPAIGN_COL,
                    ca.TEXT_COL,
                    ca.AD_NAME_COL,
                    *METRIC_COLUMNS.values(),
                ]
            )
        ),
    )


def ledger() -> TabRows:
    return tab(
        "ベクトルメディア",
        [
            {
                "運営": "ベクトル（VCL）",
                "分類": "情報",
                "アカウント名": "メディア表示名",
                "TT": "https://www.tiktok.com/@VCL",
                "IG": "https://www.instagram.com/vcl/",
                "YT": "https://www.youtube.com/@vcl",
            },
            {
                "運営": "client",
                "分類": "企業",
                "アカウント名": "企業",
                "TT": "https://www.tiktok.com/@client",
                "IG": "",
                "YT": "",
            },
        ],
    )


def raw() -> TabRows:
    return tab(
        "全体Raw",
        [
            {
                ca.POST_DATE_URL_COL: video_row()[ca.URL_COL],
                ca.POST_DATE_COL: "2026-10-01T03:04:05Z",
                "videoMeta.duration": "12.5",
                "musicMeta.musicName": "BGM",
                "musicMeta.musicAuthor": "作者",
                "musicMeta.musicOriginal": "false",
            }
        ],
    )


def batch(rows: list[dict[str, str]], **kwargs: object) -> SyncBatch:
    return build_batch(database(rows), raw(), captured_on=DAY, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("advertiser", "caption", "cost", "expected"),
    [
        ("広告主", "", "¥0", False),
        ("広告主", "案件の投稿 #PR", "¥0", True),
        ("#N/A", "投稿 ＃Ｐｒ #話題", "¥0", True),
        ("", "#pr", "-", True),
        ("-", "#PRIZE", "1,000", False),
        ("#N/A", "普通の投稿", "500", False),
    ],
)
def test_pr_uses_only_exact_normalized_hashtag(
    advertiser: str, caption: str, cost: str, expected: bool
) -> None:
    result = batch(
        [video_row(**{ca.ADVERTISER_COL: advertiser, ca.TEXT_COL: caption, ca.COST_COL: cost})]
    )
    assert result.videos[0].is_pr is expected


def test_zero_missing_and_metric_families_are_separate() -> None:
    result = batch(
        [
            video_row(
                **{
                    ca.PLAYS_COL: "9,000",
                    ca.LIKES_COL: "#N/A",
                    ca.SAVES_COL: "0",
                    "オーガニック再生数": "1,125",
                    "オーガニックいいね": "-",
                    "オーガニック保存": "",
                    ca.COST_COL: "¥0",
                    ca.CTR_COL: "0.50%",
                    "動画視聴数": "7,875",
                    "2秒動画再生数": "100",
                    "6秒動画再生数": "90",
                    "25％までの再生数": "80",
                    "50％までの再生数": "70",
                    "75％までの再生数": "60",
                    "再生完了数": "50",
                    "動画視聴1回あたりの平均視聴時間": "3.5",
                    "2秒視聴率": "20%",
                    "6秒視聴率": "18%",
                    "25％動画視聴率": "16%",
                    "50％動画視聴率": "14%",
                    "75％動画視聴率": "12%",
                    "100％動画視聴率": "10%",
                    "いいね": "30",
                    "シェア": "2",
                    "コメント": "1",
                    "有料のお気に入り": "0",
                }
            )
        ]
    )
    m = result.metrics[0]
    assert (m.total_plays, m.organic_plays, m.ad_views) == (9000, 1125, 7875)
    assert (m.total_likes, m.organic_likes, m.organic_saves) == (None, None, None)
    assert (m.total_saves, m.ad_cost, m.ad_saves) == (0, 0, 0)
    assert m.ad_ctr == 0.005
    assert (
        m.ad_views_2s,
        m.ad_views_6s,
        m.ad_views_25pct,
        m.ad_views_50pct,
        m.ad_views_75pct,
        m.ad_completions,
    ) == (100, 90, 80, 70, 60, 50)
    assert (
        m.ad_rate_2s,
        m.ad_rate_6s,
        m.ad_rate_25pct,
        m.ad_rate_50pct,
        m.ad_rate_75pct,
        m.ad_rate_100pct,
    ) == (0.2, 0.18, 0.16, 0.14, 0.12, 0.1)
    assert (m.ad_average_watch_seconds, m.ad_likes, m.ad_shares, m.ad_comments) == (3.5, 30, 2, 1)
    assert m.ad_impressions is None and m.ad_clicks is None
    assert set(METRIC_COLUMNS) == set(Metrics.model_fields) - {
        "platform",
        "video_id",
        "captured_on",
    }


def test_first_valid_duplicate_wins_and_skip_reasons() -> None:
    first = video_row(**{"動画ID(抽出)": "", ca.PLAYS_COL: "1,125"})
    rows = [
        video_row(**{ca.AD_NAME_COL: "審査落ち"}),
        first,
        video_row(**{ca.PLAYS_COL: "99,999"}),
        video_row("2", **{ca.URL_COL: "https://evil.example/video/2"}),
        {},
        video_row("3", **{"動画ID(抽出)": "999"}),
    ]
    result = batch(rows)
    assert result.rows_read == 6
    assert [v.video_id for v in result.videos] == ["1"]
    assert result.metrics[0].total_plays == 1125
    assert result.skipped == {
        "rejected": 1,
        "duplicate": 1,
        "invalid_url": 1,
        "empty_row": 1,
        "id_mismatch": 1,
    }


def test_raw_url_join_and_absent_url_stays_null() -> None:
    result = batch([video_row(), video_row("2", **{ca.TEXT_COL: "#話題 #話題 ＃ＰＲ"})])
    first, missing = result.videos
    assert first.posted_at == datetime(2026, 10, 1, 3, 4, 5, tzinfo=UTC)
    assert (first.duration_seconds, first.music_name, first.music_author, first.music_original) == (
        12.5,
        "BGM",
        "作者",
        False,
    )
    assert missing.posted_at is None and missing.duration_seconds is None
    assert missing.music_original is None and missing.music_name is None
    assert missing.hashtags == ["話題", "PR"]


def test_missing_ledger_is_reported_without_failure() -> None:
    result = batch([video_row()])
    assert result.summary()["categories"] == {"organic": 1}
    assert result.warnings == {"accounts_unconfigured": 1}
    assert result.accounts == [
        Account(platform="tiktok", handle="vcl", display_name="メディア表示名")
    ]


@pytest.mark.parametrize("plays", [0, None])
def test_derived_rates_zero_or_missing_denominator(plays: float | None) -> None:
    result = derived_rates(plays=plays, likes=10, saves=5, comments=2, shares=1)
    assert result.save_rate is None and result.engagement_rate is None


@pytest.mark.parametrize(("plays", "reference"), [(4999, True), (5000, False)])
def test_derived_rates_reference_threshold_and_missing_numerator(
    plays: float, reference: bool
) -> None:
    result = derived_rates(plays=plays, likes=10, saves=5, comments=2, shares=1)
    assert result.save_rate == 5 / plays and result.engagement_rate == 18 / plays
    assert result.is_reference is reference
    missing = derived_rates(plays=plays, likes=None, saves=0, comments=2, shares=1)
    assert missing.save_rate == 0 and missing.engagement_rate is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.tiktok.com/@User/video/123?share=1", ("tiktok", "123", "user")),
        ("https://www.instagram.com/reel/Ab_C-1/", ("instagram", "Ab_C-1", None)),
        ("https://www.youtube.com/shorts/Ab_c-123456", ("youtube", "Ab_c-123456", None)),
        ("https://tiktok.com.evil.example/@user/video/1", None),
        ("https://user@tiktok.com/@user/video/1", None),
        ("https://vm.tiktok.com/short/", None),
        ("#N/A", None),
        ("https://[", None),
    ],
)
def test_video_url_validation(url: str, expected: object) -> None:
    assert video_identity(url) == expected


def test_batch_is_idempotent() -> None:
    rows = [video_row(), video_row("2"), video_row()]
    assert (
        batch(rows, account_ledger=ledger()).model_dump()
        == batch(rows, account_ledger=ledger()).model_dump()
    )


def test_shared_number_parser_accepts_yen_and_rejects_nonfinite() -> None:
    assert ca.to_number("¥1,125") == 1125
    assert ca.to_number("￥0") == 0
    assert ca.to_number("0.00%") == 0
    for value in ("NaN", "inf", "-inf", "#N/A", "-", ""):
        assert ca.to_number(value) is None


class FakeSheets:
    def __init__(self, *, account_tabs: dict[str, TabRows] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.metadata_calls: list[str] = []
        self.account_tabs = (
            account_tabs
            if account_tabs is not None
            else {
                "ベクトルメディア": ledger(),
                "オフショア込み": tab("オフショア込み", [], ledger().headers),
            }
        )

    def get_sheet_metadata(self, sheet_id: str, request_id: str) -> SheetMetadata:
        self.metadata_calls.append(sheet_id)
        if sheet_id == "accounts":
            return SheetMetadata(
                sheet_id,
                "台帳",
                tuple(
                    SheetTab(sheet_id, index, name, 100, len(table.headers))
                    for index, (name, table) in enumerate(self.account_tabs.items())
                ),
            )
        tabs = [SheetTab(sheet_id, 123, "名前変更済み", 100, 40)]
        return SheetMetadata(sheet_id, "DB", tuple(tabs))

    def get_tab_rows(self, sheet_id: str, tab_name: str, request_id: str) -> TabRows:
        self.calls.append((sheet_id, tab_name))
        if sheet_id == "accounts":
            assert sheet_id in self.metadata_calls
            return self.account_tabs[tab_name]
        return {
            "名前変更済み": database([video_row()]),
            "全体Raw": raw(),
        }[tab_name]


@pytest.fixture(autouse=True)
def _keep_global_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    # main() のログ設定はプロセス全体に効くので、テストでは呼ばせない（他テストのログ捕捉を壊さない）
    monkeypatch.setattr(sync, "configure_logging", lambda **_: False)


@pytest.fixture
def source_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHORT_VIDEO_DB_SHEET_ID", "sheet")
    monkeypatch.setenv("SHORT_VIDEO_DB_SHEET_GID", "123")
    monkeypatch.delenv("VIDEO_LIBRARY_ACCOUNTS_SHEET_ID", raising=False)


def test_read_batch_reuses_source_env_and_gid_with_optional_tabs(
    source_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sheets = FakeSheets()
    result = sync.read_batch(sheets, request_id="test", now=NOW)  # type: ignore[arg-type]
    assert result.summary()["categories"] == {"organic": 1}
    assert sheets.calls == [("sheet", "名前変更済み"), ("sheet", "全体Raw")]
    monkeypatch.setenv("VIDEO_LIBRARY_ACCOUNTS_SHEET_ID", "accounts")
    sheets = FakeSheets()
    result = sync.read_batch(sheets, request_id="test", now=NOW)  # type: ignore[arg-type]
    assert result.summary()["categories"] == {"organic": 1}
    assert sheets.metadata_calls == ["sheet", "accounts"]
    assert sheets.calls[-2:] == [
        ("accounts", "ベクトルメディア"),
        ("accounts", "オフショア込み"),
    ]
    # UTC previous day, JST next day: snapshots use the Japanese business date.
    next_day = sync.read_batch(
        sheets, request_id="test", now=datetime(2026, 10, 29, 16, tzinfo=UTC)
    )  # type: ignore[arg-type]
    assert next_day.metrics[0].captured_on == date(2026, 10, 30)


def test_cli_dry_run_is_json_and_never_opens_db(
    source_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sync.GSheetsClient, "from_env", lambda: FakeSheets())

    def forbidden() -> None:
        pytest.fail("dry-run opened DB")

    monkeypatch.setattr(sync.PgVectorClient, "from_env", forbidden)
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        assert sync.main(["--dry-run"]) == 0
    summary = next(log for log in logs if log["event"] == "video_library_complete")
    assert summary["rows_read"] == summary["unique_videos"] == 1
    assert summary["categories"] == {"organic": 1}
    assert summary["pr_videos"] == 0 and summary["skipped"] == {}
    assert all(log["request_id"] == summary["request_id"] for log in logs)


def test_cli_commit_and_error_logs_never_expose_cells(
    source_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sync.GSheetsClient, "from_env", lambda: FakeSheets())
    pg, repository = MagicMock(), MagicMock()
    monkeypatch.setattr(sync.PgVectorClient, "from_env", lambda: pg)
    monkeypatch.setattr(sync, "VideoLibraryRepository", lambda client: repository)
    repository.save.return_value = 1
    assert sync.main([]) == 0
    assert repository.save.call_count == 1 and pg.close.call_count == 1
    repository.save.side_effect = ValueError("secret client name and private caption")
    with structlog.testing.capture_logs() as captured:
        assert sync.main([]) == 1
    logs = json.dumps(captured, ensure_ascii=False) + capsys.readouterr().out
    assert "secret client" not in logs and "private caption" not in logs
    assert repository.record_failure.call_args.kwargs["error"] == "ValueError"


def test_invalid_main_headers_fail_before_any_write() -> None:
    with pytest.raises(ValueError, match="database_columns_missing"):
        build_batch(tab("db", [{"wrong": "secret"}]), raw(), captured_on=DAY)


def test_upsert_sql_guards_unchanged_rows_and_preserves_p2_fields() -> None:
    conn = MagicMock()
    conn.execute.return_value = SimpleNamespace(rowcount=0)
    assert (
        _upsert(
            conn,
            "vl_videos",
            Video(platform="tiktok", video_id="1", url="url"),
            ("platform", "video_id"),
        )
        == 0
    )
    query, values = conn.execute.call_args.args
    statement = " ".join(query.as_string().split())
    assert 'ON CONFLICT ("platform", "video_id") DO UPDATE' in statement
    assert "IS DISTINCT FROM" in statement
    assert "updated_at = now()" in statement
    assert "first_ingested_at" not in statement
    assert "thumbnail_s3_key" not in statement and "video_s3_key" not in statement
    assert values[0:3] == ["tiktok", "1", "url"]


def test_campaign_upsert_only_inserts_discovered_pair() -> None:
    conn = MagicMock()
    conn.execute.return_value.rowcount = 0
    assert (
        _upsert(
            conn,
            "vl_campaigns",
            Campaign(advertiser="広告主", campaign="案件"),
            ("advertiser", "campaign"),
        )
        == 0
    )
    query, values = conn.execute.call_args.args
    assert 'ON CONFLICT ("advertiser", "campaign") DO NOTHING' in query.as_string()
    assert values == ["広告主", "案件"]


def test_sync_models_exclude_human_managed_settings() -> None:
    assert not {"business_category", "is_private"} & Video.model_fields.keys()
    assert set(Campaign.model_fields) == {"advertiser", "campaign"}


def test_campaigns_are_only_distinct_pairs_from_accepted_videos() -> None:
    first = video_row(**{ca.ADVERTISER_COL: "広告主", ca.CAMPAIGN_COL: "案件"})
    result = batch(
        [
            first,
            first,
            video_row(
                "2", **{ca.ADVERTISER_COL: "広告主", ca.CAMPAIGN_COL: "案件", ca.TEXT_COL: "#PR"}
            ),
            video_row("3", **{ca.ADVERTISER_COL: "広告主", ca.CAMPAIGN_COL: "別案件"}),
            video_row("4", **{ca.ADVERTISER_COL: "広告主"}),
            video_row("5", **{ca.CAMPAIGN_COL: "広告主なし"}),
            video_row(
                "6",
                **{
                    ca.ADVERTISER_COL: "除外",
                    ca.CAMPAIGN_COL: "審査落ち",
                    ca.AD_NAME_COL: "審査落ち",
                },
            ),
        ]
    )
    assert result.campaigns == [
        Campaign(advertiser="広告主", campaign="案件"),
        Campaign(advertiser="広告主", campaign="別案件"),
    ]


def test_repository_uses_admin_rls_and_single_transaction() -> None:
    pg = MagicMock()
    pg.connection.return_value.__enter__.return_value.execute.return_value.rowcount = 1
    result = batch([video_row()])
    assert VideoLibraryRepository(pg).save(result, request_id="test", started_at=NOW) == 1
    pg.connection.assert_called_once_with(
        app_role="teamagent_app",
        user_email=None,
        user_role="admin",
        application_name="teamagent-video-library-sync",
    )
    conn = pg.connection.return_value.__enter__.return_value
    assert conn.execute.call_count == 4  # account + video + metrics + run


def test_migration_only_adds_six_tables_with_rls() -> None:
    sql = (
        Path(__file__).resolve().parents[2] / "infra/migrations/0035_video_library.sql"
    ).read_text()
    assert sql.count("CREATE TABLE IF NOT EXISTS vl_") == 6
    assert sql.count("ENABLE ROW LEVEL SECURITY") == 6
    assert sql.count("FORCE ROW LEVEL SECURITY") == 6
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" not in sql
    assert "DOUBLE PRECISION NOT NULL DEFAULT 0" not in sql
    for column in (
        "business_category",
        "industry",
        "purpose",
        "is_private",
        "report_drive_file_id",
    ):
        assert not re.search(rf"^\s+{column}\s", sql, re.MULTILINE)


def test_shared_raw_join_keeps_first_valid_date_after_invalid_duplicate() -> None:
    url = video_row()[ca.URL_COL]
    source = tab(
        "全体Raw",
        [
            {ca.POST_DATE_URL_COL: url, ca.POST_DATE_COL: "#N/A"},
            {ca.POST_DATE_URL_COL: url, ca.POST_DATE_COL: "2026-10-02T00:00:00Z"},
        ],
    )
    assert ca.build_post_date_index(source.headers, source.rows) == {url: "2026-10-02"}
    result = build_batch(database([video_row()]), source, captured_on=DAY)
    assert result.videos[0].posted_at == datetime(2026, 10, 2, tzinfo=UTC)


def test_missing_gid_configuration_never_falls_back_to_first_tab(
    source_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SHORT_VIDEO_DB_SHEET_GID")
    sheets = FakeSheets()
    with pytest.raises(ValueError, match="short_video_source_unconfigured"):
        sync.read_batch(sheets, request_id="test", now=NOW)  # type: ignore[arg-type]
    assert sheets.calls == []


def test_repository_failure_run_preserves_read_counts_and_only_safe_error() -> None:
    pg = MagicMock()
    result = batch([video_row(), video_row()])
    VideoLibraryRepository(pg).record_failure(
        request_id="test", started_at=NOW, error="ValueError", batch=result
    )
    params = pg.connection.return_value.__enter__.return_value.execute.call_args.args[1]
    assert params[2] == 2 and params[3] == 0
    assert params[4].obj == {"duplicate": 1}
    assert params[-1] == "ValueError"


@pytest.mark.parametrize(
    "timestamp",
    ["2025 11 01T11:00:00.000Z", "2025/11/01T11:00:00.000Z", "2025-11-01T11:00:00.000Z"],
)
def test_library_posted_at_normalizes_date_separators(timestamp: str) -> None:
    source = tab(
        "全体Raw", [{ca.POST_DATE_URL_COL: video_row()[ca.URL_COL], ca.POST_DATE_COL: timestamp}]
    )
    result = build_batch(database([video_row()]), source, captured_on=DAY)
    assert result.videos[0].posted_at == datetime(2025, 11, 1, 11, tzinfo=UTC)


def test_url_missing_is_distinct_from_invalid_url_and_empty_row() -> None:
    result = batch(
        [
            video_row(**{ca.URL_COL: ""}),
            video_row(**{ca.URL_COL: " #N/A "}),
            video_row(**{ca.URL_COL: "not a post URL"}),
            {},
        ]
    )
    assert result.skipped == {"url_missing": 2, "invalid_url": 1, "empty_row": 1}
    assert not result.videos


@pytest.mark.parametrize("bracket", ["【", "［", "(", "（"])
def test_hashtags_stop_at_bracket_annotations(bracket: str) -> None:
    result = batch(
        [video_row(**{ca.TEXT_COL: f"#ラーメン好き{bracket}7/30配信】 #PR{bracket}広告】"})]
    )
    assert result.videos[0].hashtags == ["ラーメン好き", "PR"]
    assert result.videos[0].is_pr


def test_migration_drops_each_policy_before_creation() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "infra/migrations/0035_video_library.sql"
    ).read_text()
    policies = re.findall(r"CREATE POLICY (\w+) ON (\w+)", source)
    assert len(policies) == 18
    for name, table_name in policies:
        assert (
            f"DROP POLICY IF EXISTS {name} ON {table_name};\nCREATE POLICY {name} ON {table_name}"
            in source
        )
    assert "operator_label TEXT" in source
    assert "is_own_media" not in source
    assert "('organic', 'オーガニック', 50)" in source
    assert "    operator TEXT" not in source


def test_read_batch_combines_account_tabs_by_header(
    source_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_LIBRARY_ACCOUNTS_SHEET_ID", "accounts")
    first = tab(
        "ベクトルメディア",
        [
            {
                "運営": "NewsTV",
                "分類": "情報",
                "アカウント名": "First",
                "TT": "https://www.tiktok.com/@vcl",
            }
        ],
        ledger().headers,
    )
    second = tab(
        "オフショア込み",
        [
            {
                "分類": "旅行",
                "追加列": "extra",
                "アカウント名": "Second",
                "TT": "https://www.tiktok.com/@offshore",
                "運営": "オフショア",
            }
        ],
        ("分類", "追加列", "アカウント名", "TT", "運営"),
    )
    # Metadata order must not decide which tab wins.
    sheets = FakeSheets(account_tabs={"オフショア込み": second, "ベクトルメディア": first})
    builder = MagicMock(wraps=sync.build_batch)
    monkeypatch.setattr(sync, "build_batch", builder)
    result = sync.read_batch(sheets, request_id="test", now=NOW)  # type: ignore[arg-type]
    assert sheets.metadata_calls == ["sheet", "accounts"]
    assert sheets.calls[-2:] == [("accounts", "ベクトルメディア"), ("accounts", "オフショア込み")]
    accounts = {account.handle: account for account in result.accounts}
    assert set(accounts) == {"vcl", "offshore"}
    assert (
        accounts["vcl"].display_name,
        accounts["vcl"].classification,
        accounts["vcl"].operator_label,
    ) == ("First", "情報", "NewsTV")
    assert (
        accounts["offshore"].display_name,
        accounts["offshore"].classification,
        accounts["offshore"].operator_label,
    ) == ("Second", "旅行", "オフショア")
    assert result.summary()["categories"] == {"organic": 1}
    merged = builder.call_args.kwargs["account_ledger"]
    assert isinstance(merged, TabRows) and merged.row_count == 2
    assert merged.headers == (*ledger().headers, "追加列")
    assert dict(zip(merged.headers, merged.rows[1], strict=True)) == {
        "運営": "オフショア",
        "分類": "旅行",
        "アカウント名": "Second",
        "TT": "https://www.tiktok.com/@offshore",
        "IG": "",
        "YT": "",
        "追加列": "extra",
    }
    assert "accounts_tab_missing" not in result.warnings


@pytest.mark.parametrize("available", [("ベクトルメディア",), ("オフショア込み",), ()])
def test_read_batch_warns_for_missing_account_tabs(
    source_env: None,
    monkeypatch: pytest.MonkeyPatch,
    available: tuple[str, ...],
) -> None:
    monkeypatch.setenv("VIDEO_LIBRARY_ACCOUNTS_SHEET_ID", "accounts")
    sheets = FakeSheets(account_tabs={name: ledger() for name in available})
    result = sync.read_batch(sheets, request_id="test", now=NOW)  # type: ignore[arg-type]
    assert sheets.metadata_calls == ["sheet", "accounts"]
    assert sheets.calls == [
        ("sheet", "名前変更済み"),
        ("sheet", "全体Raw"),
        *[("accounts", name) for name in available],
    ]
    assert result.warnings.get("accounts_tab_missing", 0) == 2 - len(available)
    assert result.summary()["categories"] == {"organic": 1}


def test_read_batch_account_duplicates_keep_first_tab(
    source_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_LIBRARY_ACCOUNTS_SHEET_ID", "accounts")
    first = tab(
        "ベクトルメディア",
        [
            {
                "運営": "外部",
                "分類": "First category",
                "アカウント名": "First name",
                "TT": "https://www.tiktok.com/@vcl",
            }
        ],
        ledger().headers,
    )
    second = tab(
        "オフショア込み",
        [
            {
                "運営": "オフショア",
                "分類": "Second category",
                "アカウント名": "Second name",
                "TT": "https://www.tiktok.com/@VCL/",
            }
        ],
        ledger().headers,
    )
    sheets = FakeSheets(account_tabs={"オフショア込み": second, "ベクトルメディア": first})
    result = sync.read_batch(sheets, request_id="test", now=NOW)  # type: ignore[arg-type]
    assert len(result.accounts) == 1
    account = result.accounts[0]
    assert (account.operator_label, account.display_name, account.classification) == (
        "外部",
        "First name",
        "First category",
    )
    assert result.summary()["categories"] == {"organic": 1}


@pytest.mark.parametrize("advertiser", ["#N/A", "", "-"])
def test_non_pr_video_is_organic(advertiser: str) -> None:
    result = batch([video_row(**{ca.ADVERTISER_COL: advertiser})])
    assert not result.videos[0].is_pr
    assert result.summary()["categories"] == {"organic": 1}
    assert "advertiser_without_pr" not in result.warnings


def test_advertiser_without_pr_is_organic_and_warned_once_per_video() -> None:
    row = video_row(**{ca.ADVERTISER_COL: "広告主", ca.CAMPAIGN_COL: "案件"})
    pr = video_row(
        "2", **{ca.ADVERTISER_COL: "広告主", ca.CAMPAIGN_COL: "案件", ca.TEXT_COL: "#PR"}
    )
    result = batch([row, row, pr], account_ledger=ledger())
    assert result.summary()["categories"] == {"organic": 1, "unset": 1}
    assert [video.is_pr for video in result.videos] == [False, True]
    assert result.warnings["advertiser_without_pr"] == 1
    assert result.skipped == {"duplicate": 1}
    assert result.campaigns == [Campaign(advertiser="広告主", campaign="案件")]


@pytest.mark.parametrize(
    "label",
    [
        "NewsTV",
        "ベクトル（VCL）",
        "ベクトル九州",
        "gracemode",
        "イニシャル",
        "オフショア",
        "外部",
        "",
    ],
)
@pytest.mark.parametrize("is_pr", [True, False])
def test_operator_label_does_not_affect_business_category(label: str, is_pr: bool) -> None:
    accounts = tab(
        "ベクトルメディア", [{"運営": label, "TT": "https://www.tiktok.com/@vcl"}], ledger().headers
    )
    result = batch(
        [
            video_row(
                **{
                    ca.ADVERTISER_COL: "広告主",
                    ca.CAMPAIGN_COL: "案件",
                    ca.TEXT_COL: "#PR" if is_pr else "通常投稿",
                }
            )
        ],
        account_ledger=accounts,
    )
    assert result.accounts[0].operator_label == (label or None)
    assert result.summary()["categories"] == ({"unset": 1} if is_pr else {"organic": 1})
    assert result.videos[0].is_pr is is_pr


def test_organic_category_exists_in_migration() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "infra/migrations/0035_video_library.sql"
    ).read_text()
    assert "('organic', 'オーガニック', 50)" in source


@pytest.mark.parametrize("campaign_category", [None, "media", "tto", "kirinuki", "other", "custom"])
@pytest.mark.parametrize("is_pr", [True, False])
def test_resolve_business_category_uses_pr_and_human_setting(
    is_pr: bool,
    campaign_category: str | None,
) -> None:
    assert resolve_business_category(is_pr=is_pr, campaign_category=campaign_category) == (
        campaign_category if is_pr else "organic"
    )


def test_summary_counts_organic_and_unset_without_persisting_categories() -> None:
    result = batch(
        [
            video_row(),
            video_row("2", **{ca.TEXT_COL: "＃Ｐｒ"}),
            video_row(
                "3", **{ca.ADVERTISER_COL: "広告主", ca.CAMPAIGN_COL: "案件", ca.TEXT_COL: "#PR"}
            ),
            video_row("3"),
        ]
    )
    assert result.summary()["categories"] == {"organic": 1, "unset": 2}
    assert result.summary()["pr_videos"] == 2
    assert all("business_category" not in video.model_dump() for video in result.videos)


def test_summary_uses_shared_business_category_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.ingest import video_library

    rule = MagicMock(return_value="future-category")
    monkeypatch.setattr(video_library, "resolve_business_category", rule)
    assert batch([video_row()]).summary()["categories"] == {"future-category": 1}
    rule.assert_called_once_with(is_pr=False)


def test_read_batch_ignores_campaign_settings_tab(
    source_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheets = FakeSheets()
    monkeypatch.setattr(
        sheets,
        "get_sheet_metadata",
        lambda sheet_id, request_id: SheetMetadata(
            sheet_id,
            "DB",
            (
                SheetTab(sheet_id, 123, "名前変更済み", 100, 40),
                SheetTab(sheet_id, 456, "案件マスタ", 10, 6),
            ),
        ),
    )
    result = sync.read_batch(sheets, request_id="test", now=NOW)  # type: ignore[arg-type]
    assert sheets.calls == [("sheet", "名前変更済み"), ("sheet", "全体Raw")]
    assert result.warnings == {"accounts_unconfigured": 1}


@pytest.mark.parametrize("stage", ["metadata", "first_tab", "second_tab"])
def test_accounts_ledger_failure_warns_and_continues_without_ledger(
    source_env: None,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    monkeypatch.setenv("VIDEO_LIBRARY_ACCOUNTS_SHEET_ID", "accounts")
    sheets = FakeSheets()
    get_metadata, get_rows = sheets.get_sheet_metadata, sheets.get_tab_rows

    def metadata(sheet_id: str, request_id: str) -> SheetMetadata:
        if sheet_id == "accounts" and stage == "metadata":
            raise RuntimeError("private ledger details")
        return get_metadata(sheet_id, request_id)

    def rows(sheet_id: str, tab_name: str, request_id: str) -> TabRows:
        failed_tab = "ベクトルメディア" if stage == "first_tab" else "オフショア込み"
        if sheet_id == "accounts" and tab_name == failed_tab:
            raise RuntimeError("private ledger details")
        return get_rows(sheet_id, tab_name, request_id)

    monkeypatch.setattr(sheets, "get_sheet_metadata", metadata)
    monkeypatch.setattr(sheets, "get_tab_rows", rows)
    builder = MagicMock(wraps=sync.build_batch)
    monkeypatch.setattr(sync, "build_batch", builder)
    result = sync.read_batch(sheets, request_id="test", now=NOW)  # type: ignore[arg-type]
    assert result.warnings["accounts_ledger_unavailable"] == 1
    assert result.accounts == []
    assert "accounts_unconfigured" not in result.warnings
    assert len(result.videos) == len(result.metrics) == 1
    assert result.summary()["categories"] == {"organic": 1}
    assert builder.call_args.kwargs["account_ledger"] is None


@pytest.mark.parametrize("failed_tab", ["名前変更済み", "全体Raw"])
def test_required_sheet_failure_still_fails_sync(
    source_env: None,
    monkeypatch: pytest.MonkeyPatch,
    failed_tab: str,
) -> None:
    sheets = FakeSheets()
    original = sheets.get_tab_rows

    def get_rows(sheet_id: str, tab_name: str, request_id: str) -> TabRows:
        if tab_name == failed_tab:
            raise RuntimeError("required sheet unavailable")
        return original(sheet_id, tab_name, request_id)

    monkeypatch.setattr(sheets, "get_tab_rows", get_rows)
    with pytest.raises(RuntimeError, match="required sheet unavailable"):
        sync.read_batch(sheets, request_id="test", now=NOW)  # type: ignore[arg-type]


def test_cli_expands_oauth_json_and_passes_sources_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import os

    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "GOOGLE_OAUTH_JSON",
        json.dumps({"client_id": "id", "client_secret": "secret", "refresh_token": "token"}),
    )

    def sheets_from_env() -> FakeSheets:
        assert os.environ["GOOGLE_CLIENT_ID"] == "id"
        assert os.environ["GOOGLE_CLIENT_SECRET"] == "secret"
        assert os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"] == "token"
        return FakeSheets()

    monkeypatch.setattr(sync.GSheetsClient, "from_env", sheets_from_env)
    reader = MagicMock(return_value=SyncBatch())
    monkeypatch.setattr(sync, "read_batch", reader)
    path = tmp_path / "resolved.yaml"
    assert sync.main(["--dry-run"], sources_path=path) == 0
    assert reader.call_args.kwargs["sources_path"] == path
