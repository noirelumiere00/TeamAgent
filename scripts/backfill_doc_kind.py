"""既存 documents に cls_is_template / cls_is_recurring をタイトルルールでバックフィルする。

検索ノイズ対策（テンプレ/雛形・定期報告の除外）は ingest.classify の 2 フラグを前提と
するが、既存 ~790 docs には付いていない。本スクリプトは **タイトルだけ** を読み、
決定論ルール ``teamagent.ingest.classify._kind_from_title`` を再利用して
``documents.metadata`` に ``cls_is_template`` / ``cls_is_recurring`` = "true" を追記する。

設計:
- Bedrock を一切呼ばない（コスト $0・タイトルは SELECT 済みの値のみ）。
- 既定 **dry-run**（対象件数とタイトル一覧を表示するだけ）。--commit で初めて UPDATE。
- フラグは **追記のみ**（true を付けるだけ・既存キーの削除/false 上書きはしない）。
  既に "true" が付いている doc はスキップ（冪等・再実行安全）。
- UPDATE は ``metadata = COALESCE(metadata,'{}'::jsonb) || %s::jsonb`` の JSONB マージ
  （他キー不変・migration 不要）。値は placeholder bind（injection 安全）。
- DB 接続は SSM ポートフォワード前提の --dsn 引数（無ければ DATABASE_URL）。
  実行は人間ゲート（本スクリプトを自動実行しない）。

Usage:
    # SSM ポートフォワードを張ってから（例: localhost:15432 → RDS）
    python scripts/backfill_doc_kind.py --dsn postgresql://user:pass@localhost:15432/db
    python scripts/backfill_doc_kind.py --dsn ... --limit 50          # 先頭 N 件だけ確認
    python scripts/backfill_doc_kind.py --dsn ... --commit            # 書き込み
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

import structlog  # noqa: E402

from teamagent.ingest.classify import _kind_from_title  # noqa: E402

logger = structlog.get_logger(__name__)


# -----------------------------------------------------------
# 純関数（DB 非依存・テスト対象）
# -----------------------------------------------------------
def plan_updates(
    rows: list[tuple[Any, str | None, str | None, str | None]],
) -> list[tuple[Any, str, dict[str, str]]]:
    """(id, title, 既存 cls_is_template, 既存 cls_is_recurring) 行から追記 patch を組む。

    _kind_from_title（決定論・LLM 非依存）でタイトル判定し、立てるべきフラグのうち
    **まだ "true" が付いていないものだけ** を patch に載せる（冪等）。patch が空の行は
    返さない。返り値: (doc_id, title, patch) のリスト。
    """
    updates: list[tuple[Any, str, dict[str, str]]] = []
    for doc_id, title, cur_template, cur_recurring in rows:
        is_template, is_recurring = _kind_from_title(title or "")
        patch: dict[str, str] = {}
        if is_template and cur_template != "true":
            patch["cls_is_template"] = "true"
        if is_recurring and cur_recurring != "true":
            patch["cls_is_recurring"] = "true"
        if patch:
            updates.append((doc_id, title or "", patch))
    return updates


# -----------------------------------------------------------
# DB 実行部
# -----------------------------------------------------------
def backfill(*, dsn: str, commit: bool, limit: int | None = None) -> dict[str, int]:
    """documents のタイトルを読み、2 フラグを JSONB マージで追記する。戻り値: 集計 dict。"""
    import psycopg

    stats = {"scanned": 0, "planned": 0, "updated": 0}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            q = (
                "SELECT id, title, "
                "metadata->>'cls_is_template' AS cur_template, "
                "metadata->>'cls_is_recurring' AS cur_recurring "
                "FROM documents ORDER BY id"
            )
            if limit:
                q += f" LIMIT {int(limit)}"
            cur.execute(q)
            rows = cur.fetchall()

        stats["scanned"] = len(rows)
        updates = plan_updates(list(rows))
        stats["planned"] = len(updates)
        logger.info(
            "backfill_doc_kind_plan",
            scanned=stats["scanned"],
            planned=stats["planned"],
            commit=commit,
        )
        # dry-run / commit 共通: 対象タイトル一覧を表示（人間レビュー用・本文は読まない）。
        for doc_id, title, patch in updates:
            flags = ",".join(sorted(patch))
            print(f"  [{flags}] {title}  (id={doc_id})")

        if commit and updates:
            with conn.cursor() as cur:
                for doc_id, _title, patch in updates:
                    cur.execute(
                        "UPDATE documents "
                        "SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb "
                        "WHERE id = %s",
                        (json.dumps(patch), doc_id),
                    )
                    stats["updated"] += 1
            conn.commit()
        logger.info("backfill_doc_kind_done", **stats)
    return stats


def main() -> int:
    import os

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dsn",
        default=None,
        help="Postgres DSN（SSM ポートフォワード前提）。省略時は DATABASE_URL",
    )
    p.add_argument("--commit", action="store_true", help="既定 dry-run。指定時のみ UPDATE")
    p.add_argument("--limit", type=int, default=None, help="先頭 N 件のみ（検証用）")
    args = p.parse_args()

    dsn = args.dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        print("[ERROR] --dsn か DATABASE_URL を指定してください", file=sys.stderr)
        return 2

    try:
        stats = backfill(dsn=dsn, commit=args.commit, limit=args.limit)
    except Exception as e:
        print(f"[ERROR] backfill failed: {e}", file=sys.stderr)
        return 2
    mode = "commit" if args.commit else "dry-run"
    print(
        f"scanned={stats['scanned']} planned={stats['planned']} "
        f"updated={stats['updated']} mode={mode}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
