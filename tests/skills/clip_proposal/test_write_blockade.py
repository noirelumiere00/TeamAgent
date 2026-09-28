"""安全装置「書込系ゼロ」を **構造で** 固定する（計画 §2-2）。

現状のコードは gmail / calendar / drive / sheets のどれも import していないが、
それは「まだ書いていない」だけで、封鎖として固定されていない。便C 以降に配達や
ドライブ保存を足す担当がうっかり drive adapter を import しても誰も止めない。

ここでは ``skills/clip_proposal/**`` の import を ast で走査し、

1. 禁止 adapter（Google 系・メール送信系・チャンネル投稿系）が 1 つでも現れたら落とす
2. ``SlackClient`` に対して呼んでよいメソッドを allowlist で縛る

を固定する。遅延 import（関数内 import）も走査対象に含める。ここを top-level だけに
すると、``def _deliver(): from teamagent.adapters import gdrive_client`` が素通りする。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[3] / "src/teamagent/skills/clip_proposal"

#: clip_proposal から触ってはいけない adapter（書込系・外部送信系）。
FORBIDDEN_ADAPTERS = frozenset(
    {
        "gmail_client",
        "gcalendar_client",
        "gdrive_client",
        "gdocs_client",
        "gsheets_client",
        "gslides_client",
        "gpeople_client",
        "google_auth",
        "google_oauth_flow",
        "drive_video",
        "oauth_token_store",
        "slack_oauth_flow",
        "slack_channel_ingest_client",
        "report_publish",
    }
)

#: SlackClient に対して呼んでよいメソッド（計画 §2-2 の 4 つ）。
ALLOWED_SLACK_METHODS = frozenset(
    {
        "upload_file",
        "lookup_user_id_by_email",
        "open_dm",
        "download_file_bounded",
    }
)


def _modules() -> list[Path]:
    return sorted(path for path in PACKAGE.glob("*.py"))


def _imported_names(tree: ast.AST) -> set[str]:
    """top-level も関数内も含めた、全 import の「モジュール名の部品」。"""

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.update(node.module.split("."))
            for alias in node.names:
                names.add(alias.name)
    return names


def test_the_package_has_modules_to_scan() -> None:
    """走査対象が 0 件になっていたら、この封鎖テストは何も守っていない。"""

    assert len(_modules()) >= 7


@pytest.mark.parametrize("path", _modules(), ids=lambda path: path.name)
def test_no_write_capable_adapter_is_reachable(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = FORBIDDEN_ADAPTERS & _imported_names(tree)
    assert not found, f"{path.name} imports write-capable adapter(s): {sorted(found)}"


@pytest.mark.parametrize("path", _modules(), ids=lambda path: path.name)
def test_slack_client_methods_stay_inside_the_allowlist(path: Path) -> None:
    """``SlackClient`` を掴んだモジュールは、allowlist 外のメソッドを呼ばない。

    ``chat_postMessage`` / ``conversations_join`` 等が生えた瞬間に赤くする
    （チャンネル投稿は小俣さん本人 DM 限定の規律に反する）。
    """

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    if "SlackClient" not in _imported_names(tree):
        pytest.skip("SlackClient を掴んでいない")

    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    leaked = {
        name
        for name in called
        if name.startswith(("chat_", "conversations_", "files_", "admin_"))
        and name not in ALLOWED_SLACK_METHODS
    }
    assert not leaked, f"{path.name} calls Slack method(s) outside the allowlist: {sorted(leaked)}"


def test_the_scan_sees_a_late_import(tmp_path: Path) -> None:
    """走査が関数内 import を拾うことを、走査器自身に対して確かめる。

    top-level だけを見る実装に戻すと、この 1 本が赤くなる。
    """

    module = tmp_path / "sneaky.py"
    module.write_text(
        "def deliver():\n    from teamagent.adapters import gdrive_client\n"
        "    return gdrive_client\n",
        encoding="utf-8",
    )
    tree = ast.parse(module.read_text(encoding="utf-8"))
    assert "gdrive_client" in _imported_names(tree)
    assert FORBIDDEN_ADAPTERS & _imported_names(tree)
