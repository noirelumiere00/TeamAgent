"""既存 documents の source 固有IDから欠損 modified_at を安全に補完する。

現在の対象は Slack のみ。external_id は ingest が ``<channel_id>:<epoch_seconds>`` で
生成しており、投稿時刻そのものを保持している。通常 Slack ingest は各チャネル最新100件の
1ページだけを読むため、過去行を全件補完する用途では本スクリプトを使う。

既定は dry-run。``--commit`` でも、NULL件数・ID形式・時刻範囲・更新件数のどれかが
想定外なら transaction を rollback して終了する。external_id や本文は出力しない。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg

_SLACK_EXTERNAL_ID_RE = re.compile(
    r"^(?P<channel>C[A-Z0-9]{10}):(?P<epoch>[0-9]{10}(?:\.[0-9]{1,6})?)$"
)
# data/ingest_sources.yaml の取り込み対象2チャネルと同じ fail-closed allowlist。
# 本番NULL 373件の prefix もこの2値だけ（153件 + 220件）とread-only監査済み。
_ALLOWED_SLACK_CHANNEL_IDS = frozenset({"C091ZSVTKF1", "C0A1207GYHZ"})
_EARLIEST_ALLOWED = dt.datetime(2010, 1, 1, tzinfo=dt.UTC)

_SELECT_NULL_SLACK = """
    SELECT id, external_id
    FROM documents
    WHERE source_type = 'slack'
      AND modified_at IS NULL
    ORDER BY id
"""
_UPDATE_ONE = """
    UPDATE documents
    SET modified_at = %s
    WHERE id = %s
      AND source_type = 'slack'
      AND modified_at IS NULL
"""
_COUNT_REMAINING = """
    SELECT count(*)
    FROM documents
    WHERE source_type = 'slack'
      AND modified_at IS NULL
"""


def slack_external_id_datetime(
    external_id: str,
    *,
    now: dt.datetime | None = None,
) -> dt.datetime | None:
    """厳密な ``channel:epoch`` だけを UTC datetime にする（推測しない）。"""
    match = _SLACK_EXTERNAL_ID_RE.fullmatch(str(external_id or ""))
    if match is None:
        return None
    if match.group("channel") not in _ALLOWED_SLACK_CHANNEL_IDS:
        return None
    try:
        epoch = Decimal(match.group("epoch"))
    except InvalidOperation:
        return None

    seconds = int(epoch)
    micros = int((epoch - Decimal(seconds)) * Decimal(1_000_000))
    try:
        parsed = dt.datetime.fromtimestamp(seconds, tz=dt.UTC).replace(microsecond=micros)
    except (OverflowError, OSError, ValueError):
        return None

    ceiling = (now or dt.datetime.now(dt.UTC)) + dt.timedelta(days=1)
    if parsed < _EARLIEST_ALLOWED or parsed > ceiling:
        return None
    return parsed


def _read_candidates(conn: Any, *, lock: bool) -> list[tuple[Any, str]]:
    sql = _SELECT_NULL_SLACK + (" FOR UPDATE" if lock else "")
    with conn.cursor() as cur:
        cur.execute(sql)
        return [(row[0], str(row[1] or "")) for row in cur.fetchall()]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="検証通過後に更新を確定する")
    parser.add_argument(
        "--expected-count",
        type=int,
        default=None,
        help="NULL Slack行の期待件数。異なれば更新せず終了する",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.commit and args.expected_count is None:
        raise SystemExit("--commit には --expected-count が必須です")
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("TEAMAGENT_DB_DSN")
    if not dsn:
        raise SystemExit("DATABASE_URL または TEAMAGENT_DB_DSN が必要です")
    if args.expected_count is not None and args.expected_count < 0:
        raise SystemExit("--expected-count は0以上で指定してください")

    with psycopg.connect(dsn) as conn:
        rows = _read_candidates(conn, lock=args.commit)
        parsed = [(row_id, slack_external_id_datetime(external_id)) for row_id, external_id in rows]
        valid = [(when, row_id) for row_id, when in parsed if when is not None]
        invalid_count = len(rows) - len(valid)
        report = {
            "mode": "commit" if args.commit else "dry-run",
            "null_slack_rows": len(rows),
            "valid_candidates": len(valid),
            "invalid_candidates": invalid_count,
            "updated": 0,
            "remaining_null_slack_rows": len(rows),
        }

        if args.expected_count is not None and len(rows) != args.expected_count:
            raise RuntimeError(
                f"NULL Slack件数が期待値と不一致: expected={args.expected_count} actual={len(rows)}"
            )
        if invalid_count:
            raise RuntimeError(
                f"安全に解釈できない Slack external_id が {invalid_count} 件あります"
            )
        if not args.commit:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0

        updated = 0
        if valid:
            with conn.cursor() as cur:
                cur.executemany(_UPDATE_ONE, valid)
                updated = cur.rowcount
        if updated != len(valid):
            raise RuntimeError(f"更新件数が不一致: expected={len(valid)} actual={updated}")

        with conn.cursor() as cur:
            cur.execute(_COUNT_REMAINING)
            remaining_row = cur.fetchone()
        if remaining_row is None:
            raise RuntimeError("Slack modified_at NULL の残件数を取得できませんでした")
        remaining = int(remaining_row[0])
        if remaining:
            raise RuntimeError(f"Slack modified_at NULL が {remaining} 件残りました")

        report["updated"] = updated
        report["remaining_null_slack_rows"] = remaining
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
