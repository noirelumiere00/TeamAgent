"""Administrator-only initial media corpus import. Preview by default; --commit writes.

Reuse saved acquisition (no Actor start):
  python scripts/ingest_media_cases.py --dataset p7MhpFwAJg1pF0M8g \
    --kvs aico-media-cases-newstv10-20261028 --commit

Explicit paid acquisition, at most 400 posts/account and 3 concurrent accounts:
  python scripts/ingest_media_cases.py --fetch --commit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@contextmanager
def report_progress(progress: Any, interval_s: float = 10) -> Iterator[None]:
    started = time.monotonic()
    stop = threading.Event()

    def report() -> None:
        processed, total, failed = progress.snapshot()
        print(
            f"media_cases 処理済み={processed}/{total} 失敗={failed} "
            f"経過秒={time.monotonic() - started:.0f}",
            file=sys.stderr,
            flush=True,
        )

    def run() -> None:
        while not stop.wait(interval_s):
            report()

    thread = threading.Thread(target=run, daemon=True, name="media-progress")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        report()


def main(argv: list[str] | None = None, *, skill: Any | None = None) -> int:
    from teamagent.skills.media_case_search.skill import (
        IngestProgress,
        MediaCaseSearchSkill,
        accounts,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", help="Existing Apify dataset ID (GET only)")
    source.add_argument("--fetch", action="store_true", help="Start paid profiles runs")
    parser.add_argument("--kvs", default="aico-media-cases-newstv10-20261028")
    parser.add_argument("--accounts", nargs="+", default=list(accounts()))
    parser.add_argument("--commit", action="store_true", help="Enable DB writes and Gemini charges")
    args = parser.parse_args(argv)
    handles = list(dict.fromkeys(args.accounts))
    if any(h not in accounts() for h in handles):
        parser.error("accounts must come from the approved ten-account CSV")
    if not args.commit:
        print(
            json.dumps(
                {
                    "mode": "preview",
                    "accounts": handles,
                    "dataset": args.dataset,
                    "kvs": args.kvs if args.dataset else None,
                    "posts_per_account": 400,
                    "max_parallel_accounts": 3,
                    "max_parallel_posts_per_account": 6,
                    "max_parallel_gemini_calls": 12,
                    "gemini_monthly_usd": os.environ.get("COST_MEDIA_CASE_GEMINI_MONTHLY_USD"),
                    "actor_start": args.fetch,
                    "note": "No connections. --commit enables database writes and Gemini costs.",
                },
                ensure_ascii=False,
            )
        )
        return 0
    try:
        progress = IngestProgress()
        with report_progress(progress):
            result = (skill or MediaCaseSearchSkill()).ingest_initial(
                handles, dataset_id=args.dataset, kvs=args.kvs, progress=progress
            )
    except Exception as exc:
        # SDK errors may contain signed URLs or credentials; emit only the error class.
        print(json.dumps({"status": "failed", "error": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result["failed"] or result["gaps"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
