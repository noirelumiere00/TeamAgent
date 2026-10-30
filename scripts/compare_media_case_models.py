"""Compare paid distillation of saved Apify posts; never starts Actors or writes corpus DB.

JSON goes to stdout, a short table to stderr. JSONL accepts raw Apify dataset rows.
--kvs supplies their original Apify image/subtitle store (also for local JSONL).
CostGuard reservations/settlements still write the existing cost ledger.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from contextlib import redirect_stdout
from itertools import combinations
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from teamagent.adapters.apify_client import ApifyClient, ApifyRunResult
from teamagent.adapters.apify_media_cases import ApifyCaseAssets, PostAssets
from teamagent.adapters.cost_guard import CostGuard, CostLimitExceededError
from teamagent.skills.media_case_search.distill import MODELS, CaseDistiller, normalize_post


class ComparisonAssets(ApifyCaseAssets):
    """Freeze successful and missing assets so every model sees the same input."""

    def __init__(self, client: ApifyClient, run: ApifyRunResult) -> None:
        super().__init__(client, run)
        self.refs: dict[str, PostAssets] = {}
        self.images: dict[str, tuple[bytes, str] | Exception] = {}

    def for_post(self, item: dict[str, Any], post_id: str) -> PostAssets:
        if post_id not in self.refs:
            self.refs[post_id] = super().for_post(item, post_id)
        return self.refs[post_id]

    def image(self, key: str) -> tuple[bytes, str]:
        if key not in self.images:
            try:
                self.images[key] = super().image(key)
            except Exception as exc:
                self.images[key] = exc
        value = self.images[key]
        if isinstance(value, Exception):
            raise value
        return value


def select_posts(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    from teamagent.skills.media_case_search.skill import accounts

    if limit < 1:
        raise ValueError("limit must be positive")
    posts: dict[str, dict[str, Any]] = {}
    approved = accounts()
    for raw in rows:
        match = re.fullmatch(
            r"https://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]+)/(?:photo|video)/[0-9]+",
            str(raw.get("webVideoUrl") or ""),
        )
        if not match or match[1] not in approved:
            raise ValueError("unapproved or invalid post")
        post = normalize_post(raw, match[1])
        if post["id"] in posts and posts[post["id"]] != raw:
            raise ValueError("conflicting duplicate post")
        posts[post["id"]] = raw
    if not posts:
        raise ValueError("no posts")
    return [posts[key] for key in sorted(posts, key=int)[:limit]]


def summarize(results: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    models = {}
    for model, rows in results.items():
        total_cost = sum(row["cost_usd"] for row in rows)
        elapsed = sum(row["elapsed_s"] for row in rows)
        models[model] = {
            "posts": len(rows),
            "failed": sum(row["status"] != "ok" for row in rows),
            "cost_usd": total_cost,
            "cost_per_post_usd": total_cost / len(rows) if rows else None,
            "elapsed_s": elapsed,
            "seconds_per_post": elapsed / len(rows) if rows else None,
        }
    agreement = []
    for left, right in combinations(results, 2):
        a = {row["post_id"]: row for row in results[left] if row["status"] == "ok"}
        b = {row["post_id"]: row for row in results[right] if row["status"] == "ok"}
        common = sorted(a.keys() & b.keys())
        fields = {}
        for field in ("brand", "campaign", "category"):
            matches = both_empty = 0
            for post_id in common:
                av = {item[field] for item in a[post_id]["items"] if item.get(field)}
                bv = {item[field] for item in b[post_id]["items"] if item.get(field)}
                matches += av == bv
                both_empty += not av and not bv
            fields[field] = {
                "matches": matches,
                "compared": len(common),
                "both_empty": both_empty,
                "rate": matches / len(common) if common else None,
            }
        agreement.append({"models": [left, right], "fields": fields})
    return {"models": models, "agreement": agreement}


def compare(
    rows: list[dict[str, Any]], assets: ApifyCaseAssets, distillers: dict[str, CaseDistiller]
) -> dict[str, Any]:
    results: dict[str, list[dict[str, Any]]] = {model: [] for model in distillers}
    stopped = False
    for raw in rows:
        handle = str(raw["webVideoUrl"]).split("/@", 1)[1].split("/", 1)[0]
        post = normalize_post(raw, handle)
        if isinstance(assets, ComparisonAssets):
            refs = assets.for_post(raw, post["id"])
            keys = [refs.cover_key] if refs.cover_key else []
            if post["mediaType"] == "photo":
                keys.extend([key for key in refs.slide_keys if key != refs.cover_key][:10])
            for key in keys:
                try:
                    assets.image(key)
                except Exception:
                    pass  # Distillation records missing assets through its usual warnings.
        for model, distiller in distillers.items():
            before = distiller.settled_cost_usd
            started = time.perf_counter()
            row: dict[str, Any] = {"post_id": post["id"], "status": "ok"}
            try:
                metadata, _, _ = distiller.analyze(
                    post, raw, assets, f"compare:{post['id']}:{model}", {}
                )
                if metadata is None:
                    raise ValueError("missing analysis")
                row.update(
                    {
                        key: metadata[key]
                        for key in (
                            "items",
                            "model",
                            "thinking",
                            "image_count",
                            "warnings",
                            "coverage",
                            "input_hash",
                        )
                    }
                )
                if not metadata["analysis_complete"]:
                    row.update(status="failed", error="IncompleteAnalysis")
            except Exception as exc:
                row.update(status="failed", error=type(exc).__name__)
                if isinstance(exc, ClientError):
                    row["error_code"] = exc.response.get("Error", {}).get("Code")
                if isinstance(exc, CostLimitExceededError):
                    stopped = True
            row.update(
                cost_usd=distiller.settled_cost_usd - before,
                elapsed_s=time.perf_counter() - started,
            )
            results[model].append(row)
            if stopped:
                break
        if stopped:
            break
        if isinstance(assets, ComparisonAssets):
            assets.images.clear()
            assets.refs.clear()
    return {
        "selection": {
            "order": "post_id ascending, deduplicated",
            "post_ids": [str(r["id"]) for r in rows],
        },
        "agreement_method": "Per-post exact equality of non-null value sets; successful pairs only; not accuracy.",
        "cost_method": "CostGuard settled USD; unknown failed calls retain the per-attempt reservation.",
        "timing_method": "Distillation including retries and cost ledger; shared asset downloads excluded.",
        "stopped_on_budget": stopped,
        **summarize(results),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset")
    source.add_argument("--jsonl", type=Path)
    parser.add_argument("--kvs", required=True, help="Original Apify KVS ID or name")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    args = parser.parse_args(argv)
    if args.limit < 1 or len(set(args.models)) < 2:
        parser.error("--limit must be positive and --models needs at least two distinct models")
    try:
        with redirect_stdout(sys.stderr):
            ledger = CostGuard.from_env()
            if ledger is None:
                raise CostLimitExceededError("cost ledger required")
            apify = ApifyClient.from_env(ledger=ledger)
            if args.dataset:
                run = apify.read_media_dataset(args.dataset, args.kvs)
            else:
                rows = [
                    json.loads(line) for line in args.jsonl.read_text().splitlines() if line.strip()
                ]
                run = ApifyRunResult(
                    rows, "", "", "SUCCEEDED", 0, kvs_id=apify.resolve_kvs_id(args.kvs)
                )
            selected = select_posts(run.items, args.limit)
            assets = ComparisonAssets(apify, run)
            distillers = {
                model: CaseDistiller.from_env(ledger, model=model)
                for model in dict.fromkeys(args.models)
            }
            report = compare(selected, assets, distillers)
            report["source"] = {
                "dataset": args.dataset,
                "jsonl": str(args.jsonl) if args.jsonl else None,
                "kvs": args.kvs,
            }
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("model | posts | failed | USD/post | seconds/post", file=sys.stderr)
    for model, metrics in report["models"].items():
        cost, seconds = metrics["cost_per_post_usd"], metrics["seconds_per_post"]
        cost_text = f"{cost:.6f}" if cost is not None else "-"
        seconds_text = f"{seconds:.2f}" if seconds is not None else "-"
        print(
            f"{model} | {metrics['posts']} | {metrics['failed']} | {cost_text} | {seconds_text}",
            file=sys.stderr,
        )
    for pair in report["agreement"]:
        rates = [
            f"{field}={m['matches']}/{m['compared']} ({m['rate']:.1%})"
            if m["rate"] is not None
            else f"{field}=N/A"
            for field, m in pair["fields"].items()
        ]
        print(f"{' / '.join(pair['models'])}: {', '.join(rates)}", file=sys.stderr)
    return 2 if any(m["failed"] for m in report["models"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
