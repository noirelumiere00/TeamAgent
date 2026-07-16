"""取り込み鮮度の監視（サイレント故障の検知）。

背景: 2026-07-13、Slack の取り込みが 5/28 以降 6 週間止まっていたのに誰も気づかず、
共有された営業ナレッジが検索に載らない状態が続いた（原因: 週次スケジュール DISABLED＋
手動再取込が Drive/Sheets のみ）。「ある source_type の最新取り込みが N 日以上遅れたら
気づける」仕組みが無かったのが真因。本モジュールは documents の source_type ごとの
最新 ingested_at を見て、閾値を超えて古い（or 1件も無い）ソースを列挙する。

read-only（SELECT のみ）。ingest run 末尾で呼ばれ、stale を検出したら ops へ通知する。
"""

from __future__ import annotations

import datetime as _dt
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# 監視対象ソースと既定の許容遅延（日）。週次取り込み＋猶予で 8 日。
# env INGEST_FRESHNESS_MAX_AGE_DAYS で一括上書き可（source 個別は将来必要なら拡張）。
_DEFAULT_MAX_AGE_DAYS = 8
_MONITORED_SOURCES = ("slack", "gdrive", "gsheets")


@dataclass(frozen=True)
class StaleSource:
    """閾値を超えて古い（or 未取り込みの）source_type 1 件。"""

    source_type: str
    newest: _dt.datetime | None  # 最新の ingested_at（1件も無ければ None）
    age_days: float | None  # 経過日数（None なら未取り込み）
    threshold_days: int

    @property
    def reason(self) -> str:
        if self.newest is None:
            return "1件も取り込まれていない"
        return f"最終取り込みから {self.age_days:.1f} 日経過（閾値 {self.threshold_days} 日）"


def max_age_days_from_env() -> int:
    """INGEST_FRESHNESS_MAX_AGE_DAYS を読む（未設定/不正は既定 8）。"""
    raw = os.environ.get("INGEST_FRESHNESS_MAX_AGE_DAYS", "").strip()
    if not raw:
        return _DEFAULT_MAX_AGE_DAYS
    try:
        v = int(raw)
        return v if v > 0 else _DEFAULT_MAX_AGE_DAYS
    except ValueError:
        return _DEFAULT_MAX_AGE_DAYS


def find_stale_sources(
    cursor: Any,
    *,
    now: _dt.datetime,
    max_age_days: int,
    monitored: tuple[str, ...] = _MONITORED_SOURCES,
) -> list[StaleSource]:
    """documents の source_type 別 max(ingested_at) を見て stale を列挙する（read-only）。

    - monitored のうち閾値超過のもの、および 1 件も無いものを StaleSource で返す。
    - cursor は execute/fetchall を持つ DBAPI カーソル（本番は admin role 接続）。
    """
    cursor.execute(
        "SELECT source_type, max(ingested_at) AS newest FROM documents GROUP BY source_type"
    )
    newest_by_source: dict[str, _dt.datetime | None] = {}
    for row in cursor.fetchall():
        if isinstance(row, Mapping):
            source_value = row.get("source_type")
            newest = row.get("newest", row.get("max"))
        else:
            source_value = row[0]
            newest = row[1]
        src = str(source_value) if source_value is not None else ""
        newest_by_source[src] = newest

    stale: list[StaleSource] = []
    for src in monitored:
        newest = newest_by_source.get(src)
        if newest is None:
            stale.append(StaleSource(src, None, None, max_age_days))
            continue
        # tz-naive で来た場合は UTC とみなす（DB は timestamptz だが保険）。
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=_dt.UTC)
        age = (now - newest).total_seconds() / 86400.0
        if age > max_age_days:
            stale.append(StaleSource(src, newest, age, max_age_days))
    return stale
