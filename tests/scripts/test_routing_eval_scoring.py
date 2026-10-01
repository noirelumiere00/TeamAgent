"""scripts/routing_eval の決定的な部分（採点・集計・雛形の検査・代役の応答形式）を固定する。

LLM の呼び出しはしない（課金ゼロ）。scripts/ は package ではないため importlib で読む。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
EVAL_DIR = PROJECT_ROOT / "scripts" / "routing_eval"


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"routing_eval_{name}", EVAL_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scoring = _load("scoring")
make_template = _load("make_template")

PROPS = {
    "calendar_freebusy": {"mode", "relative_day", "_user_context"},
    "calendar_event": {"title", "start", "_user_context"},
    "mail_followup": {"client_name", "lookback_days", "_user_context"},
    "mail_summary": {"client_name", "_user_context"},
    "mail_reply": {"client_name", "_user_context"},
    "search": {"query", "_user_context"},
}


def _row(**kw: Any) -> Any:
    base = {"id": "t-01", "utterance": "u", "expect": "calendar_freebusy"}
    base.update(kw)
    return scoring.CorpusRow.from_json(base)


def _resp(text: str = "", calls: list[tuple[str, dict[str, Any]]] | None = None) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"text": text})
    for name, args in calls or []:
        content.append({"toolUse": {"name": f"teamagent__{name}", "toolUseId": "x", "input": args}})
    return scoring.parse_response(content)


# ── 採点 ─────────────────────────────────────────────────────────────────────


def test_expected_tool_passes() -> None:
    r = scoring.score(_row(), _resp(calls=[("calendar_freebusy", {"mode": "free"})]), PROPS)
    assert r["pass"] and r["selected_tool"] == "calendar_freebusy" and not r["no_tool"]


def test_wrong_tool_fails_with_reason() -> None:
    r = scoring.score(_row(), _resp(calls=[("mail_followup", {})]), PROPS)
    assert not r["pass"]
    assert any("選択違い" in x for x in r["fail_reasons"])


def test_forbid_checked_on_every_call_in_turn() -> None:
    row = _row(forbid=["calendar_event"])
    r = scoring.score(row, _resp(calls=[("calendar_freebusy", {}), ("calendar_event", {})]), PROPS)
    assert r["forbid_violation"] and not r["pass"]


def test_arg_rule_equal_violation() -> None:
    row = _row(arg_rules={"mode": "must_equal:agenda"})
    r = scoring.score(row, _resp(calls=[("calendar_freebusy", {"mode": "free"})]), PROPS)
    assert r["arg_violation"] and not r["pass"]
    r = scoring.score(row, _resp(calls=[("calendar_freebusy", {"mode": "agenda"})]), PROPS)
    assert r["pass"]


def test_arg_rule_absent_treats_empty_string_as_absent() -> None:
    row = _row(
        expect="__ask_back__", alt_ok=["mail_followup"], arg_rules={"client_name": "must_be_absent"}
    )
    assert scoring.score(row, _resp(calls=[("mail_followup", {"client_name": ""})]), PROPS)["pass"]
    r = scoring.score(row, _resp(calls=[("mail_followup", {"client_name": "返信必要"})]), PROPS)
    assert r["arg_violation"] and not r["pass"]


def test_arg_rule_present() -> None:
    row = _row(expect="mail_summary", arg_rules={"client_name": "must_be_present"})
    assert not scoring.score(row, _resp(calls=[("mail_summary", {})]), PROPS)["pass"]
    assert scoring.score(row, _resp(calls=[("mail_summary", {"client_name": "アサヒ"})]), PROPS)[
        "pass"
    ]


def test_arg_rule_only_applies_to_tools_owning_the_field() -> None:
    # search は client_name を持たないので、must_be_present の検査対象外（選択違いとして別に落ちる）
    row = _row(expect="mail_summary", arg_rules={"client_name": "must_be_present"})
    r = scoring.score(row, _resp(calls=[("search", {"query": "x"})]), PROPS)
    assert not r["arg_violation"]
    assert any("選択違い" in x for x in r["fail_reasons"])


def test_ask_back_row_passes_on_question_without_tool() -> None:
    row = _row(expect="__ask_back__", alt_ok=["mail_followup"])
    r = scoring.score(row, _resp(text="どの取引先のメールですか？"), PROPS)
    assert r["pass"] and r["no_tool"] and r["response_kind"] == "ask_back"
    assert not r["false_refusal"]


def test_ask_back_row_fails_on_refusal() -> None:
    row = _row(expect="__ask_back__", alt_ok=["mail_followup"])
    r = scoring.score(row, _resp(text="メールの確認はできません。"), PROPS)
    assert not r["pass"] and r["response_kind"] == "refusal"
    # 期待が聞き返しの行は「誤拒否」には数えない（不合格にはなる）
    assert not r["false_refusal"]


def test_false_refusal_counted_when_tool_expected() -> None:
    r = scoring.score(_row(), _resp(text="カレンダーにはアクセスできません。"), PROPS)
    assert r["false_refusal"] and r["no_tool"] and not r["pass"]


def test_leaked_internal_names_is_informational() -> None:
    r = scoring.score(
        _row(),
        _resp(text="`calendar_freebusy` を呼びます", calls=[("calendar_freebusy", {})]),
        PROPS,
    )
    assert r["pass"]
    assert r["leaked_internal_names"] == ["calendar_freebusy"]
    # 単語の一部（researcher 等）は数えない
    assert scoring.leaked_names("researcher に聞きます", {"search"}) == []


def test_classify_text() -> None:
    assert scoring.classify_text("それは対応していません") == "refusal"
    assert scoring.classify_text("どちらの件でしょうか？") == "ask_back"
    assert scoring.classify_text("新商品の名前を教えてください。") == "ask_back"
    assert scoring.classify_text("了解です") == "text"


def test_rescore_uses_current_rules_and_keeps_raw_fields() -> None:
    rec = {
        "case_id": "t-01",
        "utterance": "u",
        "expect": "calendar_freebusy",
        "alt_ok": [],
        "forbid": ["calendar_event"],
        "arg_rules": {},
        "text": "",
        "tool_calls": [{"name": "calendar_event", "input": {}}],
        "pass": True,  # 古い採点の値（上書きされるべき）
        "usage": {"inputTokens": 1},
    }
    out = scoring.rescore(rec, PROPS)
    assert out["pass"] is False and out["forbid_violation"] is True
    assert out["usage"] == {"inputTokens": 1}
    assert scoring.rescore({"error": "x"}, PROPS) == {"error": "x"}


# ── コーパスと正本の読み込み ───────────────────────────────────────────────────


def test_fixed_sets_are_read_from_catalog_test() -> None:
    frozen, unwired = scoring.load_fixed_sets(
        PROJECT_ROOT / "tests/skills/test_routing_descriptions_catalog.py"
    )
    assert "freebusy-01" in frozen and "mailnc-04" in frozen
    assert len(frozen) >= 18
    assert "vapproval-01" in unwired


def test_corpus_loads_and_frozen_rows_exist() -> None:
    rows = scoring.load_corpus(PROJECT_ROOT / "tests/routing/catalog_routing_corpus.jsonl")
    frozen, unwired = scoring.load_fixed_sets(
        PROJECT_ROOT / "tests/skills/test_routing_descriptions_catalog.py"
    )
    ids = {r.id for r in rows}
    assert frozen <= ids
    assert set(unwired) <= ids


def test_corpus_rejects_duplicate_ids(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    p.write_text(
        '{"id":"a","utterance":"x","expect":"search"}\n{"id":"a","utterance":"y","expect":"search"}\n'
    )
    with pytest.raises(ValueError):
        scoring.load_corpus(p)


# ── 集計と必須条件 ─────────────────────────────────────────────────────────────


def _rec(
    case: str, passed: bool, *, frozen: bool = False, unwired: bool = False, **kw: Any
) -> dict[str, Any]:
    base = {
        "case_id": case,
        "expect": "search",
        "alt_ok": [],
        "frozen": frozen,
        "unwired": unwired,
        "pass": passed,
        "selected_tool": "search" if passed else "mail_summary",
        "no_tool": False,
        "response_kind": "tool",
        "false_refusal": False,
        "forbid_violation": False,
        "arg_violation": False,
        "leaked_internal_names": [],
        "fail_reasons": [] if passed else ["選択違い"],
        "usage": {"inputTokens": 3, "cacheReadInputTokens": 100, "outputTokens": 5},
        "latency_ms": 1000,
    }
    base.update(kw)
    return base


def test_gates_require_frozen_all_pass() -> None:
    s = scoring.summarize(
        [_rec("f1", True, frozen=True), _rec("f1", False, frozen=True), _rec("n1", True)]
    )
    failed = scoring.gates(s)
    assert any("凍結" in x for x in failed)
    s = scoring.summarize([_rec("f1", True, frozen=True), _rec("n1", False)])
    assert scoring.gates(s) == []


def test_gates_require_zero_forbid_and_arg_violations() -> None:
    s = scoring.summarize(
        [_rec("n1", False, forbid_violation=True), _rec("n2", False, arg_violation=True)]
    )
    failed = scoring.gates(s)
    assert any("禁止ツール" in x for x in failed) and any("引数" in x for x in failed)


def test_gates_fail_on_api_errors() -> None:
    s = scoring.summarize(
        [_rec("n1", True), {"case_id": "n2", "error": "ThrottlingException: x", "unwired": False}]
    )
    assert any("API エラー" in x for x in scoring.gates(s))


def test_unwired_rows_excluded_from_rates() -> None:
    s = scoring.summarize([_rec("n1", True), _rec("u1", False, unwired=True)])
    assert s["pass_rate"] == 1.0 and s["unwired_runs"] == 1


def test_compare_cases_reports_direction() -> None:
    a = [_rec("c1", True), _rec("c1", True), _rec("c2", False)]
    b = [_rec("c1", True), _rec("c1", False), _rec("c2", True)]
    diff = {d["case_id"]: d for d in scoring.compare_cases(a, b)}
    assert diff["c1"]["direction"] == "worse" and diff["c1"]["b_pass"] == "1/2"
    assert diff["c2"]["direction"] == "better"


# ── 雛形の検査 ────────────────────────────────────────────────────────────────


def _src_tool(name: str, desc: str = "d") -> dict[str, Any]:
    return {"name": name, "description": desc, "inputSchema": {"type": "object", "properties": {}}}


def _sent_tool(name: str, desc: str = "d") -> dict[str, Any]:
    return {
        "toolSpec": {
            "name": f"teamagent__{name}",
            "description": desc,
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    }


def test_template_rejects_rewritten_tool_definitions() -> None:
    make_template.check_tools([_sent_tool("search")], [_src_tool("search")])
    with pytest.raises(SystemExit):
        make_template.check_tools([_sent_tool("search", "書き換え")], [_src_tool("search")])


def test_template_rejects_tools_dropped_by_filter() -> None:
    with pytest.raises(SystemExit):
        make_template.check_tools(
            [_sent_tool("search")], [_src_tool("search"), _src_tool("clientkarte")]
        )


def test_template_normalizes_only_session_id() -> None:
    body = {
        "system": [
            {
                "text": "Runtime: agent=teamagent | sessionId=a0694d96-a8d5-4111-a361-2ebf147bbdfa | host=x"
            }
        ]
    }
    assert make_template.normalize_runtime_ids(body) == 1
    assert "sessionId=00000000-0000-4000-8000-000000000000" in body["system"][0]["text"]
    assert "host=x" in body["system"][0]["text"]


def test_sentinel_counted_across_messages() -> None:
    msgs = [
        {"role": "user", "content": [{"text": "[t] __S__"}, {"cachePoint": {"type": "default"}}]}
    ]
    assert make_template.count_sentinel(msgs, "__S__") == 1


# ── 代役の応答形式（botocore の event stream 解析で読めること） ─────────────────


def test_capture_stub_event_stream_is_parseable_by_botocore() -> None:
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    stubs = _load("capture_stubs")
    from botocore.eventstream import EventStreamBuffer

    buf = EventStreamBuffer()
    buf.add_data(stubs.converse_stream_tool_use("id1", "teamagent__search", {"query": "x"}))
    events = [(m.headers[":event-type"], json.loads(m.payload)) for m in buf]
    assert [e for e, _ in events] == [
        "messageStart",
        "contentBlockStart",
        "contentBlockDelta",
        "contentBlockStop",
        "messageStop",
        "metadata",
    ]
    assert events[1][1]["start"]["toolUse"]["name"] == "teamagent__search"
    assert json.loads(events[2][1]["delta"]["toolUse"]["input"]) == {"query": "x"}


def test_capture_stub_script_matches_tail_user_messages() -> None:
    pytest.importorskip("starlette")
    pytest.importorskip("mcp")
    stubs = _load("capture_stubs")
    from botocore.eventstream import EventStreamBuffer

    turns = [
        {"user": "おはよう", "assistant": "おはようございます"},
        {
            "user": "資料を探して",
            "tool_call": {"name": "search", "arguments": {"query": "q"}},
            "tool_result": "{}",
            "assistant": "2 件ありました",
        },
    ]

    def kinds(raw: bytes) -> list[str]:
        buf = EventStreamBuffer()
        buf.add_data(raw)
        return [m.headers[":event-type"] for m in buf]

    # OpenClaw は発話の直後に「runtime context」を別の user メッセージで足す
    body = {
        "messages": [
            {"role": "user", "content": [{"text": "[t] 資料を探して"}]},
            {"role": "user", "content": [{"text": "OpenClaw runtime context ..."}]},
        ]
    }
    assert "contentBlockStart" in kinds(stubs.scripted_reply(body, turns))
    body["messages"] += [
        {
            "role": "assistant",
            "content": [{"toolUse": {"toolUseId": "a", "name": "teamagent__search", "input": {}}}],
        },
        {
            "role": "user",
            "content": [{"toolResult": {"toolUseId": "a", "content": [{"text": "{}"}]}}],
        },
    ]
    assert "contentBlockStart" not in kinds(stubs.scripted_reply(body, turns))
    with pytest.raises(ValueError):
        stubs.scripted_reply(
            {"messages": [{"role": "user", "content": [{"text": "台本に無い"}]}]}, turns
        )
