"""Real skills, replayed retrieval, fake model transport, and captured side effects."""

import json
import socket
from collections import Counter
from functools import partial
from pathlib import Path

import pytest

from teamagent.eval.answer_gate.adapters import FakeLLM
from teamagent.eval.answer_gate.runner import execute, isolated, load_cases, main, run
from teamagent.eval.answer_gate.scorer import score

ROOT = Path(__file__).parents[2] / "src/teamagent/eval/answer_gate/data"
CASES = ROOT / "cases_v1.yaml"


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_fixed_distribution_and_hits():
    cases = load_cases(CASES)
    assert len(cases) == 16
    assert Counter(c["domain"] for c in cases) == dict(
        documents=3, cases=4, deals=2, slack=1, calendar=2, mail=2, math=1, draft=1
    )
    fixture = json.loads((ROOT / "fixtures/many-v1.json").read_text())
    assert [len(fixture[lane]) for lane in ("vault", "slack", "case")] == [5, 17, 3]
    assert len(fixture["refs"]) == 5
    assert any("５件" in c["utterance"] for c in cases)
    assert all(c["critical"] for c in cases)


def test_cli_fake_completes_all_cases(tmp_path):
    out = tmp_path / "out.jsonl"
    assert main(["--cases", str(CASES), "--out", str(out), "--fake-llm"]) == 0
    results = rows(out)
    assert len(results) == 16
    assert all(r["completed"] and r["result"]["pass"] for r in results), [
        (r["id"], r["result"]) for r in results if not r["result"]["pass"]
    ]
    assert all(r["external_sent"] is False and r["side_effects"] == [] for r in results)
    assert any(r["model_calls"] for r in results)


def test_one_cited_item_fails_five_case_question(tmp_path):
    out = tmp_path / "bad.jsonl"
    assert run(CASES, out, fake_llm=True, llm_factory=partial(FakeLLM, one_item=True)) == 1
    results = rows(out)
    assert len(results) == 16 and all(r["completed"] for r in results)
    result = next(r for r in results if r["id"] == "case-summary-005")
    assert not result["result"]["checks"]["count"]["pass"]
    assert result["model_calls"]


def test_no_model_or_business_client_constructed_in_fake_mode(monkeypatch):
    import boto3
    import psycopg

    def forbidden(*args, **kwargs):
        pytest.fail("unexpected real client")

    monkeypatch.setattr(boto3, "client", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)
    case = load_cases(CASES)[4]
    fixture = json.loads((ROOT / "fixtures/many-v1.json").read_text())
    record = execute(case, fixture, fake_llm=True)
    assert score(case, fixture, record)["pass"]


def test_sinks_and_network_are_recorded():
    import asyncio

    import psycopg

    from teamagent.adapters.slack_client import SlackClient
    from teamagent.mcp_gateway import direct_search_answer, progress_notify

    record = {"side_effects": []}
    with isolated(record, fake_llm=True):
        direct_search_answer.deliver("answer", "list", None, request_id="fixture")
        asyncio.run(progress_notify.send_progress("search", {}, request_id="fixture"))
        asyncio.run(
            SlackClient.post_message(None, channel="CFIXTURE", text="fixture", request_id="fixture")
        )
        psycopg.connect("fixture")
        with socket.socket() as sock, pytest.raises(PermissionError):
            sock.connect(("127.0.0.1", 9))
    assert Counter(e["kind"] for e in record["side_effects"]) == dict(
        direct_search_answer=1, slack_send=1, progress_notify=1, db_connection=1, network=1
    )


def test_missing_fixture_is_failure_and_all_results_written(tmp_path):
    import yaml

    cases = load_cases(CASES)
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(yaml.safe_dump({"version": "answer-gate-v1", "cases": cases}))
    out = tmp_path / "out.jsonl"
    assert run(cases_path, out, fake_llm=True) == 1
    assert len(rows(out)) == 16
    assert all(not r["completed"] for r in rows(out))


def test_timeout_is_incomplete_even_for_noncritical_case(tmp_path):
    import shutil

    import yaml

    case = load_cases(CASES)[0]
    case["time_limit_s"] = 0.001
    case["critical"] = False
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(yaml.safe_dump({"version": "answer-gate-v1", "cases": [case]}))
    shutil.copytree(ROOT / "fixtures", tmp_path / "fixtures")
    out = tmp_path / "timeout.jsonl"
    assert run(cases_path, out, fake_llm=True) == 1
    result = rows(out)[0]
    assert result["completed"] is False
    assert "TimeoutError" in result["error"]
