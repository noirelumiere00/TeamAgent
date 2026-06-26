"""check_openclaw_config の不変条件検査テスト（依存ゼロ・純関数）。"""

from __future__ import annotations

import importlib.util
import pathlib

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "check_openclaw_config.py"


def _load() -> object:
    spec = importlib.util.spec_from_file_location("check_openclaw_config", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _load()


# ── parse_slack_policy ────────────────────────────────────────────
def test_parse_basic_json5() -> None:
    text = """
    channels: {
      slack: {
        // コメント行
        groupPolicy: "open",
        dmPolicy: "open",
        allowFrom: [
          "*", // 全社内開放
        ],
      },
    }
    """
    p = m.parse_slack_policy(text)
    assert p["dmPolicy"] == "open"
    assert p["groupPolicy"] == "open"
    assert p["allowFrom"] == ["*"]


def test_parse_preserves_url_in_comments_and_values() -> None:
    # `//` を含む URL を行コメント除去で壊さない
    text = 'a: "https://connect.newstv.co.jp/cb", dmPolicy: "allowlist", allowFrom: ["U1"]'
    p = m.parse_slack_policy(text)
    assert p["dmPolicy"] == "allowlist"
    assert p["allowFrom"] == ["U1"]


def test_parse_absent_allowfrom_is_none() -> None:
    p = m.parse_slack_policy('dmPolicy: "open"')
    assert p["allowFrom"] is None


# ── check_slack_policy ────────────────────────────────────────────
def test_open_with_wildcard_ok() -> None:
    assert (
        m.check_slack_policy({"dmPolicy": "open", "groupPolicy": "open", "allowFrom": ["*"]}) == []
    )


def test_open_without_wildcard_violates() -> None:
    # 今回の事故クラス：open なのに "*" 無し
    errs = m.check_slack_policy(
        {"dmPolicy": "open", "groupPolicy": "open", "allowFrom": ["U09CX1CCBLN"]}
    )
    assert errs and any('"*"' in e for e in errs)


def test_open_with_none_allowfrom_violates() -> None:
    errs = m.check_slack_policy({"dmPolicy": "open", "groupPolicy": "open", "allowFrom": None})
    assert errs


def test_empty_allowfrom_violates() -> None:
    errs = m.check_slack_policy({"dmPolicy": "allowlist", "groupPolicy": "open", "allowFrom": []})
    assert errs and any("全拒否" in e for e in errs)


def test_allowlist_with_users_ok() -> None:
    assert (
        m.check_slack_policy(
            {"dmPolicy": "allowlist", "groupPolicy": "open", "allowFrom": ["U1", "U2"]}
        )
        == []
    )


def test_invalid_dmpolicy_violates() -> None:
    errs = m.check_slack_policy({"dmPolicy": "everyone", "groupPolicy": "open", "allowFrom": ["*"]})
    assert errs and any("dmPolicy 不正値" in e for e in errs)


def test_invalid_grouppolicy_violates() -> None:
    errs = m.check_slack_policy({"dmPolicy": "open", "groupPolicy": "everyone", "allowFrom": ["*"]})
    assert errs and any("groupPolicy 不正値" in e for e in errs)


def test_missing_dmpolicy_violates() -> None:
    errs = m.check_slack_policy({"dmPolicy": None, "groupPolicy": "open", "allowFrom": ["*"]})
    assert errs and any("dmPolicy" in e for e in errs)


# ── 実リポジトリの config に対する E2E ─────────────────────────────
def test_real_repo_config_passes() -> None:
    # 現行 config（allowFrom=["*"]）は PASS する＝退行検知
    cfg = _REPO / "infra" / "openclaw" / "openclaw.config.json5"
    assert m.main(str(cfg)) == 0
