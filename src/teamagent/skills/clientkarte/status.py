"""カルテの基準日と、期限を過ぎた予定を回答の形を保って補足する。"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date
from html import escape

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.search import timeline_status

_DATE = re.compile(
    r"(?<![\d/])(?:(?P<year>\d{4})[-/年])?(?P<month>\d{1,2})"
    r"(?:[-/](?P<day>\d{1,2})(?!\d)|月(?:(?P<jday>\d{1,2})日|"
    r"(?P<period>上旬|中旬|下旬))?)"
    r"(?:[〜～~–-](?:(?P<endmonth>\d{1,2})[月/])?"
    r"(?:(?P<endday>\d{1,2})日?|(?P<endperiod>上旬|中旬|下旬)))?"
)
_PLAN = re.compile(r"予定|期日|期限|締[め切]|決定時期|コンペ決定")
_PAST_PLAN = "予定だった（結果は未確認）"


def _deadline(match: re.Match[str], as_of: date) -> date | None:
    month = int(match["endmonth"] or match["month"])
    year = int(match["year"] or as_of.year)
    try:
        day = int(match["endday"] or match["day"] or match["jday"] or 0)
        period = match["endperiod"] or match["period"]
        if period or not day:
            day = {"上旬": 10, "中旬": 20}.get(period, monthrange(year, month)[1])
        return date(year, month, day)
    except ValueError:
        return None


def _past_plans(text: str, as_of: date, today: date) -> str:
    def rewrite(match: re.Match[str]) -> str:
        line = match[0]
        dates = [m for m in _DATE.finditer(line) if not re.match(r"\s*時点", line[m.end() :])]
        deadlines = [_deadline(m, as_of) for m in dates]
        if (
            _PLAN.search(line)
            and deadlines
            and all(d is not None and d < today for d in deadlines)
            and _PAST_PLAN not in line
        ):
            # 結果の記載がある行は未確認に戻さない。
            if not re.search(r"実施済|提出済|提案済|決定済|結果[:：]|見送り|中止|延期", line):
                if re.search(r"予定(?!日)", line):
                    line = re.sub(
                        r"(?:を)?予定(?!日)(?:していた|している|だった|でした|です)?",
                        lambda m: ("の" if m[0].startswith("を") else "") + _PAST_PLAN,
                        line,
                    )
                else:
                    last = dates[-1]
                    line = line[: last.end()] + " " + _PAST_PLAN + line[last.end() :]
        return line

    return re.sub(r"[^。；;、,\n]+", rewrite, text)


def dated_answer(answer: str, hits: list[SearchHit], client_name: str, today: date) -> str:
    """案件ごとの最新の記録日を一度付け、過去の予定を現在の予定から区別する。"""
    latest: dict[str, str] = {}
    for hit in hits:
        meta = hit.metadata or {}
        label = str(
            meta.get("campaign")
            or meta.get("case_product")
            or meta.get("proposed_menu")
            or client_name
        )
        found, _ = timeline_status.evidence_date(hit)
        if found:
            latest[label] = max(latest.get(label, ""), found)
    if not latest:
        return answer

    lines = answer.splitlines()
    current = date.fromisoformat(next(iter(latest.values()))) if len(latest) == 1 else None
    seen: set[str] = set()
    for index, line in enumerate(lines):
        labels = [label for label in latest if label in line]
        if len(labels) == 1:
            label = labels[0]
            current = date.fromisoformat(latest[label])
            if label not in seen:
                marker = f"（{current.month}/{current.day} 時点）"
                if marker not in line and (len(latest) != 1 or marker not in answer):
                    line = line.replace(label, label + marker, 1)
                seen.add(label)
        elif len(labels) > 1:
            current = None
        if current:
            line = _past_plans(line, current, today)
        lines[index] = line

    for label, found in latest.items():
        if label in seen:
            continue
        as_of = date.fromisoformat(found)
        marker = f"（{as_of.month}/{as_of.day} 時点）"
        if len(latest) == 1 and lines:
            if marker not in answer:
                lines[0] += marker
        else:
            lines.append(f"{label}{marker}")
    return "\n".join(lines)


def case_state_lines(hits: list[SearchHit]) -> str:
    """search と同じ正本の判定を一行で添える。根拠のない案件は足さない。"""
    lines = []
    for status in timeline_status.resolve_statuses(hits, case_state=True):
        if not any(bundle.records for bundle in status.evidence_sources):
            continue
        evidence = status.evidence
        assert evidence is not None
        records = [
            r
            for r in evidence.records
            if r.authority == "official"
            and r.state == evidence.state
            and status.text.startswith(f"{r.state_as_of} 時点")
        ]
        if evidence.resolution != "resolved":
            records = [r for bundle in status.evidence_sources for r in bundle.records]
        dates = sorted({r.evidence_date for r in records if r.evidence_date})
        source_date = "・".join(dates) if dates else "日付不明"
        label = escape(
            timeline_status.guard_state_words(" ".join(status.label.split())), quote=False
        )
        url = evidence.source_ref.url
        if not url or not re.fullmatch(r"https?://[^\s<>|]+", url):
            url = None
        lines.append(
            f"状態: {label} / {status.text}（根拠日: {source_date}）" + (f" {url}" if url else "")
        )
    return "\n".join(lines)
