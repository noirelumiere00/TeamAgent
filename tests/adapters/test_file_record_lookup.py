"""ファイル記録は索引にせず、連番＋枝番と保存URLの確定対応だけ読む。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gsheets_client import GSheetsClient, TabRows

HEADERS = ("連番", "枝番", "Slack投稿URL", "保存ファイルリンク", "保存ファイル（リンク付き）")
POST = "https://example.slack.com/archives/C0123456789/p1792461784123456"
SOURCE = "slack://C0123456789/1792461784.123456"
URL = "https://drive.google.com/file/d/FILE_A/view"


def _client(rows: list[tuple[str, ...]], headers: tuple[str, ...] = HEADERS) -> GSheetsClient:
    client = GSheetsClient(service=MagicMock())
    client.get_tab_rows = MagicMock(
        return_value=TabRows("SHEET", "ファイル記録", headers, tuple(rows), len(rows))
    )
    return client


def test_exact_post_maps_unique_record_without_filename_guessing() -> None:
    client = _client([("20261020-001", "1", POST, URL, "different_display_name.pdf")])

    refs = client.lookup_saved_file_records("SHEET", "req", slack_sources=[SOURCE])

    assert len(refs) == 1
    assert (refs[0].record_number, refs[0].branch_number) == ("20261020-001", "1")
    assert refs[0].slack_source_uri == SOURCE
    assert refs[0].drive_file_id == "FILE_A"
    assert refs[0].drive_url == URL
    client.get_tab_rows.assert_called_once_with(
        "SHEET", "ファイル記録", "req", value_render_option="FORMULA"
    )


def test_two_branches_for_post_need_explicit_record_key() -> None:
    client = _client(
        [
            ("20261020-001", "1", POST, URL, "first.pdf"),
            ("20261020-001", "2", POST, URL.replace("FILE_A", "FILE_B"), "second.pdf"),
        ],
        (*HEADERS[:-1], "保存名"),
    )

    assert client.lookup_saved_file_records("SHEET", "req", slack_sources=[POST]) == []
    refs = client.lookup_saved_file_records("SHEET", "req", record_keys=[("20261020-001", "2")])
    assert [ref.drive_file_id for ref in refs] == ["FILE_B"]


def test_duplicate_record_key_is_ambiguous_even_with_same_url() -> None:
    row = ("20261020-001", "1", POST, URL, "material.pdf")
    client = _client([row, row], (*HEADERS[:-1], "保存名"))

    assert (
        client.lookup_saved_file_records(
            "SHEET", "req", record_keys=[("20261020-001", "1")], slack_sources=[POST]
        )
        == []
    )


def test_hyperlink_formula_preserves_saved_url_and_label() -> None:
    client = _client([("20261020-001", "1", POST, "", f'=HYPERLINK("{URL}","material.pdf")')])

    refs = client.lookup_saved_file_records("SHEET", "req", slack_sources=[POST])
    assert refs[0].drive_url == URL
    assert refs[0].title == "material.pdf"


@pytest.mark.parametrize(
    "url",
    [
        "https://drive.google.com.evil.test/file/d/FILE_A/view",
        "https://drive.google.com/file/d/FILE_A.invalid/view",
        "https://drive.google.com/open?id=FILE_A&id=FILE_B",
        '=HYPERLINK(A1,"material.pdf")',
    ],
)
def test_unconfirmed_saved_link_is_not_guessed(url: str) -> None:
    client = _client([("20261020-001", "1", POST, url, "")], (*HEADERS[:-1], "保存名"))

    assert client.lookup_saved_file_records("SHEET", "req", slack_sources=[POST]) == []


def test_record_number_without_branch_is_not_enough() -> None:
    client = _client([("20261020-001", "", POST, URL, "")], (*HEADERS[:-1], "保存名"))

    assert client.lookup_saved_file_records("SHEET", "req", slack_sources=[POST]) == []


def test_rich_text_hyperlink_is_read_without_writing() -> None:
    service = MagicMock()
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [
            {
                "data": [
                    {
                        "rowData": [
                            {"values": [{"formattedValue": header} for header in HEADERS]},
                            {
                                "values": [
                                    {"formattedValue": "20261020-001"},
                                    {"formattedValue": "1"},
                                    {"formattedValue": POST},
                                    {},
                                    {"formattedValue": "material.pdf", "hyperlink": URL},
                                ]
                            },
                        ]
                    }
                ]
            }
        ]
    }
    client = _client([("20261020-001", "1", POST, "", "material.pdf")])
    client._service = service

    refs = client.lookup_saved_file_records("SHEET", "req", slack_sources=[POST])

    assert refs[0].drive_url == URL
    kwargs = service.spreadsheets.return_value.get.call_args.kwargs
    assert kwargs["includeGridData"] is True
    assert "hyperlink" in kwargs["fields"]
    service.spreadsheets.return_value.values.return_value.update.assert_not_called()


def test_rich_text_read_does_not_mix_keys_from_an_earlier_snapshot() -> None:
    service = MagicMock()
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [
            {
                "data": [
                    {
                        "rowData": [
                            {"values": [{"formattedValue": header} for header in HEADERS]},
                            {
                                "values": [
                                    {"formattedValue": "20261020-002"},
                                    {"formattedValue": "1"},
                                    {},
                                    {},
                                    {
                                        "formattedValue": "moved.pdf",
                                        "hyperlink": URL.replace("FILE_A", "FILE_B"),
                                    },
                                ]
                            },
                        ]
                    }
                ]
            }
        ]
    }
    client = _client([("20261020-001", "1", POST, "", "material.pdf")])
    client._service = service

    assert (
        client.lookup_saved_file_records("SHEET", "req", record_keys=[("20261020-001", "1")]) == []
    )


def test_channel_ts_columns_can_match_without_post_url() -> None:
    client = _client(
        [("20261020-001", "1", "C0123456789", "1792461784.123456", URL)],
        ("連番", "枝番", "チャンネルID", "ts", "保存ファイルリンク"),
    )

    refs = client.lookup_saved_file_records("SHEET", "req", slack_sources=[POST])
    assert refs[0].slack_source_uri == SOURCE


def test_empty_requests_do_not_fetch_sheet() -> None:
    client = _client([])
    assert client.lookup_saved_file_records("SHEET", "req") == []
    client.get_tab_rows.assert_not_called()


def test_conflicting_saved_links_are_not_picked_by_column_order() -> None:
    client = _client(
        [
            (
                "20261020-001",
                "1",
                POST,
                URL,
                f'=HYPERLINK("{URL.replace("FILE_A", "FILE_B")}","material.pdf")',
            )
        ]
    )

    assert client.lookup_saved_file_records("SHEET", "req", slack_sources=[POST]) == []


def test_post_reply_is_not_matched_as_its_parent_thread() -> None:
    reply = POST.replace("p1792461784123456", "p1792461785123456")
    client = _client([("20261020-001", "1", reply + "?thread_ts=1792461784.123456", URL, "")])

    assert client.lookup_saved_file_records("SHEET", "req", slack_sources=[SOURCE]) == []


def test_rich_text_snapshot_also_keeps_plain_mappings_in_same_batch() -> None:
    service = MagicMock()
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [
            {
                "data": [
                    {
                        "rowData": [
                            {"values": [{"formattedValue": header} for header in HEADERS]},
                            {
                                "values": [
                                    {"formattedValue": "20261020-001"},
                                    {"formattedValue": "1"},
                                    {},
                                    {"formattedValue": URL},
                                    {"formattedValue": "first.pdf"},
                                ]
                            },
                            {
                                "values": [
                                    {"formattedValue": "20261020-001"},
                                    {"formattedValue": "2"},
                                    {},
                                    {},
                                    {
                                        "formattedValue": "second.pdf",
                                        "hyperlink": URL.replace("FILE_A", "FILE_B"),
                                    },
                                ]
                            },
                        ]
                    }
                ]
            }
        ]
    }
    client = _client(
        [
            ("20261020-001", "1", "", URL, "first.pdf"),
            ("20261020-001", "2", "", "", "second.pdf"),
        ]
    )
    client._service = service

    records = client.lookup_saved_file_records(
        "SHEET", "req", record_keys=[("20261020-001", "1"), ("20261020-001", "2")]
    )
    assert [record.drive_file_id for record in records] == ["FILE_A", "FILE_B"]
