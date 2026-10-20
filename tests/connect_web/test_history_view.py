"""connect-web /admin/history と /app/history（質問と回答の履歴・上司 5）。

固定すること（設計 history_view_design_v1_20261014.md・2026-10-14 小俣さん決裁）:
- 管理者は HISTORY_ADMIN_EMAILS のちょうど 1 名（小俣さん）。空は誰も見られない・管理者でなければ 404
- 利用状況画面・本人メモの allowlist とは共用しない
- **Slack を読む前に監査を確定**。監査が失敗したら 503 で Slack に触れない
- bot token が無い・ワークスペースが無いときは 503（準備中）で何も読まない
- 本人画面はログインした本人の DM だけ（クエリの ?user= は読まない）・本人の閲覧は監査しない
- 本文はエスケープし、一回限りのリンクは伏せ、http(s) 以外はリンクにしない・キャッシュさせない
- アクセスログに「誰の記録を見たか」を残さない
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from teamagent.adapters import dm_history as dh
from teamagent.adapters import history_audit as ha
from teamagent.connect_web import app as connect_app
from teamagent.connect_web.app import create_app, slack_text_to_html
from tests.connect_web.test_admin_usage import _OTHER, _OWNER, _config, _cookies, _FakePg

TEAM = "T0123456789"
TARGET = "U0AAAAAAA1"
OWN = "U0SELF0001"
ROOT = Path(__file__).resolve().parents[2]


class _Log:
    events: ClassVar[list[tuple[str, Any]]] = []


class _FakeAudit:
    fail = False

    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    def record(self, **kwargs: Any) -> None:
        _Log.events.append(("audit", kwargs))
        if type(self).fail:
            raise ha.HistoryAuditError("db_failed")


class _FakeReader:
    missing_token = False
    fail_read = False
    unknown_email = False

    @classmethod
    def from_env(cls) -> _FakeReader:
        if cls.missing_token:
            raise dh.DmHistoryError("no_bot_token")
        return cls()

    def list_users(self) -> list[dh.DmUser]:
        _Log.events.append(("list", None))
        return [dh.DmUser(TARGET, "稲上 <script>")]

    def user_id_for_email(self, email: str) -> str:
        _Log.events.append(("lookup", email))
        if type(self).unknown_email:
            raise dh.DmHistoryError("users_not_found")
        return OWN

    def read(self, slack_user_id: str, *, days: int) -> dh.DmRead:
        _Log.events.append(("read", (slack_user_id, days)))
        if type(self).fail_read:
            raise dh.DmHistoryError("ratelimited")
        return dh.DmRead(
            messages=(
                dh.DmMessage(1_791_900_000.0, "user", "提案書を<script>alert(1)</script>探して"),
                dh.DmMessage(
                    1_791_900_010.0,
                    "aico",
                    "連携して: <https://connect.newstv.co.jp/oauth2/start/SECRETSTATE|Google を連携>\n"
                    "資料: <https://drive.google.com/file/d/1|花王の提案書>",
                ),
            ),
            truncated=False,
        )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _Log.events = []
    _FakeAudit.fail = False
    _FakeReader.missing_token = False
    _FakeReader.fail_read = False
    _FakeReader.unknown_email = False
    monkeypatch.setattr(ha, "HistoryAudit", _FakeAudit)
    monkeypatch.setattr(dh, "DmHistoryReader", _FakeReader)
    monkeypatch.setenv("SLACK_TEAM_ID", TEAM)
    monkeypatch.setenv("HISTORY_ADMIN_EMAILS", _OWNER)
    monkeypatch.delenv("PERSONAL_MEMORY_ADMIN_EMAILS", raising=False)
    return TestClient(create_app(search_config=_config(), admin_pg=_FakePg()))


def _kinds() -> list[str]:
    return [kind for kind, _ in _Log.events]


# ── 管理者画面 ─────────────────────────────────────────────────────────────


def test_only_the_history_admin_can_open_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 利用状況画面・本人メモの管理者でも、履歴の allowlist に無ければ 404（共用しない）
    monkeypatch.setenv("CONNECT_ADMIN_EMAILS", _OTHER)
    monkeypatch.setenv("PERSONAL_MEMORY_ADMIN_EMAILS", _OTHER)
    assert client.get("/admin/history", cookies=_cookies(_OTHER)).status_code == 404
    assert client.get(f"/admin/history?user={TARGET}", cookies=_cookies(_OTHER)).status_code == 404
    assert _Log.events == []
    unauth = client.get("/admin/history", follow_redirects=False)
    assert unauth.status_code == 303
    assert unauth.headers["location"] == "/search/login?next=/admin/history"


def test_empty_allowlist_lets_nobody_in(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HISTORY_ADMIN_EMAILS", "")
    assert client.get(f"/admin/history?user={TARGET}", cookies=_cookies(_OWNER)).status_code == 404
    assert _Log.events == []


def test_list_shows_people_without_reading_or_auditing(client: TestClient) -> None:
    page = client.get("/admin/history", cookies=_cookies(_OWNER))
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert page.headers["x-robots-tag"] == "noindex"
    assert f"/admin/history?user={TARGET}" in page.text
    assert "<script>" not in page.text and "稲上 &lt;script&gt;" in page.text
    assert _kinds() == ["list"]


def test_view_audits_before_reading_and_escapes(client: TestClient) -> None:
    page = client.get(f"/admin/history?user={TARGET.lower()}", cookies=_cookies(_OWNER))
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert _kinds() == ["audit", "read"]
    assert _Log.events[0][1] == {
        "admin_email": _OWNER,
        "team_id": TEAM,
        "slack_user_id": TARGET,
        "days": dh.HISTORY_DAYS_MAX,
    }
    assert _Log.events[1][1] == (TARGET, dh.HISTORY_DAYS_MAX)
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    # 他人の連携リンクは管理者にも出さない。普通のリンクは開ける
    assert "SECRETSTATE" not in page.text and "oauth2/start" not in page.text
    assert dh.SECRET_LINK_MASK in page.text
    assert '<a href="https://drive.google.com/file/d/1"' in page.text


def test_audit_failure_shows_nothing_and_never_reads_slack(client: TestClient) -> None:
    _FakeAudit.fail = True
    page = client.get(f"/admin/history?user={TARGET}", cookies=_cookies(_OWNER))
    assert page.status_code == 503
    assert "何も表示していません" in page.text and "提案書" not in page.text
    assert _kinds() == ["audit"]


def test_missing_bot_token_is_503_before_audit(client: TestClient) -> None:
    _FakeReader.missing_token = True
    page = client.get(f"/admin/history?user={TARGET}", cookies=_cookies(_OWNER))
    assert page.status_code == 503 and "準備中" in page.text
    assert _Log.events == []


def test_missing_team_is_503_before_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLACK_TEAM_ID", "")
    page = client.get(f"/admin/history?user={TARGET}", cookies=_cookies(_OWNER))
    assert page.status_code == 503 and _Log.events == []


def test_bad_target_is_400_without_audit_or_read(client: TestClient) -> None:
    page = client.get("/admin/history?user=someone@example.com", cookies=_cookies(_OWNER))
    assert page.status_code == 400 and _Log.events == []


def test_slack_failure_after_audit_is_503(client: TestClient) -> None:
    _FakeReader.fail_read = True
    page = client.get(f"/admin/history?user={TARGET}", cookies=_cookies(_OWNER))
    assert page.status_code == 503 and _kinds() == ["audit", "read"]


# ── 本人画面 ───────────────────────────────────────────────────────────────


def test_self_view_reads_only_the_logged_in_user(client: TestClient) -> None:
    unauth = client.get("/app/history", follow_redirects=False)
    assert unauth.status_code == 303
    assert unauth.headers["location"] == "/search/login?next=/app/history"
    # 他人の ID を渡されても読まない（本人の email から引いた ID だけ）
    page = client.get(f"/app/history?user={TARGET}", cookies=_cookies(_OTHER))
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert _Log.events == [("lookup", _OTHER), ("read", (OWN, dh.HISTORY_DAYS_MAX))]
    assert "あなたと Aico の会話" in page.text
    assert "SECRETSTATE" not in page.text


def test_self_view_needs_no_admin_and_is_not_audited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HISTORY_ADMIN_EMAILS", "")
    page = client.get("/app/history", cookies=_cookies(_OWNER))
    assert page.status_code == 200 and "audit" not in _kinds()


def test_self_view_unknown_email_and_failures(client: TestClient) -> None:
    _FakeReader.unknown_email = True
    page = client.get("/app/history", cookies=_cookies(_OWNER))
    assert page.status_code == 200 and "見つかりませんでした" in page.text
    _FakeReader.unknown_email = False
    _FakeReader.fail_read = True
    assert client.get("/app/history", cookies=_cookies(_OWNER)).status_code == 503
    _FakeReader.missing_token = True
    assert client.get("/app/history", cookies=_cookies(_OWNER)).status_code == 503


# ── 表示の部品 ─────────────────────────────────────────────────────────────


def test_only_http_links_become_anchors() -> None:
    out = slack_text_to_html(
        "<javascript:alert(1)|押して> <https://a.example/x?a=1&amp;b=2|資料> <@U0123456789> "
        "<#C0123456789|general> <!here> a &lt; b"
    )
    assert '<a href="javascript' not in out and "押して" in out
    # Slack は本文の < > を &lt; &gt; で送る。どちらの形でもタグにならない
    assert slack_text_to_html("&lt;img src=x onerror=alert(1)&gt;") == (
        "&lt;img src=x onerror=alert(1)&gt;"
    )
    assert slack_text_to_html("<img src=x onerror=alert(1)>") == (
        "&lt;img src=x onerror=alert(1)&gt;"
    )
    assert '<a href="https://a.example/x?a=1&amp;b=2" rel="noopener noreferrer"' in out
    assert "@メンバー" in out and "#general" in out and "@here" in out
    # Slack の実体参照は二重にエスケープしない
    assert "a &lt; b" in out and "&amp;lt;" not in out


def test_safe_next_allows_the_history_pages() -> None:
    assert connect_app._safe_next("/app/history") == "/app/history"
    assert connect_app._safe_next("/admin/history") == "/admin/history"
    assert connect_app._safe_next("//evil.example/app/history") == "/app"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"/admin/history?user={TARGET}", "/admin/history?<redacted>"),
        (f"/admin/memory?user={TARGET}", "/admin/memory?<redacted>"),
        ("/admin?user=x@vectorinc.co.jp", "/admin?<redacted>"),
        ("/admin/history", "/admin/history"),
        ("/app/history", "/app/history"),
    ],
)
def test_access_log_does_not_keep_whose_history_was_viewed(path: str, expected: str) -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        "%s %s %s %s %s",
        ("1.2.3.4", "GET", path, "1.1", 200),
        None,
    )
    assert connect_app._RedactAdminUserAccessLog().filter(record)
    assert record.args is not None and record.args[2] == expected  # type: ignore[index]


# ── terraform ─────────────────────────────────────────────────────────────


def test_terraform_allows_exactly_one_history_admin_and_wires_the_bot_token() -> None:
    tf = (ROOT / "infra/terraform/connect_web.tf").read_text(encoding="utf-8")
    block = tf[tf.index('variable "history_admin_emails"') :]
    block = block[: block.index("\n}\n")]
    assert 'default     = ""' in block
    pattern = re.search(r'can\(regex\("(.+?)", var\.history_admin_emails\)\)', block)
    assert pattern is not None
    regex = re.compile(pattern.group(1).replace("[:space:]", r"\s"))
    assert regex.fullmatch("s-komata@vectorinc.co.jp")
    assert not regex.fullmatch("s-komata@vectorinc.co.jp,a@vectorinc.co.jp")
    assert '"HISTORY_ADMIN_EMAILS", value = var.history_admin_emails' in tf
    assert (
        '{ name = "SLACK_BOT_TOKEN", valueFrom = data.aws_secretsmanager_secret.slack_bot.arn }'
        in tf
    )
    iam = tf[tf.index('data "aws_iam_policy_document" "ecs_execution_connect_web_secrets"') :]
    iam = iam[: iam.index("\n}\n")]
    assert "data.aws_secretsmanager_secret.slack_bot.arn" in iam


def test_admin_history_has_shared_navigation_but_self_history_does_not(client: TestClient) -> None:
    admin = client.get("/admin/history", cookies=_cookies(_OWNER))
    assert 'href="/admin"' in admin.text and 'href="/admin/memory"' in admin.text
    assert '<aside class="sidebar"' in admin.text
    assert '<main id="main" class="admin-content">' in admin.text
    assert 'aria-label="会話履歴" aria-current="page"' in admin.text
    assert "'unsafe-inline'" not in admin.headers["content-security-policy"]
    own = client.get("/app/history", cookies=_cookies(_OTHER))
    assert 'aria-label="管理画面"' not in own.text
