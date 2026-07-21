#!/usr/bin/env python3
"""検索回答の4段階評価を、read-only で週次集計して Markdown に出力する。"""

from __future__ import annotations

import argparse
import html
import os
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

_SELECT_RATINGS_SQL = """
SELECT id, user_email, search_session_id, answer_id, query, score, note, created_at
FROM search_feedback
WHERE target_type = 'answer'
  AND score IS NOT NULL
  AND created_at >= NOW() - (%s * INTERVAL '1 day')
ORDER BY created_at DESC, id DESC
"""


@dataclass(frozen=True)
class RatingSummary:
    """集計済みの評価データと、出力に必要な機械判定値。"""

    rows: list[dict[str, Any]]
    source_count: int
    session_replaced_count: int
    answer_replaced_count: int
    score_counts: Counter[int]
    user_counts: Counter[str]

    @property
    def replacement_count(self) -> int:
        return self.session_replaced_count + self.answer_replaced_count

    @property
    def replacement_rate(self) -> float:
        if not self.source_count:
            return 0.0
        return self.replacement_count / self.source_count


def _row_key(row: Mapping[str, Any]) -> tuple[datetime, int]:
    """created_at と id による、仕様上の決定論的な新しさの順序を返す。"""
    created_at = row["created_at"]
    if not isinstance(created_at, datetime):
        raise TypeError("created_at must be a datetime")
    return created_at, int(row["id"])


def _latest_per_key(
    rows: Iterable[Mapping[str, Any]], key_name: str
) -> tuple[list[dict[str, Any]], int]:
    """キーがNULLでない行を最新1件にし、NULLの行は行単位で残す。"""
    input_rows = [dict(row) for row in rows]
    latest: dict[tuple[str, Any], dict[str, Any]] = {}
    null_key_rows: list[dict[str, Any]] = []

    for row in input_rows:
        value = row.get(key_name)
        if value is None:
            null_key_rows.append(row)
            continue
        key = (str(row["user_email"]), value)
        current = latest.get(key)
        if current is None or _row_key(row) > _row_key(current):
            latest[key] = row

    resolved = [*latest.values(), *null_key_rows]
    resolved.sort(key=_row_key, reverse=True)
    return resolved, len(input_rows) - len(resolved)


def eligible_ratings(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """4段階評価として有効な検索回答行だけを取り出す。"""
    return [
        dict(row)
        for row in rows
        if row.get("target_type", "answer") == "answer" and row.get("score") is not None
    ]


def resolve_ratings(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    """セッション、回答の順で付け直しを解決する。"""
    eligible = eligible_ratings(rows)
    by_session, session_replaced_count = _latest_per_key(eligible, "search_session_id")
    by_answer, answer_replaced_count = _latest_per_key(by_session, "answer_id")
    return by_answer, session_replaced_count, answer_replaced_count


def summarize_ratings(rows: Iterable[Mapping[str, Any]]) -> RatingSummary:
    """行dictの列から、表示用の集計値を作る。"""
    eligible = eligible_ratings(rows)
    resolved, session_replaced_count, answer_replaced_count = resolve_ratings(eligible)
    return RatingSummary(
        rows=resolved,
        source_count=len(eligible),
        session_replaced_count=session_replaced_count,
        answer_replaced_count=answer_replaced_count,
        score_counts=Counter(int(row["score"]) for row in resolved),
        user_counts=Counter(str(row["user_email"]) for row in resolved),
    )


def _markdown_cell(value: Any) -> str:
    return html.escape(str(value or "").replace("\r", "")).replace("|", "\\|").replace("\n", "<br>")


def _user_labels(user_counts: Mapping[str, int], with_emails: bool) -> dict[str, str]:
    if with_emails:
        return {email: email for email in user_counts}
    return {email: f"user{index}" for index, email in enumerate(sorted(user_counts), start=1)}


def render_markdown(summary: RatingSummary, days: int, *, with_emails: bool = False) -> str:
    """集計をMarkdownへ整形する。既定では送信者を匿名化する。"""
    labels = _user_labels(summary.user_counts, with_emails)
    active_users = len(summary.user_counts)
    weekly_count = len(summary.rows)
    weekly_equivalent_count = weekly_count / days * 7
    low_ratings = sorted(
        (row for row in summary.rows if int(row["score"]) <= 2),
        key=lambda row: (int(row["score"]), *_row_key(row)),
    )[:10]
    note_rows = [row for row in summary.rows if row.get("note") is not None]

    lines = [
        "# 検索回答の評価サマリ",
        "",
        "## 期間サマリ",
        "",
        f"対象期間: 直近 {days} 日",
        f"入力件数: {weekly_count}",
        f"入力者数: {active_users}",
        "score 分布: "
        + " / ".join(f"{score}: {summary.score_counts[score]}" for score in range(4, 0, -1)),
        (
            "付け直し率: "
            f"{summary.replacement_rate:.1%} "
            f"({summary.replacement_count}/{summary.source_count} 件を最新評価へ集約)"
        ),
        "",
        "## 低評価（score <= 2）クエリ Top 10",
        "",
        "| クエリ | score | コメント |",
        "| --- | ---: | --- |",
    ]
    lines.extend(
        f"| {_markdown_cell(row.get('query'))} | {row['score']} | {_markdown_cell(row.get('note'))} |"
        for row in low_ratings
    )
    if not low_ratings:
        lines.append("| 該当なし | - | - |")

    weekly_judgement = (
        f"- 週間件数: {weekly_count} / 閾値 10 → {'満たす' if weekly_count >= 10 else '未達'}"
        if days == 7
        else (
            f"- 週間換算件数: {weekly_equivalent_count:g} "
            f"(直近 {days} 日の入力 {weekly_count} 件、--days {days} のため週間閾値は換算) "
            f"/ 閾値 10 → {'満たす' if weekly_equivalent_count >= 10 else '未達'}"
        )
    )
    lines.extend(
        [
            "",
            "## コメント一覧",
            "",
            "| クエリ | score | コメント |",
            "| --- | ---: | --- |",
        ]
    )
    lines.extend(
        f"| {_markdown_cell(row.get('query'))} | {row['score']} | {_markdown_cell(row.get('note'))} |"
        for row in note_rows
    )
    if not note_rows:
        lines.append("| 該当なし | - | - |")

    lines.extend(
        [
            "",
            "## 形骸化判定",
            "",
            f"- 入力者数: {active_users} / 閾値 5 → {'満たす' if active_users >= 5 else '未達'}",
            weekly_judgement,
            "",
            "## per-user 送信件数",
            "",
            "| 送信者 | 件数 |",
            "| --- | ---: |",
        ]
    )
    lines.extend(
        f"| {labels[email]} | {count} |" for email, count in sorted(summary.user_counts.items())
    )
    if not summary.user_counts:
        lines.append("| 該当なし | 0 |")

    return "\n".join(lines) + "\n"


def fetch_ratings(dsn: str, days: int) -> list[dict[str, Any]]:
    """管理用DSNで対象期間の評価行を読む。"""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT_RATINGS_SQL, (days,))
            return list(cur.fetchall())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="検索回答の評価サマリをMarkdownで出力します。")
    parser.add_argument("--days", type=int, default=7, help="集計対象の日数（既定: 7）")
    parser.add_argument("--out", type=Path, help="Markdownの出力先。未指定時は標準出力")
    parser.add_argument(
        "--with-emails",
        action="store_true",
        help="per-user欄に実名メールアドレスを出力します。実名版はSlackへ流さないでください。",
    )
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days は正の整数にしてください")
    return args


def main() -> None:
    args = parse_args()
    dsn = os.getenv("ANSWER_RATING_DB_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("ANSWER_RATING_DB_URL または DATABASE_URL を設定してください")
    output = render_markdown(
        summarize_ratings(fetch_ratings(dsn, args.days)), args.days, with_emails=args.with_emails
    )
    if args.out:
        args.out.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
