"""カルテの基準日と、期限を過ぎた予定を回答の形を保って補足する。"""

from __future__ import annotations

import re
import unicodedata
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
_PLAN = re.compile(r"予定|期日|期限|締[め切]|決定")
_PAST_PLAN = "予定していた（結果は未確認）"


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
        if _PLAN.search(line) and deadlines and all(d is not None and d < today for d in deadlines):
            # 結果の記載がある行は未確認に戻さない。
            if not re.search(
                r"実施済|提出済|提案済|決定済|決定した|決定しました|結果[:：]|見送り|中止|延期",
                line,
            ):
                if re.search(r"予定(?!日)", line):

                    def past_plan(plan: re.Match[str]) -> str:
                        particle = plan["particle"] or ""
                        before = line[: plan.start()]
                        if particle == "の" or re.search(r"[ぁ-ゖ]$", before):
                            return particle + "予定だった（結果は未確認）"
                        if particle == "を" or re.search(r"[一-龥々ァ-ヶー]$", before):
                            return "を" + _PAST_PLAN
                        return _PAST_PLAN

                    line = re.sub(
                        r"(?P<particle>を|の)?予定(?!日)"
                        r"(?:していた|している|だった|でした|です|だ)?"
                        r"(?:\s*(?:[（(]結果(?:は)?未確認[）)]|"
                        r"(?:だが|が|ものの)結果(?:は)?未確認))*",
                        past_plan,
                        line,
                    )
                elif re.search(r"決定(?:する)?$", line.strip()):
                    line = re.sub(r"決定(?:する)?(?=\s*$)", "決定を" + _PAST_PLAN, line)
                else:
                    last = dates[-1]
                    line = line[: last.end()] + " " + _PAST_PLAN + line[last.end() :]
        return line

    return re.sub(r"[^。；;、,\n]+", rewrite, text)


def dated_answer(answer: str, hits: list[SearchHit], client_name: str, today: date) -> str:
    """案件ごとの最新の記録日を一度付け、過去の予定を現在の予定から区別する。"""
    dated = []
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
            dated.append((hit.chunk_id, label, date.fromisoformat(found)))
    # LLM の既存注記も一度外し、案件の見出し末尾にまとめる。
    answer = re.sub(r"（\d{1,2}/\d{1,2}\s*時点(?:の記録)?）", "", answer) if dated else answer
    lines = answer.splitlines()

    def fold(text: str) -> str:
        return re.sub(r"[\s・、,，()（）*#]", "", unicodedata.normalize("NFKC", text)).casefold()

    case_names = {
        fold(str((hit.metadata or {}).get(key) or ""))
        for hit in hits
        for key in ("campaign", "case_product")
    } - {"", fold(client_name)}
    for index, line in enumerate(lines):
        label = line.strip()
        if (
            (index == 0 or not lines[index - 1].strip())
            and (index == len(lines) - 1 or not lines[index + 1].strip())
            and re.fullmatch(r"(?![-・])[^。！？!?：:*#<>\n]{1,45}", label)
            and (
                fold(label) in case_names
                or re.search(r"(?:案件|プロジェクト)(?:[（(][^()（）]+[）)])?$", label)
            )
        ):
            lines[index] = "### " + label
    if not dated:
        return "\n".join(lines)

    headings = [
        i
        for i, line in enumerate(lines)
        if re.fullmatch(r"\s*(?:#{1,3} +.+|(\*{1,2})[^*]+\1)\s*", line)
    ]
    assigned: set[int] = set()
    rewritten: set[int] = set()

    for pos, start in enumerate(headings):
        end = headings[pos + 1] if pos + 1 < len(headings) else len(lines)
        section = "\n".join(lines[start:end])
        citations = {int(n) for n in re.findall(r"chunk_id[:：]\s*(-?\d+)", section)}
        matches = [
            item
            for item in dated
            if item[0] not in assigned
            and (
                item[0] in citations
                or (
                    (len(headings) == 1 or fold(item[1]) != fold(client_name))
                    and fold(item[1]) in fold(lines[start])
                )
            )
        ]
        if not matches:
            continue
        current = max(item[2] for item in matches)
        assigned.update(item[0] for item in matches)
        lines[start] = lines[start].rstrip() + f"（{current.month}/{current.day} 時点）"
        for index in range(start + 1, end):
            lines[index] = _past_plans(lines[index], current, today)
            rewritten.add(index)

    # 冒頭の要約や案件に紐付かない段落にも、同じ期限の規則を適用する。
    latest = max(item[2] for item in dated)
    for index, line in enumerate(lines):
        if index not in rewritten and index not in headings:
            lines[index] = _past_plans(line, latest, today)

    remaining = [item[2] for item in dated if item[0] not in assigned]
    if remaining:
        current = max(remaining)
        if not assigned and len({item[1] for item in dated}) == 1:
            lines = [_past_plans(line, current, today) for line in lines]
        lines.append(f"（{current.month}/{current.day} 時点の記録）")
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
