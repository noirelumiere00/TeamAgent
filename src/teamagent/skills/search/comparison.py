"""検索で取得した施策集計の比較。欠測を補完せず、勝敗と表はコードで確定する。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.search.campaign_files import campaign_key, is_campaign_hit
from teamagent.skills.search.client_match import names_overlap
from teamagent.skills.search.result_guard import find_client_mention

_COMPARISON = re.compile(r"比較|比べ|何が効|どれが効|どっちが|どちらが|勝ち|効果")
_NUMBER = r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)(?=\s|、|。|$)"
_METRICS = (
    ("本数", r"^投稿本数[:：]\s*"),
    ("再生合計", r"^再生数[:：]\s*合計\s*"),
    ("再生平均", r"^再生数[:：][^\n]*?平均\s*"),
    ("再生最大", r"^再生数[:：][^\n]*?最大\s*"),
    ("保存平均", r"^反応[:：][^\n]*?保存\s*平均\s*"),
)


def is_comparison_query(query: str) -> bool:
    """一覧語と併記されても、比較・効果を問う依頼は要約を省かない。"""
    return bool(_COMPARISON.search(query))


def named_comparison_clients(query: str, vocabulary: Sequence[str]) -> list[str]:
    """既存の語境界検査で名指しを拾い、長い法人名・別名の重複を除く。"""
    if not is_comparison_query(query):
        return []
    clients: list[str] = []
    for name in sorted(vocabulary, key=len, reverse=True):
        if find_client_mention(query, [name], strict=True) and not any(
            names_overlap(name, previous) for previous in clients
        ):
            clients.append(name)
    return clients if len(clients) >= 2 else []


def _comparison_rows(
    hits: list[SearchHit],
) -> dict[tuple[str, str], tuple[int, list[Decimal | None]]]:
    rows: dict[tuple[str, str], tuple[int, list[Decimal | None]]] = {}
    for hit in hits:
        key = campaign_key(hit) if is_campaign_hit(hit) else None
        if not key or key in rows:
            continue
        values: list[Decimal | None] = []
        for _, pattern in _METRICS:
            match = re.search(pattern + _NUMBER, hit.content, re.MULTILINE)
            values.append(Decimal(match[1].replace(",", "")) if match else None)
        rows[key] = (hit.chunk_id, values)
    return rows


def _label(key: tuple[str, str]) -> str:
    return " / ".join(key).replace("|", "／").replace("\n", " ")


def compact_comparison(query: str, hits: list[SearchHit]) -> tuple[str, list[int]]:
    """出典3件の範囲で、同じ指標・平均同士だけを最大2文で比較する。"""
    if not is_comparison_query(query):
        return "", []
    rows = _comparison_rows(hits)
    if not 2 <= len(rows) <= 3:
        return "", []
    units = {"再生平均": "回", "保存平均": "件"}
    lines: list[str] = []
    for i, (metric, _) in enumerate(_METRICS):
        if metric not in units:
            continue
        values = {key: values[i] for key, (_, values) in rows.items()}
        if any(value is None for value in values.values()):
            continue
        known = {key: value for key, value in values.items() if value is not None}
        best = max(known.values())
        winners = [_label(key) for key, value in known.items() if value == best]
        result = "同率" if len(winners) == len(rows) else "・".join(winners) + "が上"
        stats = "、".join(
            f"{_label(key)}が{value:,}{units[metric]}" for key, value in known.items()
        )
        lines.append(f"{metric}は{stats}で、{result}です。")
    return "\n".join(lines), [chunk_id for chunk_id, _ in rows.values()] if lines else []


def comparison_table(query: str, hits: list[SearchHit]) -> str:
    """同一施策の重複チャンクを除外し、2施策以上のときだけ表を返す。"""
    if not is_comparison_query(query):
        return ""
    rows = {key: values for key, (_, values) in _comparison_rows(hits).items()}
    if len(rows) < 2:
        return ""

    lines = [
        "# 施策比較表（取得資料の集計値）",
        "| 施策 | 本数 | 再生合計 | 再生平均 | 再生最大 | 保存平均 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, values in rows.items():
        cells = ["なし" if value is None else f"{value:,}" for value in values]
        lines.append(f"| {_label(key)} | " + " | ".join(cells) + " |")
    conclusions: dict[str, list[str]] = {}
    for i, (metric, _) in enumerate(_METRICS):
        values_present = [value for values in rows.values() if (value := values[i]) is not None]
        if len(values_present) != len(rows):
            result = "比較不可（欠測あり）"
        else:
            best = max(values_present)
            winners = [_label(key) for key, values in rows.items() if values[i] == best]
            result = "同率" if len(winners) == len(rows) else "・".join(winners) + "が上"
        lines.append(f"{metric}: {result}")
        if metric != "本数":
            conclusions.setdefault(result, []).append(metric)
    lines.append(
        "結論: "
        + "、".join(f"{'・'.join(metrics)}は{result}" for result, metrics in conclusions.items())
        + "です。"
    )
    lines.append(
        "集計値の大小であり、施策の因果効果を示すものではありません。保存平均の欠測は補完しません。"
    )
    return "\n".join(lines)
