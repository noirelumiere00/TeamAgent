"""No AWS calls: strict validation, calibrated gates, costs, retries and masked evaluation."""

from __future__ import annotations

import json
from contextlib import nullcontext
from copy import deepcopy
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.ingest.video_library_case_evaluation import (
    Trial,
    calibrate,
    evaluate,
    metrics,
    stratified_sample,
)
from teamagent.ingest.video_library_case_inference import BudgetExceededError, Inference, run
from teamagent.ingest.video_library_case_models import (
    DEFAULT_MODEL,
    Case,
    Gate,
    Policy,
    Post,
    Prediction,
    catalog_hash,
    digest,
    input_hash,
    validate_prediction,
)

CASES = [Case(advertiser="Brand", campaign="Product"), Case(advertiser="Brand", campaign="Other")]
POST = Post(caption="Product #PR @brand", hashtags=["PR"], mentions=["@brand"])
RAW: dict[str, Any] = {
    "advertiser": "Brand",
    "campaign": "Product",
    "match_type": "existing",
    "confidence": 0.99,
    "evidence": [{"field": "caption", "quote": "Product"}],
}


def prediction(**changes: Any) -> Prediction:
    return Prediction.model_validate({**RAW, **changes})


def client(text: str | None = None, stop_reason: str = "end_turn") -> MagicMock:
    fake = MagicMock(model_id=DEFAULT_MODEL)
    fake.converse.return_value = ConverseResponse(
        text=text if text is not None else json.dumps(RAW),
        usage=TokenUsage(100, 50, 0, 0, 0.00035),
        model_id=DEFAULT_MODEL,
        latency_ms=1,
        stop_reason=stop_reason,
    )
    return fake


def policy(engine: Inference) -> Policy:
    return Policy(
        model=DEFAULT_MODEL,
        prompt_version="v1",
        prompt_hash=digest(engine.prompt),
        catalog_hash=catalog_hash(CASES),
        evaluation_hash="evaluated",
        existing=Gate(threshold=0.98, selected=100, correct=98),
        advertiser_only=Gate(threshold=0.99, selected=50, correct=50),
    )


def repo() -> MagicMock:
    fake = MagicMock()
    fake.run_lock.side_effect = lambda: nullcontext(True)
    fake.load.return_value = (
        CASES,
        [
            dict(
                platform="tiktok",
                video_id="1",
                is_pr=True,
                **POST.model_dump(exclude={"mentions", "posted_at"}),
            )
        ],
    )
    fake.save.return_value = True
    return fake


@pytest.mark.parametrize(
    "changes",
    [
        {"campaign": "Invented"},
        {"advertiser": "Imaginary"},
        {"confidence": "0.99"},
        {"confidence": True},
        {"confidence": 1.1},
        {"confidence": float("nan")},
        {"extra": "ignored?"},
        {"match_type": "approved"},
        {"advertiser": ""},
        {"evidence": [{"field": "caption", "quote": "not in original"}]},
        {"evidence": [{"field": "hashtags", "quote": "Product"}]},
        {"evidence": [{"field": "account_name", "quote": "Brand"}]},
        {"evidence": []},
        {"campaign": None},
        {"advertiser": None},
        {"match_type": "unknown"},
        {"match_type": "new"},
    ],
)
def test_json_rejects_invalid_or_ungrounded(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_prediction(json.dumps({**RAW, **changes}), POST, CASES)


@pytest.mark.parametrize("text", ["{}", "[]", "null", "not json", "```json\n{}\n```"])
def test_json_requires_exact_object(text: str) -> None:
    with pytest.raises(ValueError):
        validate_prediction(text, POST, CASES)


def test_known_advertiser_and_original_quotes() -> None:
    assert validate_prediction(json.dumps(RAW), POST, CASES) == prediction()
    with pytest.raises(ValueError, match="known_advertiser_conflict"):
        validate_prediction(json.dumps(RAW), POST.model_copy(update={"advertiser": "Other"}), CASES)
    raw = {**RAW, "evidence": [{"field": "mentions", "quote": "@brand"}]}
    assert validate_prediction(json.dumps(raw), POST, CASES).advertiser == "Brand"


def test_gates_partial_new_unknown_and_below_threshold() -> None:
    p = policy(Inference(client(), max_cost_usd=1))
    assert p.status(prediction()) == "applied"
    assert p.status(prediction(confidence=0.979)) == "unknown"
    assert p.status(prediction(campaign=None, match_type="unknown")) == "applied"
    assert p.status(prediction(campaign=None, match_type="unknown", confidence=0.98)) == "unknown"
    assert p.status(prediction(campaign="New", match_type="new", confidence=1.0)) == "candidate"
    assert p.status(prediction(advertiser=None, campaign=None, match_type="unknown")) == "unknown"
    p.existing = Gate(threshold=0.9, selected=100, correct=97)
    assert p.status(prediction()) == "unknown"


def test_inference_uses_cached_haiku_single_attempt_and_stops_before_cost_limit() -> None:
    fake = client()
    engine = Inference(fake, max_cost_usd=1)
    engine.predict(POST, CASES, request_id="request")
    reserved = engine.reserved_usd
    engine.max_cost_usd = reserved * 1.5
    with pytest.raises(BudgetExceededError):
        engine.predict(POST, CASES, request_id="request")
    assert fake.converse.call_count == 1
    assert engine.cost_usd == 0.00035
    args = fake.converse.call_args.kwargs
    assert args["cache_system"] and args["temperature"] == 0.1 and args["max_attempts"] == 1
    assert args["request_id"] == "request"
    assert "Product" in args["system"]


@pytest.mark.parametrize(
    "model,budget",
    [("unpriced-haiku", 1), ("sonnet", 1), (DEFAULT_MODEL, 0), (DEFAULT_MODEL, float("nan"))],
)
def test_unpriced_model_and_invalid_budget_rejected(model: str, budget: float) -> None:
    with pytest.raises(ValueError):
        Inference(MagicMock(model_id=model), max_cost_usd=budget)


@pytest.mark.parametrize(
    "failure", [TimeoutError("private customer"), ValueError("private customer")]
)
def test_failed_call_is_retryable_and_does_not_log_customer(
    failure: Exception, capsys: Any
) -> None:
    fake = client()
    fake.converse.side_effect = failure
    repository = repo()
    engine = Inference(fake, max_cost_usd=1)
    counts = run(repository, engine, policy=None, limit=1, dry_run=False, request_id="request")
    assert counts == {"attempted": 1, "failed": 1}
    repository.invalidate.assert_called_once()
    repository.save.assert_not_called()
    assert "private customer" not in capsys.readouterr().out
    assert engine.reserved_usd > 0


@pytest.mark.parametrize("text,stop", [("bad json", "end_turn"), (json.dumps(RAW), "max_tokens")])
def test_malformed_or_truncated_response_not_saved(text: str, stop: str) -> None:
    repository = repo()
    counts = run(
        repository,
        Inference(client(text, stop), max_cost_usd=1),
        policy=None,
        limit=1,
        dry_run=False,
        request_id="test",
    )
    assert counts["failed"] == 1
    repository.save.assert_not_called()


def test_run_application_idempotency_input_changes_limit_and_dry_run() -> None:
    repository = repo()
    engine = Inference(client(), max_cost_usd=1)
    p = policy(engine)
    counts = run(repository, engine, policy=p, limit=1, dry_run=False, request_id="test")
    assert counts == {"attempted": 1, "applied": 1}
    assert repository.save.call_args.args[2] == "applied"
    row = repository.load.return_value[1][0]
    row["input_hash"] = repository.save.call_args.args[3]
    repository.save.reset_mock()
    assert (
        run(repository, engine, policy=p, limit=1, dry_run=False, request_id="t")["unchanged"] == 1
    )
    repository.save.assert_not_called()
    row["caption"] += " changed"
    assert run(repository, engine, policy=p, limit=1, dry_run=True, request_id="t")["applied"] == 1
    repository.save.assert_not_called()
    repository.load.return_value[1].append({**row, "video_id": "2"})
    assert (
        run(repository, engine, policy=None, limit=1, dry_run=False, request_id="t")["attempted"]
        == 1
    )
    assert repository.save.call_args.args[2] == "unknown"
    assert input_hash(POST, CASES, DEFAULT_MODEL, engine.prompt, p) != input_hash(
        POST, CASES, DEFAULT_MODEL, engine.prompt + "v2", p
    )


def test_lock_stale_policy_and_budget_stop() -> None:
    repository = repo()
    engine = Inference(client(), max_cost_usd=0.000001)
    assert (
        run(repository, engine, policy=None, limit=1, dry_run=False, request_id="t")["budget_stop"]
        == 1
    )
    engine.client.converse.assert_not_called()
    repository.run_lock.side_effect = lambda: nullcontext(False)
    assert run(repository, engine, policy=None, limit=1, dry_run=False, request_id="t") == {
        "locked": 1
    }
    repository.run_lock.side_effect = lambda: nullcontext(True)
    p = policy(engine)
    p.catalog_hash = "stale"
    with pytest.raises(ValueError, match="stale_evaluation_policy"):
        run(repository, engine, policy=p, limit=1, dry_run=False, request_id="t")


def trial(**changes: Any) -> Trial:
    return Trial.model_validate(
        dict(
            key="test",
            truth=CASES[0],
            known_advertiser=True,
            withheld=False,
            prediction=prediction(),
            **changes,
        )
    )


def test_evaluation_metrics_and_thresholds() -> None:
    tuning = [trial() for _ in range(50)]
    final = [trial() for _ in range(100)]
    assert calibrate(tuning, final, "existing") == Gate(threshold=0.99, selected=100, correct=100)
    final[0].prediction = prediction(campaign="Other")
    final[1].prediction = prediction(campaign="Other")
    assert metrics(final)["precision"] == 0.98
    assert calibrate(tuning, final, "existing") is not None
    final[2].prediction = prediction(campaign="Other")
    assert calibrate(tuning, final, "existing") is None
    assert calibrate(tuning, final[:49], "existing") is None
    final = [trial() for _ in range(100)]
    final[0].withheld = True
    assert calibrate(tuning, final, "existing") is None
    final[0].withheld = False
    final[0].prediction = None
    final[0].error_code = "ValueError"
    assert calibrate(tuning, final, "existing") is None
    scores = metrics(final)
    assert scores["exact_accuracy"] == 0.99 and scores["unknown_rate"] == 0.01
    assert scores["advertiser_accuracy"] == 0.99 and scores["known_campaign_accuracy"] == 0.99
    assert metrics([])["precision"] == 0


def test_evaluation_masks_labels_withholds_cases_and_saves_only_passing_policy() -> None:
    rows = [
        dict(
            platform="tiktok",
            video_id=str(i),
            caption=f"Product {i}",
            advertiser="Brand",
            campaign="Product",
            account_handle=f"a{i % 3}",
        )
        for i in range(150)
    ]
    sampled = stratified_sample([*rows, deepcopy(rows[0])], 150, 42)
    assert len(sampled) == 150 and sampled == stratified_sample(rows, 150, 42)
    engine = Inference(client(), max_cost_usd=10)
    calls: list[tuple[Post, list[Case]]] = []

    def predict(post: Post, cases: list[Case], **kwargs: Any) -> Prediction:
        calls.append((post, cases))
        return (
            prediction() if CASES[0] in cases else prediction(campaign="Product", match_type="new")
        )

    engine.predict = predict  # type: ignore[method-assign]
    p, report = evaluate(sampled, CASES, engine, seed=42)
    assert p.existing and p.existing.passed()
    assert p.advertiser_only is None
    assert report["final"]["n"] == 100
    assert sum(post.advertiser is None for post, _ in calls) == 75
    assert sum(CASES[0] not in cases for _, cases in calls) == 30
    assert p.compatible(DEFAULT_MODEL, engine.prompt, CASES)
    assert "Brand" not in json.dumps(report["by_case"])


def test_dry_run_never_invalidates_or_saves_and_stale_write_not_counted_as_applied() -> None:
    repository = repo()
    engine = Inference(client(), max_cost_usd=1)
    run(repository, engine, policy=policy(engine), limit=1, dry_run=True, request_id="t")
    repository.invalidate.assert_not_called()
    repository.save.assert_not_called()
    repository.save.return_value = False
    assert run(
        repository, engine, policy=policy(engine), limit=1, dry_run=False, request_id="t"
    ) == {
        "attempted": 1,
        "stale": 1,
    }
    repository.save.side_effect = RuntimeError("DB unavailable")
    assert run(
        repository, engine, policy=policy(engine), limit=1, dry_run=False, request_id="t"
    ) == {
        "attempted": 1,
        "failed": 1,
    }


def test_stratification_covers_small_cases_before_large_case() -> None:
    rows = [
        dict(
            platform="tiktok",
            video_id=str(i),
            caption=f"unique{i}",
            advertiser="Brand",
            campaign="Product" if i < 100 else str(i),
        )
        for i in range(105)
    ]
    sample = stratified_sample(rows, 6, 42)
    assert len({r["campaign"] for r in sample}) == 6


def test_partial_evaluation_budget_does_not_produce_policy() -> None:
    engine = Inference(client(), max_cost_usd=0.000001)
    with pytest.raises(BudgetExceededError):
        evaluate(
            [dict(platform="tiktok", video_id="1", advertiser="Brand", campaign="Product")],
            CASES,
            engine,
            seed=42,
        )


def test_entry_point_without_policy_is_safe_and_errors_do_not_log_inputs(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from teamagent.ingest import video_library_case_inference as module

    monkeypatch.delenv("VIDEO_LIBRARY_CASE_POLICY", raising=False)
    monkeypatch.setenv("VIDEO_LIBRARY_CASE_LIMIT", "2")
    monkeypatch.setenv("VIDEO_LIBRARY_CASE_MAX_COST_USD", "1")
    monkeypatch.setattr(module, "configure_logging", lambda: False)
    monkeypatch.setattr(module.BedrockClient, "from_env", lambda **_: client())
    monkeypatch.setattr(module.PgVectorClient, "from_env", lambda: MagicMock())
    repository = repo()
    monkeypatch.setattr(module, "CaseRepository", lambda _: repository)
    assert module.main([]) == 0
    assert repository.save.call_args.args[2] == "unknown"
    repository.save.reset_mock()
    assert module.main(["--dry-run"]) == 0
    repository.save.assert_not_called()
    monkeypatch.setattr(
        module.PgVectorClient, "from_env", MagicMock(side_effect=ValueError("private name"))
    )
    assert module.main([]) == 1
    assert "private name" not in capsys.readouterr().out
