"""検索で取得した施策集計の比較。欠測を補完せず、勝敗と表はコードで確定する。"""

from __future__ import annotations

import re
from decimal import Decimal

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.search.campaign_files import campaign_key, is_campaign_hit

_COMPARISON = re.compile(r"比較|何が効|どっちが|どちらが|勝ち|効果")
_NUMBER = r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)(?=\s|、|。|$)"
_METRICS = (
    ("本数", r"^投稿本数[:：]\s*"),
    ("再生合計", r"^再生数[:：]\s*合計\s*"),
    ("再生平均", r"^再生数[:：][^\n]*?平均\s*"),
    ("再生最大", r"^再生数[:：][^\n]*?最大\s*"),
    ("保存平均", r"^反応[:：][^\n]*?保存\s*平均\s*"),
)


def comparison_table(query: str, hits: list[SearchHit]) -> str:
    """同一施策の重複チャンクを除外し、2施策以上のときだけ表を返す。"""
    if not _COMPARISON.search(query):
        return ""
    rows: dict[tuple[str, str], list[Decimal | None]] = {}
    for hit in hits:
        key = campaign_key(hit) if is_campaign_hit(hit) else None
        if not key or key in rows:
            continue
        values: list[Decimal | None] = []
        for _, pattern in _METRICS:
            match = re.search(pattern + _NUMBER, hit.content, re.MULTILINE)
            values.append(Decimal(match[1].replace(",", "")) if match else None)
        rows[key] = values
    if len(rows) < 2:
        return ""

    def label(key: tuple[str, str]) -> str:
        return " / ".join(key).replace("|", "／").replace("\n", " ")

    lines = [
        "# 施策比較表（取得資料の集計値）",
        "| 施策 | 本数 | 再生合計 | 再生平均 | 再生最大 | 保存平均 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, values in rows.items():
        cells = ["なし" if value is None else f"{value:,}" for value in values]
        lines.append(f"| {label(key)} | " + " | ".join(cells) + " |")
    for i, (metric, _) in enumerate(_METRICS):
        values_present = [value for values in rows.values() if (value := values[i]) is not None]
        if len(values_present) != len(rows):
            result = "比較不可（欠測あり）"
        else:
            best = max(values_present)
            winners = [label(key) for key, values in rows.items() if values[i] == best]
            result = "同率" if len(winners) == len(rows) else "・".join(winners) + "が上"
        lines.append(f"{metric}: {result}")
    lines.append(
        "集計値の大小であり、施策の因果効果を示すものではありません。保存平均の欠測は補完しません。"
    )
    return "\n".join(lines)
