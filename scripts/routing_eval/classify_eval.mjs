#!/usr/bin/env node
// Offline only: importing the plugin does not register hooks or call tools.
import {readFileSync} from 'node:fs';
import {performance} from 'node:perf_hooks';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {classifyInboundRoute} from '../../infra/openclaw/caller-identity-plugin/dist/index.js';

// Fixed projection of existing intents, not additional utterance matching rules.
const DOMAINS = {
  generation: 'document_creation', document: 'internal_knowledge',
  cases: 'cases', status: 'cases', metrics: 'cases', comparison: 'cases',
  internal_term: 'internal_knowledge', internal_topic: 'internal_knowledge',
  external_research: 'public_research', existing_operation: 'integration',
  general: 'general', chat: 'general', unknown: 'unknown',
};
const FIRST_WEEK_RULES = new Set(['case_list', 'internal_status', 'internal_metrics']);
const rate = (n, N) => ({n, N, rate: N ? n / N : null});
const countMatches = (rows, predicate) => rate(rows.filter(predicate).length, rows.length);
const fields = ['domain', 'rule_id', 'requires_internal_evidence', 'requested_output', 'fallback'];
const exact = row => fields.every(field => row.expected[field] === row.actual[field]);

function confusion(rows, field) {
  const matrix = {};
  for (const {expected, actual} of rows) {
    const counts = matrix[expected[field]] ??= {};
    counts[actual[field]] = (counts[actual[field]] ?? 0) + 1;
  }
  return matrix;
}

export function evaluateRows(corpus) {
  if (!corpus.length) throw new Error('No corpus rows selected');
  const rows = corpus.map(expected => {
    const start = performance.now();
    const route = classifyInboundRoute(expected.utterance);
    const duration = performance.now() - start;
    // Text-only eligibility from prepareInboundRoute; no identity/media/hook simulation.
    const fallback = !route.requires_internal_evidence || route.requested_output === 'file' ||
      Boolean(route.secondary_intent);
    const domain = route.rule_id === 'document_delivery' ? 'integration' : DOMAINS[route.intent];
    if (!domain) throw new Error(`Unmapped plugin intent: ${route.intent}`);
    return {id: expected.id, expected, actual: {...route, domain, fallback}, classification_ms: duration};
  });
  const byRule = {};
  const rules = [...new Set(rows.flatMap(row => [row.expected.rule_id, row.actual.rule_id]))].sort();
  for (const rule of rules) {
    const expected = rows.filter(row => row.expected.rule_id === rule);
    const predicted = rows.filter(row => row.actual.rule_id === rule);
    const direct = predicted.filter(row => !row.actual.fallback);
    const recall = countMatches(expected, row => row.actual.rule_id === rule);
    const precision = countMatches(predicted, row => row.expected.rule_id === rule);
    const directPrecision = countMatches(direct, exact);
    byRule[rule] = {
      rule_match: recall, rule_precision: precision,
      all_fields_match: countMatches(expected, exact), direct_precision: directPrecision,
      first_week_candidate: FIRST_WEEK_RULES.has(rule),
      classification_gate_pass: FIRST_WEEK_RULES.has(rule) && recall.N >= 5 && recall.rate >= 0.95 &&
        directPrecision.N >= 5 && directPrecision.rate >= 0.95,
    };
  }
  const durations = rows.map(row => row.classification_ms).sort((a, b) => a - b);
  const percentile = p => durations[Math.ceil(durations.length * p) - 1];
  return {
    total: rows.length,
    domain_accuracy: countMatches(rows, row => row.expected.domain === row.actual.domain),
    rule_accuracy: countMatches(rows, row => row.expected.rule_id === row.actual.rule_id),
    by_domain: Object.fromEntries([...new Set(rows.map(row => row.expected.domain))].sort()
      .map(domain => [domain, countMatches(rows.filter(row => row.expected.domain === domain),
        row => row.expected.domain === row.actual.domain)])),
    by_rule: byRule,
    confusion_matrix: confusion(rows, 'domain'), rule_confusion_matrix: confusion(rows, 'rule_id'),
    evidence_accuracy: countMatches(rows, row =>
      row.expected.requires_internal_evidence === row.actual.requires_internal_evidence),
    output_accuracy: countMatches(rows, row => row.expected.requested_output === row.actual.requested_output),
    fallback: {
      predicted: countMatches(rows, row => row.actual.fallback),
      expected: countMatches(rows, row => row.expected.fallback),
      agreement: countMatches(rows, row => row.expected.fallback === row.actual.fallback),
      false_direct: rows.filter(row => row.expected.fallback && !row.actual.fallback).map(row => row.id),
    },
    classification_ms: {p50: percentile(0.5), p95: percentile(0.95), method: 'nearest-rank, single cold pass, import excluded'},
    rows,
  };
}

function formatRate(value) {
  return `${value.n}/${value.N} (${value.rate === null ? 'n/a' : `${(value.rate * 100).toFixed(1)}%`})`;
}

export function formatText(report) {
  return [
    `Routing v2 baseline: split=${report.split}, N=${report.total}`,
    `Domain: ${formatRate(report.domain_accuracy)}`,
    `Rule ID: ${formatRate(report.rule_accuracy)}`,
    ...Object.entries(report.by_domain).map(([domain, value]) => `  ${domain}: ${formatRate(value)}`),
    'Rule: correct/expected; correct/predicted; exact direct/predicted direct; classification gate',
    ...Object.entries(report.by_rule).map(([rule, value]) =>
      `  ${rule}: ${formatRate(value.rule_match)}; ${formatRate(value.rule_precision)}; ` +
      `${formatRate(value.direct_precision)}; ${value.classification_gate_pass ? 'PASS' : 'HOLD'}`),
    `Domain confusion (expected -> predicted): ${JSON.stringify(report.confusion_matrix)}`,
    `Rule confusion (expected -> predicted): ${JSON.stringify(report.rule_confusion_matrix)}`,
    `Internal evidence: ${formatRate(report.evidence_accuracy)}`,
    `Requested output: ${formatRate(report.output_accuracy)}`,
    `Fallback: predicted ${formatRate(report.fallback.predicted)}, expected ${formatRate(report.fallback.expected)}, ` +
      `agreement ${formatRate(report.fallback.agreement)}`,
    `False direct: ${report.fallback.false_direct.join(', ') || 'none'}`,
    `Classification ms: p50=${report.classification_ms.p50.toFixed(3)}, p95=${report.classification_ms.p95.toFixed(3)}`,
    'Classification only; gate PASS does not authorize enforce. Tools/hooks/answers are not executed.',
  ].join('\n');
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    const args = process.argv.slice(2);
    const split = args.length === 0 ? 'all' : args.length === 2 && args[0] === '--split' ? args[1] : '';
    if (!['eval', 'tune', 'all'].includes(split)) throw new Error('Usage: classify_eval.mjs [--split eval|tune|all]');
    const corpus = readFileSync(new URL('../../tests/routing/routing_v2_corpus.jsonl', import.meta.url), 'utf8')
      .trim().split('\n').map(line => JSON.parse(line));
    const report = {split, ...evaluateRows(corpus.filter(row => split === 'all' || row.split === split))};
    // stdout stays machine-readable; stderr contains the human-readable report.
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    process.stderr.write(`${formatText(report)}\n`);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}
