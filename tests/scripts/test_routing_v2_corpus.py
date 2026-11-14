"""Synthetic routing labels and offline evaluator contracts; no accuracy gate in CI."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unicodedata
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests/routing/routing_v2_corpus.jsonl"
RUNNER = ROOT / "scripts/routing_eval/classify_eval.mjs"
NODE = shutil.which("node")
REQUIRED = {
    "id",
    "split",
    "utterance",
    "domain",
    "rule_id",
    "requires_internal_evidence",
    "requested_output",
    "expected_tool",
    "fallback",
    "notes",
}
PRIVATE_DATA = re.compile(
    r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+"
    r"|[a-z][a-z0-9+.-]*://\S+|www\.\S+"
    r"|(?:[a-z0-9-]+\.)+(?:com|net|org|jp|io|dev)\b"
    r"|(?-i:(?<![A-Za-z0-9])U[A-Z0-9]{8,}(?![A-Za-z0-9]))",
    re.IGNORECASE,
)


def test_corpus_structure_and_privacy() -> None:
    rows = [json.loads(line) for line in CORPUS.read_text().splitlines()]
    assert Counter(row["split"] for row in rows) == {"tune": 84, "eval": 43}
    assert len({row["id"] for row in rows}) == len(rows)
    plugin = (ROOT / "infra/openclaw/caller-identity-plugin/dist/index.js").read_text()
    classifier = plugin.split("export function classifyInboundRoute(text) {", 1)[1].split(
        "export function createCallerIdentityPlugin", 1
    )[0]
    rule_ids = set(re.findall(r'route\("[^"]+", (?:true|false), "[^"]+", "([^"]+)"\)', classifier))
    utterances: dict[str, set[str]] = {"tune": set(), "eval": set()}
    families: dict[str, set[str]] = {"tune": set(), "eval": set()}
    for row in rows:
        assert REQUIRED <= row.keys(), row.get("id")
        assert row["split"] in {"tune", "eval"}
        for key in ("id", "domain", "rule_id", "requested_output", "notes"):
            assert isinstance(row[key], str) and row[key].strip(), (row["id"], key)
        assert isinstance(row["utterance"], str)
        assert row["utterance"].strip() or row["rule_id"] == "absent"
        assert type(row["requires_internal_evidence"]) is bool
        assert type(row["fallback"]) is bool
        assert row["expected_tool"] is None or (
            isinstance(row["expected_tool"], str) and row["expected_tool"].strip()
        )
        assert row["rule_id"] in rule_ids
        assert row["requested_output"] in {"answer", "list", "artifact", "tool", "file"}
        assert not PRIVATE_DATA.search(json.dumps(row, ensure_ascii=False)), row["id"]
        utterances[row["split"]].add(unicodedata.normalize("NFKC", row["utterance"]).strip())
        family = re.match(r"family=([a-z0-9_]+);", row["notes"])
        assert family, row["id"]
        families[row["split"]].add(family[1])
    assert utterances["tune"].isdisjoint(utterances["eval"])
    # Families are manually assigned; this catches accidental movement, not semantic equivalence.
    assert families["tune"].isdisjoint(families["eval"])


@pytest.mark.parametrize(
    "value",
    [
        "fixture@example.invalid",
        "https://example.invalid/item",
        "www.example.invalid",
        "example.com",
        "U01234567",
        "<@U0ABCDEF12>",
    ],
)
def test_privacy_patterns(value: str) -> None:
    assert PRIVATE_DATA.search(value)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize(("split", "size"), [("eval", 43), ("tune", 84), ("all", 127)])
def test_classifier_emits_json(split: str, size: int) -> None:
    result = subprocess.run(
        [NODE, str(RUNNER), "--split", split],
        cwd=ROOT.parent,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["split"] == split
    assert report["total"] == len(report["rows"]) == size
    assert report["domain_accuracy"]["N"] == size
    assert report["classification_ms"]["p50"] <= report["classification_ms"]["p95"]
    assert "Domain confusion" in result.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_scoring_counts_confusion_fallback_and_small_sample_gate() -> None:
    script = f"""
        import assert from 'node:assert/strict';
        import {{evaluateRows}} from {json.dumps(RUNNER.as_uri())};
        // Tools and service access must never be used by the evaluator.
        globalThis.fetch = () => {{ throw new Error('network forbidden'); }};
        const base = {{utterance: '社内の事例を一覧にして。', domain: 'cases',
          rule_id: 'case_list', requires_internal_evidence: true,
          requested_output: 'list', fallback: false}};
        const wrong = {{...base, id: 'wrong', domain: 'slack', rule_id: 'existing_operation',
          requires_internal_evidence: false, requested_output: 'answer', fallback: true}};
        const result = evaluateRows([{{...base, id: 'right'}}, wrong]);
        assert.deepEqual(result.domain_accuracy, {{n: 1, N: 2, rate: 0.5}});
        assert.equal(result.confusion_matrix.slack.cases, 1);
        assert.equal(result.rule_confusion_matrix.existing_operation.case_list, 1);
        assert.equal(result.evidence_accuracy.n, 1);
        assert.equal(result.output_accuracy.n, 1);
        assert.equal(result.by_rule.case_list.rule_match.N, 1);
        assert.equal(result.by_rule.case_list.rule_precision.N, 2);
        assert.equal(result.by_rule.case_list.direct_precision.n, 1);
        assert.equal(result.by_rule.case_list.classification_gate_pass, false);
        assert.deepEqual(result.fallback.false_direct, ['wrong']);
        assert.equal(result.by_rule.existing_operation.rule_precision.rate, null);
        const five = Array.from({{length: 5}}, (_, i) => ({{...base, id: String(i)}}));
        assert.equal(evaluateRows(five).by_rule.case_list.classification_gate_pass, true);
        assert.equal(evaluateRows([...five, wrong]).by_rule.case_list.classification_gate_pass, false);
        assert.throws(() => evaluateRows([]), /No corpus rows/);
    """
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_invalid_split_fails() -> None:
    result = subprocess.run(
        [NODE, str(RUNNER), "--split", "invalid"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert not result.stdout
    assert "Usage:" in result.stderr
