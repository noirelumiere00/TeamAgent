"""Authenticated, escaped account confirmation and submission without a database."""

from unittest.mock import patch

import httpx
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from teamagent.ingest.video_library_account_repository import (
    OWN_ACCOUNT_OPERATORS,
    AccountAddRepository,
)
from tests.connect_web.test_library_import import ORIGIN, ImportPg, intake
from tests.connect_web.test_search_routes import _EMAIL

__all__ = ["intake"]
PATH = "/library/import/accounts"


def post_accounts(client: TestClient, **fields: str) -> httpx.Response:
    return client.post(
        PATH,
        headers=ORIGIN,
        data={"accounts": "@sample", "operator_label": "NewsTV", "action": "check", **fields},
        follow_redirects=False,
    )


def test_account_form_is_separate_and_requires_operator(
    intake: tuple[TestClient, ImportPg],
) -> None:
    client, _ = intake
    soup = BeautifulSoup(client.get("/library/import").text, "html.parser")
    form = soup.select_one(f'form[action="{PATH}"]')
    assert form and form.find_parent("form") is None
    textarea = form.select_one('textarea[name="accounts"]')
    assert textarea.has_attr("required")
    assert form.find("label", attrs={"for": textarea["id"]}).get_text() == (
        "TikTok アカウント（URL か @ID・1行1件・50件まで）"
    )
    select = form.select_one('select[name="operator_label"]')
    assert select.has_attr("required")
    assert form.find("label", attrs={"for": select["id"]}).get_text() == "運営"
    assert [option["value"] for option in select.select("option")] == ["", *OWN_ACCOUNT_OPERATORS]
    assert not form.select('button[value="submit"]')


def test_account_check_results_counts_links_and_no_writes(
    intake: tuple[TestClient, ImportPg],
) -> None:
    client, pg = intake
    pg.accounts = {
        "@EXISTING": {"operator_label": "ベクトル九州"},
        "unlabelled": {"operator_label": None},
    }
    response = post_accounts(client, accounts="@sample\nexisting\nunlabelled\nbad!\nSAMPLE")
    assert response.status_code == 200
    section = BeautifulSoup(response.text, "html.parser").select_one("#import-accounts")
    assert [row.select("td")[2].get_text() for row in section.select("tbody tr")] == [
        "追加します",
        "登録済み（運営: ベクトル九州）",
        "登録済み（運営: 運営なし）",
        "読めません",
        "1 行目と同じ",
    ]
    assert "追加予定 1 件／登録済み 2 件／読めません 1 件／重複 1 件" in section.get_text()
    assert section.select_one('a[href="https://www.tiktok.com/@sample"]').get_text() == "@sample"
    assert (
        "追加すると、このアカウントの動画（取り込み済みのものも含む）は自社の動画として扱われます"
        in section.get_text()
    )
    assert "新しい投稿の毎日の自動取り込みは準備中です" in section.get_text()
    assert section.select_one('button[value="submit"]')
    assert not any("INSERT INTO vl_accounts" in query for query, _ in pg.calls)
    assert any(s["user_email"] == _EMAIL and s["user_role"] == "user" for s in pg.sessions)


@pytest.mark.parametrize("admin", [False, True])
def test_account_submit_calls_repository_and_preserves_existing(
    intake: tuple[TestClient, ImportPg], monkeypatch: pytest.MonkeyPatch, admin: bool
) -> None:
    client, pg = intake
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", _EMAIL if admin else "")
    legacy = {"operator_label": "イニシャル", "display_name": "既存名"}
    pg.accounts["@EXISTING"] = legacy.copy()
    original = AccountAddRepository.add
    with patch.object(AccountAddRepository, "add", autospec=True, side_effect=original) as add:
        response = post_accounts(client, accounts="sample\nexisting\nSAMPLE", action="submit")
    assert response.status_code == 200
    assert add.call_count == 1
    assert add.call_args.kwargs["user_email"] == _EMAIL
    assert add.call_args.kwargs["user_role"] == ("admin" if admin else "user")
    assert add.call_args.kwargs["request_id"]
    assert "1 件追加しました／1 件は登録済みでした" in response.text
    assert "追加しました</td>" in response.text
    assert pg.accounts == {
        "@EXISTING": legacy,
        "sample": {"operator_label": "NewsTV", "added_by": _EMAIL},
    }
    assert (
        "0 件追加しました／2 件は登録済みでした"
        in post_accounts(client, accounts="sample\nexisting", action="submit").text
    )


@pytest.mark.parametrize("action", ["check", "submit"])
@pytest.mark.parametrize(
    "fields,message,field",
    [
        ({"operator_label": "other"}, "運営を選択してください。", "operator_label"),
        ({"operator_label": ""}, "運営を選択してください。", "operator_label"),
        ({"accounts": "\n".join(["sample"] * 51)}, "1回50件までです。", "accounts"),
        ({"accounts": ""}, "TikTok アカウントを入力してください。", "accounts"),
        ({"accounts": " \n\t "}, "TikTok アカウントを入力してください。", "accounts"),
    ],
)
def test_account_errors_stay_near_fields_without_writing(
    intake: tuple[TestClient, ImportPg],
    action: str,
    fields: dict[str, str],
    message: str,
    field: str,
) -> None:
    client, pg = intake
    response = post_accounts(client, action=action, **fields)
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    control = soup.select_one(f'#import-accounts [name="{field}"]')
    assert control["aria-invalid"] == "true"
    alert = soup.find(id=control["aria-describedby"])
    assert alert["role"] == "alert" and alert.get_text() == message
    assert not pg.accounts and not any("INSERT INTO vl_accounts" in q for q, _ in pg.calls)


@pytest.mark.parametrize("action", ["check", "submit"])
def test_account_values_and_existing_operator_are_escaped(
    intake: tuple[TestClient, ImportPg], action: str
) -> None:
    client, pg = intake
    value = '</textarea><script>alert("account")</script>'
    pg.accounts["existing"] = {"operator_label": "<script>operator</script>"}
    response = post_accounts(client, accounts=value + "\nexisting", action=action)
    soup = BeautifulSoup(response.text, "html.parser")
    section = soup.select_one("#import-accounts")
    assert not section.select("script")
    assert section.select_one("textarea").get_text() == value + "\nexisting"
    assert "&lt;script&gt;" in str(section)
    assert value in section.get_text() and "<script>operator</script>" in section.get_text()


@pytest.mark.parametrize("headers", [{}, {"origin": "https://evil.test"}])
def test_account_origin_is_checked_before_database(
    intake: tuple[TestClient, ImportPg], headers: dict[str, str]
) -> None:
    client, pg = intake
    response = client.post(PATH, headers=headers, data={"action": "submit", "accounts": "sample"})
    assert response.status_code == 403 and not pg.calls


@pytest.mark.parametrize(
    "headers,content,status",
    [
        ({**ORIGIN, "content-type": "application/json"}, "{}", 415),
        (
            {**ORIGIN, "content-type": "application/x-www-form-urlencoded"},
            "accounts=a&accounts=b",
            400,
        ),
        (
            {**ORIGIN, "content-type": "application/x-www-form-urlencoded"},
            "accounts=sample&user_email=forged",
            400,
        ),
    ],
)
def test_account_form_boundary_checks(
    intake: tuple[TestClient, ImportPg], headers: dict[str, str], content: str, status: int
) -> None:
    client, pg = intake
    assert client.post(PATH, headers=headers, content=content).status_code == status
    assert not pg.calls
