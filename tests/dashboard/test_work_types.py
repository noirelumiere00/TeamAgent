"""作業束（調べる/作る/整える/秘書）マッピングの網羅・畳み込み・fail-open。

分類は「定数表だけ」で決まる（LLM も SQL も使わない）。ここでは
1. factory 登録済みの MCP tool が **1つ残らず** 表に載っていること（新ツールを足したら赤）
2. 指示で確定した中核マッピングが一字一句そのままであること
3. 未知ツールが必ず「その他」へ落ちること（fail-open）
4. 集計の畳み込みと割合計算
を固定する。
"""

from __future__ import annotations

import ast
import pathlib
import re
from typing import Any

import pytest

from teamagent.dashboard.queries import (
    WORK_TYPE_ASSIST,
    WORK_TYPE_BY_TOOL,
    WORK_TYPE_CREATE,
    WORK_TYPE_INVESTIGATE,
    WORK_TYPE_ORDER,
    WORK_TYPE_ORGANIZE,
    WORK_TYPE_OTHER,
    work_type_breakdown,
    work_type_of,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SKILLS_DIR = _REPO_ROOT / "src" / "teamagent" / "skills"
_FACTORY = _REPO_ROOT / "src" / "teamagent" / "orchestrator" / "factory.py"


def _skill_tool_names() -> dict[str, str]:
    """``src/teamagent/skills/*/skill.py`` から (クラス名 → tool 名) を静的に集める。

    import は一切しない（media/gemini 等の重い依存を CI に持ち込まないため）。
    usage_events.skill には mcp_gateway.server が tool 名をそのまま書くので、
    ここで集める ``name: ClassVar[str] = "..."`` が管理画面の分類対象そのもの。
    """
    found: dict[str, str] = {}
    for path in sorted(_SKILLS_DIR.glob("*/skill.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and stmt.target.id == "name"
                    and "ClassVar" in ast.unparse(stmt.annotation)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                ):
                    found[node.name] = stmt.value.value
    return found


def _registered_tool_names() -> set[str]:
    """build_production_tools が ToolSpec に載せる Skill クラスの tool 名。"""
    factory_src = _FACTORY.read_text(encoding="utf-8")
    return {
        tool
        for cls, tool in _skill_tool_names().items()
        if re.search(r"\b" + re.escape(cls) + r"\b", factory_src)
    }


def test_skill_scan_finds_the_known_tools() -> None:
    """スキャン自体が壊れて 0 件になり、網羅テストが空振りするのを防ぐ番人。"""
    registered = _registered_tool_names()
    assert len(registered) >= 40
    # 今日追加された新ツール 4 本が確かに factory 登録済みとして拾えている。
    assert {"slack_summary", "attachment_assist", "video_capture", "web_research"} <= registered


def test_every_registered_tool_has_a_work_type() -> None:
    """factory 登録済みツールに未分類が 1 つも無い（新ツール追加時にここが赤くなる）。"""
    unclassified = sorted(_registered_tool_names() - set(WORK_TYPE_BY_TOOL))
    assert unclassified == [], (
        "作業束の未分類ツール: " + ", ".join(unclassified) + " — queries.py の表へ追加する"
    )


def test_mapping_matches_the_specified_core() -> None:
    """指示で確定した中核マッピングを一字一句で固定する。"""
    expected = {
        WORK_TYPE_INVESTIGATE: {
            "search",
            "web_research",
            "x_voice_search",
            "x_needs_mining",
            "search_surface_check",
            "tiktok_search",
            "tiktok_comment_mining",
            "video_algorithm",
            "video_analysis",
        },
        WORK_TYPE_CREATE: {
            "proposal_draft",
            "proposal_review",
            "proposal_builder_submit",
            "proposal_builder_status",
            "mail_draft",
            "mail_reply",
            "video_capture",
        },
        WORK_TYPE_ORGANIZE: {
            "slack_summary",
            "attachment_assist",
            "clientkarte",
            "knowledge_deliver",
            "knowledge_search_url",
        },
        WORK_TYPE_ASSIST: {
            "mail_summary",
            "mail_followup",
            "mail_to_internal_context",
            "calendar_event",
            "calendar_freebusy",
            "schedule_propose",
            "morning_digest",
            "oauth_connect",
        },
    }
    for work_type, tools in expected.items():
        for tool in tools:
            assert work_type_of(tool) == work_type, tool


@pytest.mark.parametrize("tool", ["not_a_real_tool", "run_agent", "chitchat", "", None])
def test_unknown_tool_falls_back_to_other(tool: str | None) -> None:
    """未知ツール・空・None は例外を投げず「その他」へ落ちる（fail-open）。"""
    assert work_type_of(tool) == WORK_TYPE_OTHER


def test_work_type_order_ends_with_other() -> None:
    assert WORK_TYPE_ORDER[-1] == WORK_TYPE_OTHER
    assert len(WORK_TYPE_ORDER) == 5


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]], executed: list[Any]) -> None:
        self._rows = rows
        self._executed = executed

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._executed.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.executed: list[Any] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows, self.executed)


def test_work_type_breakdown_folds_skills_and_computes_share() -> None:
    conn = _FakeConn(
        [
            {"skill": "search", "n": 6, "cost_usd": 0.06},
            {"skill": "web_research", "n": 2, "cost_usd": 0.02},
            {"skill": "mail_draft", "n": 1, "cost_usd": 0.01},
            {"skill": "slack_summary", "n": 1, "cost_usd": 0.01},
        ]
    )
    rows = work_type_breakdown(conn, 7)
    by_type = {r["work_type"]: r for r in rows}
    assert by_type[WORK_TYPE_INVESTIGATE]["requests"] == 8
    assert by_type[WORK_TYPE_INVESTIGATE]["cost_usd"] == pytest.approx(0.08)
    assert by_type[WORK_TYPE_INVESTIGATE]["share"] == pytest.approx(0.8)
    assert by_type[WORK_TYPE_CREATE]["requests"] == 1
    assert by_type[WORK_TYPE_ORGANIZE]["requests"] == 1
    # 0 件の中核束も行として残る（バーが消えない）。
    assert by_type[WORK_TYPE_ASSIST]["requests"] == 0
    assert by_type[WORK_TYPE_ASSIST]["share"] == 0.0
    # 未知ツールが無ければ「その他」の行は出さない。
    assert WORK_TYPE_OTHER not in by_type
    # 並び順は WORK_TYPE_ORDER のまま。
    assert [r["work_type"] for r in rows] == list(WORK_TYPE_ORDER[:4])


def test_work_type_breakdown_surfaces_unknown_tools_as_other() -> None:
    conn = _FakeConn(
        [
            {"skill": "search", "n": 1, "cost_usd": 0.01},
            {"skill": "brand_new_tool_not_in_table", "n": 3, "cost_usd": 0.03},
        ]
    )
    rows = work_type_breakdown(conn, 30)
    by_type = {r["work_type"]: r for r in rows}
    assert by_type[WORK_TYPE_OTHER]["requests"] == 3
    assert by_type[WORK_TYPE_OTHER]["share"] == pytest.approx(0.75)
    assert rows[-1]["work_type"] == WORK_TYPE_OTHER


def test_work_type_breakdown_handles_empty_and_null_cost() -> None:
    conn = _FakeConn([])
    rows = work_type_breakdown(conn, 7)
    assert [r["requests"] for r in rows] == [0, 0, 0, 0]
    assert all(r["share"] == 0.0 for r in rows)

    conn2 = _FakeConn([{"skill": "search", "n": 2, "cost_usd": None}])
    rows2 = work_type_breakdown(conn2, 7)
    assert rows2[0]["cost_usd"] == 0.0


def test_work_type_breakdown_passes_days_as_bound_parameter() -> None:
    conn = _FakeConn([])
    work_type_breakdown(conn, 30)
    sql, params = conn.executed[0]
    assert "GROUP BY skill" in sql
    assert "days')::interval" in sql
    assert params == ["30"]
