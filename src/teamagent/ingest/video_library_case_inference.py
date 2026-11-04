"""Opt-in daily inference: python -m teamagent.ingest.video_library_case_inference."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import uuid
from collections import Counter
from pathlib import Path

import structlog

from teamagent.adapters.bedrock_client import BedrockClient, _estimate_cost
from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest.video_library_case_models import (
    DEFAULT_MODEL,
    Case,
    Policy,
    Post,
    Prediction,
    application_prediction,
    input_hash,
    validate_prediction,
)
from teamagent.ingest.video_library_case_repository import CaseRepository
from teamagent.observability.logging_config import configure_logging
from teamagent.prompts.loader import load_prompt

logger = structlog.get_logger(__name__)
MAX_TOKENS = 2048


class BudgetExceededError(Exception):
    """The next request cannot be covered by the remaining budget."""


class Inference:
    def __init__(self, client: BedrockClient, *, max_cost_usd: float) -> None:
        if not math.isfinite(max_cost_usd) or max_cost_usd <= 0:
            raise ValueError("invalid_cost_limit")
        if "claude-haiku-" not in client.model_id or _estimate_cost(client.model_id, 1000, 1) <= 0:
            raise ValueError("unpriced_haiku_model")
        self.client = client
        self.max_cost_usd = max_cost_usd
        self.reserved_usd = 0.0
        self.cost_usd = 0.0
        self.prompt = load_prompt("case_inference", "v1", "system")

    def predict(self, post: Post, cases: list[Case], *, request_id: str) -> Prediction:
        system = (
            self.prompt + "\n" + json.dumps([c.model_dump() for c in cases], ensure_ascii=False)
        )
        body = post.model_dump_json()
        # UTF-8 bytes bound input tokens; reserve cache-write price and full output even on timeout.
        tokens = len(system.encode()) + len(body.encode()) + 1024
        reserve = _estimate_cost(
            self.client.model_id, tokens, MAX_TOKENS, cache_write_tokens=tokens
        )
        if self.reserved_usd + reserve > self.max_cost_usd:
            raise BudgetExceededError
        self.reserved_usd += reserve
        response = self.client.converse(
            messages=[{"role": "user", "content": [{"text": body}]}],
            request_id=request_id,
            system=system,
            temperature=0.1,
            max_tokens=MAX_TOKENS,
            cache_system=True,
            max_attempts=1,
        )
        self.cost_usd += response.usage.cost_usd
        if response.stop_reason != "end_turn":
            raise ValueError("incomplete_response")
        return validate_prediction(response.text, post, cases)


def run(
    repository: CaseRepository,
    inference: Inference,
    *,
    policy: Policy | None,
    limit: int,
    dry_run: bool,
    request_id: str,
) -> Counter[str]:
    if limit < 1:
        raise ValueError("invalid_case_limit")
    counts: Counter[str] = Counter()
    with repository.run_lock() as acquired:
        if not acquired:
            counts["locked"] += 1
            return counts
        cases, rows = repository.load()
        if policy and not policy.compatible(inference.client.model_id, inference.prompt, cases):
            raise ValueError("stale_evaluation_policy")
        for row in rows:
            post = Post.from_row(row)
            fingerprint = input_hash(
                post, cases, inference.client.model_id, inference.prompt, policy
            )
            if row.get("input_hash") == fingerprint:
                counts["unchanged"] += 1
                continue
            if counts["attempted"] >= limit:
                break
            # Invalidate stale decisions first; a failed attempt remains retryable.
            if not dry_run:
                repository.invalidate(row)
            counts["attempted"] += 1
            try:
                result = inference.predict(post, cases, request_id=request_id)
                status = (
                    policy.status(result)
                    if policy
                    else ("candidate" if result.match_type == "new" else "unknown")
                )
                if not dry_run:
                    if not repository.save(
                        row,
                        application_prediction(result, cases, post),
                        status,
                        fingerprint,
                        inference.client.model_id,
                        cases,
                    ):
                        counts["stale"] += 1
                        continue
                counts[status] += 1
            except BudgetExceededError:
                counts["attempted"] -= 1
                counts["budget_stop"] += 1
                break
            except Exception as exc:
                counts["failed"] += 1
                logger.warning(
                    "case_inference_failed", request_id=request_id, error_code=type(exc).__name__
                )
    return counts


def default_policy_path() -> Path:
    """The evaluated policy shipped in data/ (the image bakes data/ under WORKDIR /app).

    The package is a no-editable install in production, so parents[3] is not the repo root
    there; look in the repository first, then the working directory (same as the sync's yaml).
    """
    candidates = [Path(__file__).resolve().parents[3], Path.cwd()]
    for base in candidates:
        path = base / "data" / "video_library_case_policy.json"
        if path.is_file():
            return path
    return candidates[-1] / "data" / "video_library_case_policy.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Call Haiku but do not write decisions"
    )
    args = parser.parse_args(argv)
    configure_logging()
    request_id = uuid.uuid4().hex
    started = time.perf_counter()
    try:
        policy_path = Path(os.environ.get("VIDEO_LIBRARY_CASE_POLICY") or default_policy_path())
        policy = (
            Policy.model_validate_json(policy_path.read_text()) if policy_path.is_file() else None
        )
        inference = Inference(
            BedrockClient.from_env(
                model_id_override=os.environ.get("VIDEO_LIBRARY_CASE_MODEL", DEFAULT_MODEL)
            ),
            max_cost_usd=float(os.environ.get("VIDEO_LIBRARY_CASE_MAX_COST_USD", "1")),
        )
        counts = run(
            CaseRepository(PgVectorClient.from_env()),
            inference,
            policy=policy,
            limit=int(os.environ.get("VIDEO_LIBRARY_CASE_LIMIT", "100")),
            dry_run=args.dry_run,
            request_id=request_id,
        )
        logger.info(
            "case_inference_complete",
            request_id=request_id,
            counts=dict(counts),
            cost_usd=inference.cost_usd,
            reserved_usd=inference.reserved_usd,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return 0
    except Exception as exc:
        logger.error("case_inference_failed", request_id=request_id, error_code=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
