"""抽出精度の計測（設計 §3-4）: 青木 事例DB.json（78 件）を正解として tag 一致率を出す。

ローカル・DB 不要。``scripts/extract_cases.py --out`` が書いた抽出結果 JSON（CaseRecord の配列）と
正解 JSON を、client 名の正規化一致（どちらかがもう一方を含む）で対応付け、
sector（完全一致率）/ purpose（集合 F1）/ traits（集合 F1）を出す。目標: sector 0.9・purpose 0.8。

対応付けの規則:
- 正解の ``client``（例「カネカ Q10グミ」）は先頭トークン（社名）で照合する。
- 1 正解に複数の抽出が当たるときは tag 一致数が最大のものを採る（上限値の見積もり。
  case_group での束ねは次段）。
- 抽出側が「その他」の tag は不一致として数える（語彙の説明文を直す材料になる）。

Usage:
    python scripts/eval_case_extraction.py \
        --truth ~/.claude/skills/proposal-builder/assets/事例DB.json \
        --extracted /tmp/cases.json [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from teamagent.cases.extract import normalize_client  # noqa: E402

_MIN_NAME_LEN = 2


@dataclass(frozen=True)
class TruthCase:
    id: str
    client: str
    sector: str
    purpose: frozenset[str]
    traits: frozenset[str]


@dataclass(frozen=True)
class ExtractedCase:
    case_id: str
    client_internal: str
    sector: str
    purpose: frozenset[str]
    traits: frozenset[str]


@dataclass
class EvalReport:
    truth_total: int = 0
    extracted_total: int = 0
    matched: int = 0
    sector_hits: int = 0
    purpose_f1_sum: float = 0.0
    traits_f1_sum: float = 0.0
    unmatched_truth: list[str] = field(default_factory=list)
    pairs: list[tuple[str, str]] = field(default_factory=list)

    @property
    def sector_accuracy(self) -> float:
        return self.sector_hits / self.matched if self.matched else 0.0

    @property
    def purpose_f1(self) -> float:
        return self.purpose_f1_sum / self.matched if self.matched else 0.0

    @property
    def traits_f1(self) -> float:
        return self.traits_f1_sum / self.matched if self.matched else 0.0

    @property
    def coverage(self) -> float:
        return self.matched / self.truth_total if self.truth_total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "truth_total": self.truth_total,
            "extracted_total": self.extracted_total,
            "matched": self.matched,
            "coverage": round(self.coverage, 4),
            "sector_accuracy": round(self.sector_accuracy, 4),
            "purpose_f1": round(self.purpose_f1, 4),
            "traits_f1": round(self.traits_f1, 4),
            "unmatched_truth": list(self.unmatched_truth),
            "pairs": [list(pair) for pair in self.pairs],
        }


def truth_company_key(client: str) -> str:
    """正解の client（「カネカ Q10グミ」）から社名だけを正規化して返す。"""
    head = re.split(r"[\s　/／|｜]+", unicodedata.normalize("NFKC", client).strip(), maxsplit=1)
    return normalize_client(head[0] if head else client)


def clients_match(truth_client: str, extracted_client: str) -> bool:
    """社名の正規化一致（どちらかがもう一方を含む・最小 2 文字）。"""
    a = truth_company_key(truth_client)
    b = normalize_client(extracted_client)
    if len(a) < _MIN_NAME_LEN or len(b) < _MIN_NAME_LEN:
        return False
    return a in b or b in a


def set_f1(truth: frozenset[str], predicted: frozenset[str]) -> float:
    """集合 F1（両方空なら 1.0・片方だけ空なら 0.0）。"""
    if not truth and not predicted:
        return 1.0
    hit = len(truth & predicted)
    if hit == 0:
        return 0.0
    precision = hit / len(predicted)
    recall = hit / len(truth)
    return 2 * precision * recall / (precision + recall)


def _tag_overlap(truth: TruthCase, extracted: ExtractedCase) -> float:
    return (
        (1.0 if truth.sector == extracted.sector else 0.0)
        + set_f1(truth.purpose, extracted.purpose)
        + set_f1(truth.traits, extracted.traits)
    )


def load_truth(payload: Mapping[str, Any]) -> list[TruthCase]:
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError("正解 JSON に cases 配列がありません")
    out: list[TruthCase] = []
    for item in cases:
        tags = item.get("tags") or {}
        out.append(
            TruthCase(
                id=str(item.get("id") or ""),
                client=str(item.get("client") or ""),
                sector=str(tags.get("sector") or ""),
                purpose=frozenset(str(p) for p in tags.get("purpose") or []),
                traits=frozenset(str(t) for t in tags.get("traits") or []),
            )
        )
    return out


def load_extracted(payload: Any) -> list[ExtractedCase]:
    records = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("抽出 JSON は CaseRecord の配列（または {records: [...]}）")
    out: list[ExtractedCase] = []
    for item in records:
        out.append(
            ExtractedCase(
                case_id=str(item.get("case_id") or ""),
                client_internal=str(item.get("client_internal") or ""),
                sector=str(item.get("sector") or ""),
                purpose=frozenset(str(p) for p in item.get("purpose") or []),
                traits=frozenset(str(t) for t in item.get("traits") or []),
            )
        )
    return out


def evaluate(truth: Sequence[TruthCase], extracted: Sequence[ExtractedCase]) -> EvalReport:
    """正解ごとに最良の抽出を対応付けて集計する（抽出 1 件は 1 正解にしか使わない）。"""
    report = EvalReport(truth_total=len(truth), extracted_total=len(extracted))
    used: set[str] = set()
    for item in truth:
        candidates = [
            e
            for e in extracted
            if e.case_id not in used and clients_match(item.client, e.client_internal)
        ]
        if not candidates:
            report.unmatched_truth.append(item.id)
            continue
        best = max(candidates, key=lambda e: (_tag_overlap(item, e), e.case_id))
        used.add(best.case_id)
        report.matched += 1
        report.pairs.append((item.id, best.case_id))
        if item.sector == best.sector:
            report.sector_hits += 1
        report.purpose_f1_sum += set_f1(item.purpose, best.purpose)
        report.traits_f1_sum += set_f1(item.traits, best.traits)
    return report


def render_report(report: EvalReport) -> str:
    lines = [
        f"truth: {report.truth_total}  extracted: {report.extracted_total}  "
        f"matched: {report.matched} (coverage {report.coverage:.2f})",
        f"sector accuracy: {report.sector_accuracy:.2f}  (target 0.90)",
        f"purpose F1:      {report.purpose_f1:.2f}  (target 0.80)",
        f"traits F1:       {report.traits_f1:.2f}",
    ]
    if report.unmatched_truth:
        lines.append("unmatched truth ids: " + ", ".join(report.unmatched_truth[:30]))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="事例抽出の tag 一致率を測る")
    parser.add_argument("--truth", required=True, help="青木 事例DB.json")
    parser.add_argument("--extracted", required=True, help="extract_cases.py --out の JSON")
    parser.add_argument("--json", action="store_true", help="集計を JSON で出す")
    args = parser.parse_args(argv)

    truth_payload = json.loads(Path(args.truth).expanduser().read_text(encoding="utf-8"))
    extracted_payload = json.loads(Path(args.extracted).expanduser().read_text(encoding="utf-8"))
    report = evaluate(load_truth(truth_payload), load_extracted(extracted_payload))
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=1))
    else:
        print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
