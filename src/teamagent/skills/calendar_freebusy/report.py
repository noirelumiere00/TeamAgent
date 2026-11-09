"""本人の報告テンプレートへ、コードで分類した予定だけを挿入する。"""

from __future__ import annotations

import datetime as dt
import re

from teamagent.skills.calendar_freebusy.agenda import THIRD_PARTY_TITLE_NOTE, AgendaEntry
from teamagent.skills.calendar_freebusy.free_windows import day_label_ja

_SLOTS = re.compile(r"\{(appointments|work|date)\}")


def validate_template(template: str) -> None:
    if not template.strip() or len(template) > 6000:
        raise ValueError("invalid template size")
    if any(template.count("{" + slot + "}") != 1 for slot in ("appointments", "work")):
        raise ValueError("one slot per category required")
    if any(ord(ch) < 32 and ch not in "\n\t\r" for ch in template):
        raise ValueError("invalid control character")


def format_report(template: str, day: dt.date, entries: list[AgendaEntry]) -> str:
    validate_template(template)
    values = {"date": day_label_ja(day)}
    for key, appointment in (("appointments", True), ("work", False)):
        values[key] = (
            "\n".join(
                "・" + entry.label.partition(" ")[2]
                for entry in entries
                if entry.appointment == appointment
            )
            or "・なし"
        )
    # 一度だけ置換し、予定タイトル中の {work} 等をテンプレートとして再解釈しない。
    report = _SLOTS.sub(lambda m: values[m[1]], template)
    return report + ("\n" + THIRD_PARTY_TITLE_NOTE if entries else "")
