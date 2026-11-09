"""直接投稿とモデルへの返却で共通の、短いカルテ本文。"""

from __future__ import annotations

import re
from datetime import date

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.clientkarte.documents import slack_label
from teamagent.skills.search.timeline_status import evidence_date

_HEADING = re.compile(r"^(?:#{1,3} +(.+?)|(\*{1,2})(.+?)\2)(（\d+/\d+\s*時点）)?$")
_ASOF = re.compile(r"（\d+/\d+\s*時点(?:の記録)?）")
_CITATION = re.compile(r"[ \t]*[\[(][^\[\]()\n]*chunk_id[^\[\]()\n]*[\])]")


def _stamp(found: str) -> str:
    if not found:
        return "（時点不明）"
    current = date.fromisoformat(found)
    return f"（{current.month}/{current.day} 時点）"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    end = max(0, limit - 1)
    # 未確認の注記を途中で切ると、過去の予定が現在の状態に見えてしまう。
    for match in re.finditer(r"予定(?:していた|だった)（結果は未確認）", text):
        if match.start() < end < match.end():
            end = match.start()
    return text[:end].rstrip() + "…"


def short_answer(
    answer: str,
    hits: list[SearchHit],
    client_name: str,
    *,
    budget: int = 600,
    state_lines: str = "",
) -> str:
    """結論と新しい順の最大5案件に絞る。リンク・状態行は呼び出し元で保持する。"""
    if not hits:
        return answer
    latest = max(hits, key=lambda h: evidence_date(h)[0])
    latest_date = evidence_date(latest)[0]
    asof = _stamp(latest_date)
    sections: list[tuple[str, list[str]]] = []
    summary: list[str] = []
    links: list[str] = []
    current = summary
    reported_omitted = 0
    for raw in answer.splitlines():
        line = raw.strip()
        if omitted_line := re.fullmatch(r"ほか\s*(\d+)\s*件", line):
            reported_omitted += int(omitted_line[1])
            continue
        match = _HEADING.fullmatch(line)
        if match:
            label = match[1] or match[3]
            if "カルテ" in label or label == client_name:
                continue
            if re.search(r"サマリ|要約|結論|概要|現在地", label):
                current = summary
                continue
            sections.append((label + (match[4] or ""), []))
            current = sections[-1][1]
        elif "https://" in line or "http://" in line:
            links.append(_CITATION.sub("", line))
        elif line and not re.fullmatch(r"（\d+/\d+ 時点の記録）", line):
            current.append(line)

    def clean(text: str) -> str:
        text = _CITATION.sub("", text).strip(" -*")
        text = re.sub(r"\bBANT\s*[:：]?\s*[ABCD](?:評価)?\b", "", text)
        return text.replace("営業 FB", "記録").replace("営業FB", "記録").strip(" 、,")

    def section_date(section: tuple[str, list[str]]) -> str:
        label, body = section
        ids = {int(n) for n in re.findall(r"chunk_id[:：]\s*(-?\d+)", "\n".join(body))}
        dates = [
            evidence_date(h)[0]
            for h in hits
            if h.chunk_id in ids
            or any(
                str((h.metadata or {}).get(key) or "\0") in label
                for key in ("campaign", "case_product")
            )
        ]
        # dated_answer が出典から付けた時点を、年を跨いでも元の日付で並べる。
        if not dates:
            dates = [
                evidence_date(h)[0]
                for h in hits
                if evidence_date(h)[0] and _stamp(evidence_date(h)[0]) in label
            ]
        return max(dates, default="")

    sections.sort(key=section_date, reverse=True)
    conclusion = clean(summary[0]) if summary else ""
    if not conclusion:
        conclusion = next(
            (clean(line) for _, body in sections for line in body), "記録のみで現状は未確認"
        )
    conclusion_date = latest_date
    # 正本の状態が営業記録より新しければ、結論も古い記録のままにしない。
    for label, state_date, state in re.findall(
        r"^状態: (.+?) / (\d{4}-\d{2}-\d{2}) 時点の正式状態: ([^\n（]+)",
        state_lines,
        re.MULTILINE,
    ):
        if state_date >= conclusion_date:
            conclusion = f"{label}は{state}"
            conclusion_date = state_date
    conclusion = _ASOF.sub("", conclusion).strip()
    if client_name not in conclusion:
        conclusion = f"{client_name}: {conclusion}"
    conclusion = conclusion.replace(client_name, slack_label(client_name))
    conclusion = _clip(conclusion, 110) + _stamp(conclusion_date)
    if not sections and summary:
        label = str((latest.metadata or {}).get("campaign") or client_name)
        sections = [(label + asof, summary[1:] or summary)]
    omitted = reported_omitted + max(0, len(sections) - 5)
    sections = sections[:5]
    tail = f"\n\nほか {omitted} 件" if omitted else ""
    if links:
        tail += "\n\n" + "\n".join(dict.fromkeys(links))
    # shortcut: 状態行・リンクだけで600字を超えたら保持を優先。資料一覧の仕様変更時に再検討。
    available = max(0, budget - len(conclusion) - len(tail))
    per_case = available // len(sections) if sections else 0
    blocks = [conclusion]
    for label, body in sections:
        found = _ASOF.search(label)
        stamp = found[0] if found else "（時点不明）"
        title = slack_label(_ASOF.sub("", label).strip(" *"))
        heading = f"**{_clip(title, min(45, max(1, per_case - len(stamp) - 10)))}{stamp}**"
        lines = [clean(line) for line in body if clean(line)][:3]
        if len(lines) == 1:
            lines = re.split(r"(?<=。)", lines[0], maxsplit=1)
            lines = [line for line in lines if line]
        if len(lines) < 2:
            lines.extend(["追加の経過は未確認。"] * (2 - len(lines)))
        remaining = max(0, per_case - len(heading) - len(lines) - 2)
        width = remaining // len(lines)
        blocks.append(heading + "\n" + "\n".join(_clip(line, width) for line in lines))
    return "\n\n".join(blocks) + tail
