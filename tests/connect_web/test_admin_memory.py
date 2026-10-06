"""connect-web /admin/memory（本人メモの管理者閲覧・M6）。

固定すること（設計 §10b.6）:
- 本人メモ専用の allowlist（PERSONAL_MEMORY_ADMIN_EMAILS）。利用状況画面の CONNECT_ADMIN_EMAILS と共用しない
- 空・未設定は誰も見られない（利用状況画面のように小俣さんへ倒さない）・管理者でなければ 404
- 閲覧の失敗（監査の失敗を含む）は 503 で何も表示しない
- 内容はエスケープ・キャッシュさせない
- v1 の管理者はちょうど 1 名（terraform の validation）
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from teamagent.adapters import personal_memory_admin as pma
from teamagent.connect_web.app import create_app
from tests.connect_web.test_admin_usage import _OTHER, _OWNER, _config, _cookies, _FakePg

TEAM = "T0123456789"
USER = "U0123456789"
ROOT = Path(__file__).resolve().parents[2]


class _FakeAdmin:
    calls: ClassVar[list[dict[str, Any]]] = []
    fail = False

    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    def view(self, **kwargs: Any) -> list[pma.AdminRow]:
        type(self).calls.append(kwargs)
        if type(self).fail:
            raise pma.PersonalMemoryAdminError("db_failed")
        return [
            pma.AdminRow(1, "user", "返事は<script>alert(1)</script>短め", None),
            pma.AdminRow(2, "memory", "花王の案件が多い", None),
        ]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _FakeAdmin.calls = []
    _FakeAdmin.fail = False
    monkeypatch.setattr(pma, "PersonalMemoryAdmin", _FakeAdmin)
    monkeypatch.setenv("SLACK_TEAM_ID", TEAM)
    monkeypatch.setenv("PERSONAL_MEMORY_ADMIN_EMAILS", _OWNER)
    return TestClient(create_app(search_config=_config(), admin_pg=_FakePg()))


def test_only_the_memory_admin_can_open_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 利用状況画面の管理者でも、本人メモの allowlist に無ければ 404（共用しない）
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", _OTHER)
    assert client.get("/admin/memory", cookies=_cookies(_OTHER)).status_code == 404
    assert client.get("/admin/memory", cookies=_cookies(_OWNER)).status_code == 200
    unauth = client.get("/admin/memory", follow_redirects=False)
    assert unauth.status_code == 303


def test_empty_allowlist_lets_nobody_in(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PERSONAL_MEMORY_ADMIN_EMAILS", "")
    assert client.get("/admin/memory", cookies=_cookies(_OWNER)).status_code == 404
    assert _FakeAdmin.calls == []


def test_form_then_view_escapes_and_is_not_cached(client: TestClient) -> None:
    form = client.get("/admin/memory", cookies=_cookies(_OWNER))
    assert form.headers["cache-control"] == "no-store"
    assert 'name="user"' in form.text and _FakeAdmin.calls == []
    page = client.get(f"/admin/memory?user={USER.lower()}", cookies=_cookies(_OWNER))
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "<script>" not in page.text and "&lt;script&gt;" in page.text
    assert "花王の案件が多い" in page.text
    assert _FakeAdmin.calls == [{"admin_email": _OWNER, "team_id": TEAM, "slack_user_id": USER}]


def test_bad_target_is_400_without_touching_the_db(client: TestClient) -> None:
    page = client.get("/admin/memory?user=someone@example.com", cookies=_cookies(_OWNER))
    assert page.status_code == 400
    assert _FakeAdmin.calls == []


def test_failure_shows_nothing(client: TestClient) -> None:
    _FakeAdmin.fail = True
    page = client.get(f"/admin/memory?user={USER}", cookies=_cookies(_OWNER))
    assert page.status_code == 503
    assert "花王" not in page.text and "<table" not in page.text
    assert "何も表示していません" in page.text


def test_terraform_allows_exactly_one_admin() -> None:
    tf = (ROOT / "infra/terraform/connect_web.tf").read_text(encoding="utf-8")
    block = tf[tf.index('variable "personal_memory_admin_emails"') :]
    block = block[: block.index("\n}\n")]
    assert 'default     = ""' in block
    pattern = re.search(r'can\(regex\("(.+?)", var\.personal_memory_admin_emails\)\)', block)
    assert pattern is not None
    regex = re.compile(pattern.group(1).replace("[:space:]", r"\s"))
    assert regex.fullmatch("s-komata@vectorinc.co.jp")
    assert not regex.fullmatch("s-komata@vectorinc.co.jp,a@vectorinc.co.jp")
    assert '"PERSONAL_MEMORY_ADMIN_EMAILS", value = var.personal_memory_admin_emails' in tf
