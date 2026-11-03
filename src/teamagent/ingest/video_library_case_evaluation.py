"""Held-out calibration for automatic case links; no production labels are changed."""

from __future__ import annotations

import argparse
import json
import random
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest.video_library_case_inference import BudgetExceededError, Inference
from teamagent.ingest.video_library_case_models import (
    PROMPT_VERSION,
    Case,
    Gate,
    Policy,
    Post,
    Prediction,
    StrictModel,
    application_prediction,
    catalog_hash,
    digest,
)
from teamagent.ingest.video_library_case_repository import CaseRepository
from teamagent.observability.logging_config import configure_logging


class Trial(StrictModel):
    key: str
    truth: Case
    known_advertiser: bool
    withheld: bool
    prediction: Prediction | None
    error_code: str | None = None
    application: Prediction | None = None
    caption: str = ""


def stratified_sample(rows: list[dict[str, Any]], size: int, seed: int) -> list[dict[str, Any]]:
    """Round robin over cases, then account/month/caption-length strata; deduplicate reposts."""
    # Reproducible sampling, not a security token.
    rng = random.Random(seed)  # nosec B311
    buckets: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    for row in sorted(rows, key=lambda r: (r["platform"], r["video_id"])):
        identity = digest([row.get("caption"), row.get("account_handle"), row.get("account_name")])
        if identity in seen:
            continue
        seen.add(identity)
        key: tuple[str, ...] = (
            row["advertiser"],
            row["campaign"],
            row.get("account_handle") or "",
            str(row.get("posted_at") or "")[:7],
            str(len(row.get("caption") or "") // 200),
        )
        buckets[key].append(row)
    cases: dict[tuple[str, str], deque[deque[dict[str, Any]]]] = defaultdict(deque)
    for key in sorted(buckets):
        rng.shuffle(buckets[key])
        cases[(key[0], key[1])].append(deque(buckets[key]))
    order = list(cases)
    rng.shuffle(order)
    pending = deque(cases[key] for key in order)
    result: list[dict[str, Any]] = []
    while pending and len(result) < size:
        strata = pending.popleft()
        bucket = strata.popleft()
        result.append(bucket.popleft())
        if bucket:
            strata.append(bucket)
        if strata:
            pending.append(strata)
    return result


def _eligible(trial: Trial, kind: str, threshold: float) -> bool:
    p = trial.prediction
    return bool(
        p
        and p.advertiser
        and p.confidence >= threshold
        and (
            (kind == "existing" and p.match_type == "existing")
            or (kind == "advertiser_only" and p.match_type == "unknown" and p.campaign is None)
        )
    )


def _correct(trial: Trial, kind: str) -> bool:
    p = trial.application or trial.prediction
    return bool(
        p
        and p.advertiser == trial.truth.advertiser
        and (
            kind == "advertiser_only"
            or p.campaign is None
            or (not trial.withheld and p.campaign == trial.truth.campaign)
        )
    )


def _exact(trial: Trial) -> bool:
    p = trial.prediction
    return bool(p and (p.advertiser, p.campaign) == (trial.truth.advertiser, trial.truth.campaign))


def metrics(trials: list[Trial], kind: str = "existing", threshold: float = 0.0) -> dict[str, Any]:
    selected = [t for t in trials if _eligible(t, kind, threshold)]
    correct = sum(_correct(t, kind) for t in selected)
    present = [t for t in trials if not t.withheld]
    known = [t for t in present if t.known_advertiser]
    return {
        "n": len(trials),
        "selected": len(selected),
        "correct": correct,
        "precision": correct / len(selected) if selected else 0.0,
        "proposal_rate": len(selected) / len(trials) if trials else 0.0,
        "advertiser_accuracy": sum(
            bool(t.prediction and t.prediction.advertiser == t.truth.advertiser) for t in present
        )
        / len(present)
        if present
        else 0.0,
        "exact_accuracy": sum(_exact(t) for t in present) / len(present) if present else 0.0,
        "known_campaign_accuracy": sum(_exact(t) for t in known) / len(known) if known else 0.0,
        "unknown_rate": sum(
            t.prediction is None or t.prediction.match_type == "unknown" for t in trials
        )
        / len(trials)
        if trials
        else 0.0,
        "invalid_outputs": sum(t.error_code in {"ValueError", "ValidationError"} for t in trials),
        "withheld_existing_errors": sum(
            bool(t.withheld and t.prediction and t.prediction.match_type == "existing")
            for t in trials
        ),
    }


def calibrate(tuning: list[Trial], final: list[Trial], kind: str) -> Gate | None:
    thresholds = sorted({t.prediction.confidence for t in tuning if t.prediction})
    threshold = next(
        (
            v
            for v in thresholds
            if metrics(tuning, kind, v)["selected"] > 0
            and metrics(tuning, kind, v)["precision"] >= 0.97
        ),
        None,
    )
    if threshold is None:
        return None
    score = metrics(final, kind, threshold)
    if score["selected"] < 30 or score["precision"] < 0.97 or score["withheld_existing_errors"]:
        return None
    return Gate(threshold=threshold, selected=score["selected"], correct=score["correct"])


def evaluate(
    rows: list[dict[str, Any]], cases: list[Case], inference: Inference, *, seed: int
) -> tuple[Policy, dict[str, Any]]:
    trials: list[Trial] = []
    for index, row in enumerate(rows):
        truth = Case(advertiser=row["advertiser"], campaign=row["campaign"])
        post = Post.from_row(row)
        known = index % 2 == 0
        if not known:
            post.advertiser = None
        withheld = index % 5 == 4
        candidates = [c for c in cases if c != truth] if withheld else cases
        prediction = None
        error = None
        try:
            prediction = inference.predict(post, candidates, request_id=uuid.uuid4().hex)
        except BudgetExceededError:
            raise  # Never publish a policy from a partial evaluation.
        except Exception as exc:
            error = type(exc).__name__
        trials.append(
            Trial(
                key=digest([row["platform"], row["video_id"], post.model_dump()]),
                truth=truth,
                known_advertiser=known,
                withheld=withheld,
                prediction=prediction,
                error_code=error,
                application=application_prediction(prediction, candidates) if prediction else None,
                caption=post.caption[:80],
            )
        )
    # Randomize after drawing balanced strata so tuning/final both cover the catalog.
    rng = random.Random(seed)  # nosec B311
    rng.shuffle(trials)
    tuning, final = trials[:50], trials[50:]
    policy = Policy(
        application_version="v2",
        model=inference.client.model_id,
        prompt_version=PROMPT_VERSION,
        prompt_hash=digest(inference.prompt),
        catalog_hash=catalog_hash(cases),
        evaluation_hash=digest([t.model_dump() for t in trials]),
        existing=calibrate(tuning, final, "existing"),
        advertiser_only=calibrate(tuning, final, "advertiser_only"),
    )
    threshold = policy.existing.threshold if policy.existing else 0.0
    report = {
        "seed": seed,
        "tuning_n": len(tuning),
        "final": metrics(final, threshold=threshold),
        "by_case": {
            digest(c.model_dump()): metrics([t for t in final if t.truth == c], threshold=threshold)
            for c in cases
        },
        "incorrect_applications": [
            {
                "key": t.key,
                "split": split,
                "truth": t.truth.model_dump(),
                "prediction": t.prediction.model_dump(),
                "application": (t.application or t.prediction).model_dump(),
                "caption": t.caption[:80],
                "status": policy.status(t.prediction),
            }
            for split, subset in (("tuning", tuning), ("final", final))
            for t in subset
            if t.prediction
            and any(
                _eligible(t, kind, gate.threshold if gate else 0.0) and not _correct(t, kind)
                for kind, gate in (
                    ("existing", policy.existing),
                    ("advertiser_only", policy.advertiser_only),
                )
            )
        ],
        "policy": policy.model_dump(),
        "cost_usd": inference.cost_usd,
        "reserved_usd": inference.reserved_usd,
    }
    return policy, report


def main(argv: list[str] | None = None) -> int:
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--policy-output", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=150)
    parser.add_argument("--seed", type=int, default=792)
    args = parser.parse_args(argv)
    if args.sample_size < 150:
        parser.error("at least 150 labelled videos are required")
    configure_logging()
    request_id = uuid.uuid4().hex
    from teamagent.ingest.video_library_case_inference import logger
    from teamagent.ingest.video_library_case_models import DEFAULT_MODEL

    try:
        repo = CaseRepository(PgVectorClient.from_env())
        with repo.connection() as conn:
            cases = repo.cases(conn)
            rows = list(
                conn.execute(
                    """SELECT v.* FROM vl_videos v JOIN vl_campaigns c
                  ON c.advertiser=v.advertiser AND c.campaign=v.campaign
                  WHERE v.is_pr AND v.caption IS NOT NULL AND v.caption<>''
                  ORDER BY v.platform, v.video_id"""
                ).fetchall()
            )
        sample = stratified_sample(rows, args.sample_size, args.seed)
        if len(sample) < args.sample_size:
            raise ValueError("insufficient_unique_labelled_videos")
        inference = Inference(
            BedrockClient.from_env(
                model_id_override=os.environ.get("VIDEO_LIBRARY_CASE_MODEL", DEFAULT_MODEL)
            ),
            max_cost_usd=float(os.environ.get("VIDEO_LIBRARY_CASE_MAX_COST_USD", "10")),
        )
        policy, report = evaluate(sample, cases, inference, seed=args.seed)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        # Atomic replacement; a failed run must not publish a half-written policy.
        temporary = args.policy_output.with_suffix(".tmp")
        temporary.write_text(policy.model_dump_json(indent=2) + "\n")
        temporary.replace(args.policy_output)
        return 0
    except Exception as exc:
        logger.error("case_evaluation_failed", request_id=request_id, error_code=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
