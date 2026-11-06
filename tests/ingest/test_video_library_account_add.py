"""Account intake parsing and rejection before opening a database connection."""

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest.video_library_account_repository import (
    AccountAddError,
    AccountAddRepository,
    inspect_account_input,
    normalize_account_handle,
)


@pytest.mark.parametrize(
    "value",
    [
        "https://www.tiktok.com/@Some.Handle_1",
        "https://www.tiktok.com/@Some.Handle_1?lang=ja#profile",
        "tiktok.com/@Some.Handle_1",
        "www.tiktok.com/@Some.Handle_1",
        "m.tiktok.com/@Some.Handle_1",
        "https://m.tiktok.com/@Some.Handle_1/",
        "@Some.Handle_1",
        "Some.Handle_1",
        "  @Some.Handle_1  ",
        "https://www.tiktok.com/@Some.Handle_1/video/123456?lang=ja#video",
        "tiktok.com/@Some.Handle_1/video/123456",
    ],
)
def test_normalize_all_account_forms(value: str) -> None:
    assert normalize_account_handle(value) == "some.handle_1"


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/@sample",
        "https://www.tiktok.com.evil.example/@sample",
        "https://eviltiktok.com/@sample",
        "https://tiktok.com@evil.example/@sample",
        "https://evil.example@www.tiktok.com/@sample",
        "https://vt.tiktok.com/abcdef/",
        "https://vm.tiktok.com/abcdef/",
        "https://www.tiktok.com:444/@sample",
        "http://www.tiktok.com/@sample",
        "ftp://www.tiktok.com/@sample",
        "https://[invalid/@sample",
        "https://www.tiktok.com/anything/@sample",
        "https://www.tiktok.com/@sample/extra",
        "https://www.tiktok.com/@sample/video/not-a-number",
        "https://www.tiktok.com/@sample/video/１２３",
        "https://www.tiktok.com/@sample/video/",
        "https://www.tiktok.com/@sam%70le",
        "https://www.tiktok.com/@sam\nple",
        "https://www.tiktok.com\\@sample",
        "@@sample",
        "has-hyphen",
        "has space",
        "日本語",
        "a",
        "a" * 25,
        "@a",
        "https://www.tiktok.com/@a",
        "https://www.tiktok.com/@" + "a" * 25,
        "bad\x00input",
    ],
)
def test_invalid_accounts_have_fixed_error(value: str) -> None:
    with pytest.raises(AccountAddError, match=r"^invalid_account$"):
        normalize_account_handle(value)


@pytest.mark.parametrize("length", [2, 24])
@pytest.mark.parametrize("prefix", ["", "@", "https://www.tiktok.com/@"])
def test_handle_length_boundaries(length: int, prefix: str) -> None:
    assert normalize_account_handle(prefix + "A" * length) == "a" * length


def test_blank_invalid_and_normalized_duplicates_preserve_line_numbers() -> None:
    rows = inspect_account_input(
        [
            "",
            " @SAMPLE ",
            "https://www.tiktok.com/@sample/video/123",
            "wrong!",
            " ",
            "sample",
            "other",
        ]
    )
    assert [(r.line_no, r.handle, r.status, r.duplicate_of) for r in rows] == [
        (2, "sample", "added", None),
        (3, "sample", "duplicate", 2),
        (4, None, "invalid", None),
        (6, "sample", "duplicate", 2),
        (7, "other", "added", None),
    ]
    assert rows[2].error_code == "invalid_account"
    assert inspect_account_input(["", " \t "]) == []


def test_exactly_fifty_lines_allowed() -> None:
    assert len(inspect_account_input([f"handle{i}" for i in range(50)])) == 50


@pytest.mark.parametrize("method", ["preview", "add"])
@pytest.mark.parametrize(
    "lines,operator,email,error",
    [
        (["sample"] * 51, "NewsTV", "member@example.test", "too_many_lines"),
        ([""] * 51, "NewsTV", "member@example.test", "too_many_lines"),
        (["sample"], "other", "member@example.test", "invalid_operator"),
        (["sample"], "newstv", "member@example.test", "invalid_operator"),
        (["sample"], "NewsTV ", "member@example.test", "invalid_operator"),
        (["sample"], "NewsTV", " ", "invalid_user_email"),
        (["sample"], "NewsTV", "member\x00@example.test", "invalid_user_email"),
    ],
)
def test_invalid_batch_is_rejected_before_database_access(
    method: str, lines: list[str], operator: str, email: str, error: str
) -> None:
    pg = MagicMock(spec=PgVectorClient)
    repo = AccountAddRepository(pg)
    with pytest.raises(AccountAddError, match=f"^{error}$"):
        getattr(repo, method)(
            lines, operator_label=operator, user_email=email, request_id="invalid-batch"
        )
    pg.connection.assert_not_called()


def test_preview_selects_only_unique_valid_accounts() -> None:
    pg = MagicMock(spec=PgVectorClient)
    conn = pg.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.side_effect = [{"operator_label": None}, None]
    rows = AccountAddRepository(pg).preview(
        ["existing", "new_handle", "bad!", "@NEW_HANDLE"],
        operator_label="NewsTV",
        user_email="member@example.test",
        request_id="preview",
    )
    assert [(r.status, r.operator_label) for r in rows] == [
        ("exists", None),
        ("added", "NewsTV"),
        ("invalid", None),
        ("duplicate", None),
    ]
    assert conn.execute.call_count == 2
    assert all(call.args[0].startswith("SELECT ") for call in conn.execute.call_args_list)


def test_conflict_returns_actual_operator_and_logs_only_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    pg = MagicMock(spec=PgVectorClient)
    conn = pg.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.side_effect = [None, None, {"operator_label": "existing"}]
    rows = AccountAddRepository(pg).add(
        ["secret_handle"],
        operator_label="NewsTV",
        user_email="member@example.test",
        user_role="viewer",
        request_id="conflict",
    )
    assert [(r.status, r.operator_label) for r in rows] == [("exists", "existing")]
    pg.connection.assert_called_once_with(
        app_role="teamagent_app",
        user_email="member@example.test",
        user_role="viewer",
        application_name="teamagent-account-add",
    )
    output = capsys.readouterr().out
    assert "conflict" in output and "exists" in output
    assert "secret_handle" not in output and "member@example.test" not in output


def test_existing_normalized_account_is_checked_before_insert() -> None:
    pg = MagicMock(spec=PgVectorClient)
    conn = pg.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchone.return_value = {"operator_label": "existing"}
    rows = AccountAddRepository(pg).add(
        ["sample"], operator_label="NewsTV", user_email="member@example.test", request_id="legacy"
    )
    assert rows[0].status == "exists" and rows[0].operator_label == "existing"
    conn.execute.assert_called_once()
    query, params = conn.execute.call_args.args
    assert "lower(ltrim(handle,'@'))=%s" in query and params == ("sample",)
