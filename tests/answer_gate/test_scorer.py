"""Bad visible answers must fail independently of model and skill metadata."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from teamagent.eval.answer_gate.runner import load_cases
from teamagent.eval.answer_gate.scorer import score

ROOT = Path(__file__).parents[2] / "src/teamagent/eval/answer_gate/data"


def sample(case_id="case-summary-005"):
    case = next(c for c in load_cases(ROOT / "cases_v1.yaml") if c["id"] == case_id)
    fixture = json.loads((ROOT / "fixtures" / f"{case['fixture_id']}.json").read_text())
    record = dict(
        answer="\n".join(
            f"{i}. {r['title']} [資料]({r['url']})" for i, r in enumerate(fixture["refs"], 1)
        ),
        tool_calls=[{"name": t, "arguments": {}} for t in case["expected_tools"]],
        lanes=[{"name": n, "status": "ok"} for n in case["required_lanes"]],
        completed=True,
        elapsed_s=0.1,
        external_sent=False,
        side_effects=[],
        error=None,
    )
    return case, fixture, record


def test_valid_five():
    assert score(*sample())["pass"]


@pytest.mark.parametrize(
    ("bad", "check"),
    [
        ("one", "count"),
        ("fake_url", "links"),
        ("duplicate", "count"),
        ("ask", "no_ask"),
        ("tool", "no_internal_terms"),
        ("long", "length"),
        ("bare_links", "count"),
        ("missing_link", "links"),
        ("link_on_wrong_item", "links"),
        ("signed", "links"),
        ("internal_uri", "no_internal_terms"),
        ("absence", "no_ask"),
    ],
)
def test_bad_answers(bad, check):
    c, f, r = sample()
    lines = r["answer"].splitlines()
    if bad == "one":
        r["answer"] = lines[0]
    if bad == "fake_url":
        r["answer"] = r["answer"].replace(f["refs"][0]["url"], "https://example.test/fabricated")
    if bad == "duplicate":
        r["answer"] = "\n".join([lines[0]] * 5)
    if bad == "ask":
        r["answer"] += "\nどのブランドをお探しですか？"
    if bad == "tool":
        r["answer"] += "\nteamagent__search を使いました。"
    if bad == "long":
        r["answer"] += "あ" * 2501
    if bad == "bare_links":
        r["answer"] = "\n".join(v["url"] for v in f["refs"])
    if bad == "missing_link":
        r["answer"] = r["answer"].replace(f["refs"][0]["url"], "")
    if bad == "link_on_wrong_item":
        r["answer"] = r["answer"].replace(f["refs"][0]["url"], f["refs"][1]["url"])
    if bad == "signed":
        r["answer"] = r["answer"].replace(
            f["refs"][0]["url"], f["refs"][0]["url"] + "?token=secret"
        )
    if bad == "internal_uri":
        r["answer"] += "\ngdrive://private-id"
    if bad == "absence":
        r["answer"] += "\n該当する資料は見つかりませんでした。"
    result = score(c, f, r)
    assert not result["checks"][check]["pass"], result


@pytest.mark.parametrize("available", [0, 2])
def test_shortage_requires_exact_statement(available):
    c, f, r = sample()
    f["refs"] = f["refs"][:available]
    r["answer"] = "\n".join(r["answer"].splitlines()[:available])
    assert not score(c, f, r)["checks"]["count"]["pass"]
    r["answer"] += f"\n見つかったのは {available} 件です。"
    assert score(c, f, r)["pass"]


def test_same_url_distinct_items():
    c, f, r = sample()
    old = f["refs"][1]["url"]
    f["refs"][1]["url"] = f["refs"][0]["url"]
    r["answer"] = r["answer"].replace(old, f["refs"][0]["url"])
    assert score(c, f, r)["pass"]


@pytest.mark.parametrize("answer", ["総予算: 400,000円", "総予算：４０００００円"])
def test_decimal_correct(answer):
    c, f, r = sample("budget")
    r["answer"] = answer
    assert score(c, f, r)["pass"]


@pytest.mark.parametrize(
    "answer",
    [
        "総予算: 360000円",
        "120000円×3+40000円",
        "総予算: 400000ドル",
        "総予算: 400000円\n総予算: 0円",
    ],
)
def test_decimal_wrong(answer):
    c, f, r = sample("budget")
    r["answer"] = answer
    assert not score(c, f, r)["checks"]["numeric"]["pass"]


def test_wrong_ledger_expected_is_rejected():
    c, f, r = sample("budget")
    c["expected"] = "360000"
    r["answer"] = "総予算: 360000円"
    assert not score(c, f, r)["checks"]["numeric"]["pass"]


@pytest.mark.parametrize(
    "failure",
    [
        "missing_lane",
        "failed_lane",
        "unknown_tool",
        "send",
        "missing_log",
        "incomplete",
        "timeout",
        "unknown_check",
    ],
)
def test_execution_contract(failure):
    c, f, r = sample()
    if failure == "missing_lane":
        r["lanes"].pop()
    if failure == "failed_lane":
        r["lanes"][0]["status"] = "timeout"
    if failure == "unknown_tool":
        r["tool_calls"].append({"name": "unknown"})
    if failure == "send":
        r["side_effects"].append({"kind": "slack_send"})
    if failure == "missing_log":
        del r["side_effects"]
    if failure == "incomplete":
        r["completed"] = False
    if failure == "timeout":
        r["elapsed_s"] = 121
    if failure == "unknown_check":
        c["checks"].append("unknown")
    assert not score(c, f, r)["pass"]


def test_item_order_does_not_change_result():
    c, f, r = sample()
    other = deepcopy(r)
    other["answer"] = "\n".join(reversed(r["answer"].splitlines()))
    assert score(c, f, r) == score(c, f, other)
