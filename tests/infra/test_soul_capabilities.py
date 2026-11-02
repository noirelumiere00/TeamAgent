"""本番の公開範囲と SOUL の案内を、外部接続なしの固定 env で照合する。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from teamagent.mcp_gateway.allowlist import email_allowed
from teamagent.orchestrator import factory
from teamagent.skills._shared.rollout import rollout_allowed
from teamagent.skills.attachment_assist.skill import _permalink_allowed
from teamagent.skills.media_case_search import skill as media_case_search
from teamagent.skills.omiyage_report import sales25
from teamagent.skills.proposal_builder.skill import submit_allowed

ROOT = Path(__file__).resolve().parents[2]


def test_soul_capabilities_match_production_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = json.loads(
        (Path(__file__).parent / "fixtures/production_capabilities.json").read_text()
    )
    # 環境由来の認証・フラグを持ち込まない。実 Skill の run/外部 API は呼ばない。
    monkeypatch.setattr(os, "environ", dict(snapshot["mcp_env"]))
    search = SimpleNamespace(pgvector=object(), embedder=object())
    monkeypatch.setattr(factory, "_build_search_skill", lambda: search)
    monkeypatch.setattr(factory, "_build_token_store", object)
    monkeypatch.setattr(factory, "_build_slack_store", object)
    monkeypatch.setattr(media_case_search, "_production_skill", None)
    expected = snapshot["tools"]
    registered = {spec.name for spec in factory.build_production_tools()}
    assert registered == {name for name, tool in expected.items() if tool["availability"] == "全員"}
    # OFF の登録条件も検査し、追加ツールの案内漏れを拾う。
    for tool in expected.values():
        if tool["flag"]:
            monkeypatch.setenv(tool["flag"], "1")
    assert {spec.name for spec in factory.build_production_tools()} == set(expected)
    for flag in {tool["flag"] for tool in expected.values()} - {None}:
        monkeypatch.setenv(flag, "0")
        assert {spec.name for spec in factory.build_production_tools()} == {
            name for name, tool in expected.items() if tool["flag"] != flag
        }, flag
        monkeypatch.setenv(flag, "1")
    monkeypatch.setattr(os, "environ", dict(snapshot["mcp_env"]))

    soul = (ROOT / "infra/openclaw/SOUL.md").read_text()
    config = (ROOT / "infra/openclaw/openclaw.config.json5").read_text()
    included = config.split("toolFilter: {", 1)[1].split("exclude:", 1)[0]
    exposed = set(re.findall(r'"([a-z_]+)"', included))
    scope = soul.split("## 現在の公開範囲（他節より優先）\n", 1)[1].split("\n## ", 1)[0]
    preparing = {
        name
        for line in scope.splitlines()
        if "準備中です。" in line
        for name in re.findall(r"`([a-z_]+)`", line)
    }
    # 非公開ツールは内部名を案内せず、日本語の機能説明だけを残す。
    assert preparing == (set(expected) - registered) & exposed
    routing = soul.replace(scope, "")
    guided = set(re.findall(r"`([a-z_]+)`", routing)) & set(expected)
    assert guided == registered, "準備中への実行案内、または ON ツールの案内漏れ"

    assert registered <= exposed
    assert snapshot["openclaw_env"]["SLACK_DM_ALLOWLIST"] == "*"
    for name, phrases in {
        "digest_settings": ("朝のサマリーを止めて", "予定の直前通知を止めて"),
        "tiktok_search": ("今すぐ", "単一媒体"),
    }.items():
        assert any(
            f"`{name}`" in line and all(phrase in line for phrase in phrases)
            for line in routing.splitlines()
        )
    assert "他人のカレンダー変更、通知設定の変更" not in soul
    assert "利用対象外は再試行せず原文を伝える" in scope

    # 同じ空文字でも逆の意味。実際の各利用ゲートも固定する。
    member = {"identity_verified": True, "user_email": "member@example.test"}
    assert submit_allowed(member) and _permalink_allowed(member)
    assert not submit_allowed({}) and not _permalink_allowed({})
    for name in (
        "WEB_RESEARCH_ALLOWED_EMAILS",
        "MEETING_PREP_ALLOWED_EMAILS",
        "X_RESEARCH_ALLOWED_EMAILS",
        "SEARCH_SURFACE_ALLOWED_EMAILS",
        "COMMENT_MINING_ALLOWED_EMAILS",
    ):
        assert rollout_allowed(name, member["user_email"])
    assert not email_allowed(member["user_email"], set())
    monkeypatch.setenv("PROPOSAL_BUILDER_ALLOWED_EMAILS", "")
    assert not submit_allowed(member)
    monkeypatch.setenv("PROPOSAL_BUILDER_ALLOWED_EMAILS", "pilot@example.test")
    assert not submit_allowed(member)
    assert submit_allowed({**member, "user_email": "pilot@example.test"})

    verified = {
        **member,
        "verified_slack_user_id": "U_TEST_MEMBER",
        "verified_slack_team_id": "T_TEST_TEAM",
    }
    assert not sales25.enabled(verified)
    assert sales25.enabled({**verified, "user_email": "pilot@example.test"})
    assert "25P版は一部メンバー向けです。標準版で作成します" in scope
    assert "標準版への了承を得てから呼ぶ" in scope
