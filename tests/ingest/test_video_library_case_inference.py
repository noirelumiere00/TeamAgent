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
    application_prediction,
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
        application_version="v3",
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
        {"evidence": [{"field": "hashtags", "quote": "Gadget"}]},
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
    p.existing = Gate(threshold=0.9, selected=100, correct=96)
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
    for t in final[:3]:
        t.prediction = prediction(campaign="Other")
    assert metrics(final)["precision"] == 0.97
    assert calibrate(tuning, final, "existing") == Gate(threshold=0.99, selected=100, correct=97)
    final[3].prediction = prediction(campaign="Other")
    assert calibrate(tuning, final, "existing") is None
    assert calibrate(tuning, final[4:33], "existing") is None
    assert calibrate(tuning, final[4:34], "existing") is not None
    final = [trial() for _ in range(100)]
    final[0].withheld = True
    assert calibrate(tuning, final, "existing") is None
    final[0].withheld = False
    final[0].prediction = None
    final[0].error_code = "ValueError"
    assert calibrate(tuning, final, "existing") is not None
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


def test_fenced_json_answer_is_accepted_but_other_wrapping_is_not() -> None:
    from teamagent.ingest.video_library_case_models import Case, Post, validate_prediction

    post = Post(
        caption="サクセスで白髪ケア #PR",
        hashtags=["PR"],
        mentions=[],
        account_name="a",
        account_handle="a",
        posted_at="",
        advertiser=None,
    )
    cases = [Case(advertiser="花王", campaign="サクセス")]
    body = (
        '{"advertiser": "花王", "campaign": "サクセス", "match_type": "existing", "confidence": 0.95,'
        ' "evidence": [{"field": "caption", "quote": "白髪ケア"}]}'
    )
    assert validate_prediction("```json\n" + body + "\n```", post, cases).campaign == "サクセス"
    assert validate_prediction(body, post, cases).advertiser == "花王"
    import pytest

    with pytest.raises(ValueError):
        validate_prediction("答えは " + body, post, cases)


def test_quotes_ground_across_hash_spacing_width_but_not_invented_text() -> None:
    import pytest

    from teamagent.ingest.video_library_case_models import Case, Post, validate_prediction

    post = Post(
        caption="時短したくて入れた大阪ガスの乾太くん\n正直いちばん感動 #PR",
        hashtags=["乾太くん", "大阪ガス", "PR"],
        mentions=[],
        account_name="a",
        account_handle="a",
        posted_at="",
        advertiser=None,
    )
    cases = [Case(advertiser="大阪ガス", campaign="乾太くん")]

    def answer(field: str, quote: str) -> str:
        return (
            '{"advertiser": "大阪ガス", "campaign": "乾太くん", "match_type": "existing", "confidence": 0.9,'
            ' "evidence": [{"field": "' + field + '", "quote": "' + quote + '"}]}'
        )

    assert validate_prediction(answer("hashtags", "#乾太くん #大阪ガス"), post, cases)
    assert validate_prediction(
        answer("caption", "大阪ガスの乾太くん 正直いちばん感動"), post, cases
    )
    assert validate_prediction(answer("caption", "ＰＲ"), post, cases)
    with pytest.raises(ValueError, match="ungrounded_quote"):
        validate_prediction(answer("caption", "乾燥機の最高峰"), post, cases)
    with pytest.raises(ValueError, match="ungrounded_quote"):
        validate_prediction(answer("hashtags", "#乾太くん #衣類乾燥機"), post, cases)


@pytest.mark.parametrize(
    "quotes,keep_campaign",
    [
        (["Product Other"], False),
        (["Product", "Other"], False),
        (["Other"], False),
        (["Brand"], False),
        (["Product"], True),
        (["#Ｐｒｏｄｕｃｔで使える"], True),
        (["P r o d u c t"], True),
    ],
)
def test_multi_campaign_requires_unique_source_name(quotes: list[str], keep_campaign: bool) -> None:
    raw = prediction(evidence=[dict(field="caption", quote=q) for q in quotes])
    reflected = application_prediction(raw, CASES, Post(caption=" ".join(quotes)))
    assert reflected.advertiser == "Brand"
    assert reflected.campaign == ("Product" if keep_campaign else None)
    assert reflected.match_type == ("existing" if keep_campaign else "unknown")
    assert raw.campaign == "Product"  # Keep the raw answer for diagnosis.


def test_single_campaign_and_new_candidate_keep_current_behavior() -> None:
    raw = prediction(evidence=[dict(field="caption", quote="Brand")])
    assert application_prediction(raw, CASES[:1], POST) == raw
    # An unrelated advertiser's campaign must not make this advertiser ambiguous.
    assert (
        application_prediction(raw, [CASES[0], Case(advertiser="Another", campaign="Brand")], POST)
        == raw
    )
    candidate = prediction(campaign="New", match_type="new")
    assert application_prediction(candidate, CASES, POST) == candidate
    assert (
        application_prediction(
            prediction(campaign=None, match_type="unknown"), CASES, POST
        ).campaign
        is None
    )


def test_ambiguous_campaign_is_saved_as_advertiser_only_with_existing_gate() -> None:
    repository = repo()
    repository.load.return_value[1][0]["caption"] = "Product Other #PR"
    engine = Inference(
        client(json.dumps({**RAW, "evidence": [dict(field="caption", quote="Product Other")]})),
        max_cost_usd=1,
    )
    p = policy(engine)
    p.advertiser_only = None
    assert run(repository, engine, policy=p, limit=1, dry_run=False, request_id="test") == {
        "attempted": 1,
        "applied": 1,
    }
    saved = repository.save.call_args.args[1]
    assert saved.advertiser == "Brand" and saved.campaign is None and saved.match_type == "unknown"
    assert repository.save.call_args.args[2] == "applied"


def test_gate_scores_projected_advertisers_and_ignores_coverage_and_invalid_outputs() -> None:
    tuning = [trial() for _ in range(50)]
    final = [trial() for _ in range(100)]
    for t in final:
        # The raw campaign is wrong, but only the correct advertiser will be applied.
        t.prediction = prediction(campaign="Other")
        t.application = prediction(campaign=None, match_type="unknown")
    for t in final[30:]:
        t.prediction = None
        t.application = None
        t.error_code = "ValidationError"
    score = metrics(final)
    assert score["selected"] == 30 and score["precision"] == 1.0
    assert score["exact_accuracy"] == 0.0 and score["invalid_outputs"] == 70
    assert calibrate(tuning, final, "existing") == Gate(threshold=0.99, selected=30, correct=30)
    final[0].application = prediction(advertiser="Wrong", campaign=None, match_type="unknown")
    assert calibrate(tuning, final, "existing") is None


def test_gate_tuning_precision_and_withheld_errors_are_still_required() -> None:
    tuning = [trial() for _ in range(100)]
    final = [trial() for _ in range(100)]
    for t in tuning[:3]:
        t.prediction = prediction(campaign="Other")
    assert calibrate(tuning, final, "existing") is not None
    tuning[3].prediction = prediction(campaign="Other")
    assert calibrate(tuning, final, "existing") is None
    assert calibrate([], final, "existing") is None
    final[0].withheld = True
    final[0].prediction = prediction(confidence=0.1)
    final[0].application = prediction(campaign=None, match_type="unknown", confidence=0.1)
    assert calibrate([trial()], final, "existing") is None


@pytest.mark.parametrize(
    "selected,correct,passed",
    [(30, 30, True), (100, 97, True), (100, 96, False), (30, 29, False), (30, 31, False)],
)
def test_persisted_gate_uses_same_count_and_precision(
    selected: int, correct: int, passed: bool
) -> None:
    assert Gate(threshold=0.95, selected=selected, correct=correct).passed() is passed
    with pytest.raises(ValueError):
        Gate(threshold=0.95, selected=29, correct=29)


def test_old_policy_requires_reevaluation() -> None:
    old = policy(Inference(client(), max_cost_usd=1)).model_dump()
    old.pop("application_version")
    with pytest.raises(ValueError):
        Policy.model_validate(old)


def test_evaluation_report_keeps_wrong_applications_private_and_caption_short(capsys: Any) -> None:
    rows = [
        dict(
            platform="tiktok",
            video_id=str(i),
            caption="Other private-caption-" + "字" * 100,
            advertiser="Brand",
            campaign="Product",
        )
        for i in range(150)
    ]
    engine = Inference(client(), max_cost_usd=10)

    def predict(post: Post, cases: list[Case], **kwargs: Any) -> Prediction:
        if CASES[0] not in cases:
            return prediction(campaign="Product", match_type="new")
        return prediction(
            campaign="Other", confidence=0.95, evidence=[dict(field="caption", quote="Other")]
        )

    engine.predict = predict  # type: ignore[method-assign]
    _, report = evaluate(rows, CASES, engine, seed=42)
    errors = report["incorrect_applications"]
    assert len(errors) == 120
    assert {e["split"] for e in errors} == {"tuning", "final"}
    for error in errors:
        assert error["truth"] == CASES[0].model_dump()
        assert error["prediction"]["campaign"] == error["application"]["campaign"] == "Other"
        assert error["prediction"]["confidence"] == 0.95
        assert error["prediction"]["evidence"] == [dict(field="caption", quote="Other")]
        assert error["caption"] == rows[0]["caption"][:80]
        assert error["status"] == "unknown"  # Diagnostic proposals even when the gate fails.
    logs = capsys.readouterr()
    assert "private-caption" not in logs.out + logs.err
    assert "Brand" not in logs.out + logs.err


def test_normalized_campaign_names_and_overlapping_names_are_conservative() -> None:
    name = "Ｐｒｏｄｕｃｔ　２０２６"
    cases = [Case(advertiser="Brand", campaign=name), CASES[1]]
    assert (
        application_prediction(
            prediction(campaign=name, evidence=[dict(field="caption", quote="#Product2026の紹介")]),
            cases,
            Post(caption="#Product2026の紹介"),
        ).campaign
        == name
    )
    assert (
        application_prediction(
            prediction(
                campaign="ProductPlus", evidence=[dict(field="caption", quote="ProductPlus")]
            ),
            [CASES[0], Case(advertiser="Brand", campaign="ProductPlus")],
            Post(caption="ProductPlus"),
        ).campaign
        is None
    )


def test_report_drops_wrong_campaign_when_competitor_is_elsewhere_in_caption() -> None:
    rows = [
        dict(
            platform="tiktok",
            video_id=str(i),
            caption=f"{i}:Product Other",
            advertiser="Brand",
            campaign="Product",
        )
        for i in range(150)
    ]
    engine = Inference(client(), max_cost_usd=10)

    def predict(post: Post, cases: list[Case], **kwargs: Any) -> Prediction:
        if CASES[0] not in cases:
            return prediction(campaign="Product", match_type="new", confidence=0.95)
        # Most wrong raw campaigns are harmless after dropping the ambiguous campaign.
        return prediction(
            campaign="Other",
            confidence=0.95,
            evidence=[
                dict(
                    field="caption",
                    quote="Other" if post.caption.startswith("0:") else "Product Other",
                )
            ],
        )

    engine.predict = predict  # type: ignore[method-assign]
    p, report = evaluate(rows, CASES, engine, seed=42)
    assert p.existing and p.existing.passed()
    assert report["final"]["exact_accuracy"] == 0.0
    assert report["incorrect_applications"] == []


def test_report_precision_and_errors_use_the_calibrated_threshold() -> None:
    rows = [
        dict(
            platform="tiktok",
            video_id=str(i),
            caption=f"{i}:" + ("Product" if i % 2 == 0 else "Other"),
            advertiser="Brand",
            campaign="Product",
        )
        for i in range(150)
    ]
    engine = Inference(client(), max_cost_usd=10)

    def predict(post: Post, cases: list[Case], **kwargs: Any) -> Prediction:
        if CASES[0] not in cases:
            return prediction(campaign="Product", match_type="new")
        high = int(post.caption.split(":")[0]) % 2 == 0
        name = "Product" if high else "Other"
        return prediction(
            campaign=name,
            confidence=0.99 if high else 0.5,
            evidence=[dict(field="caption", quote=name)],
        )

    engine.predict = predict  # type: ignore[method-assign]
    p, report = evaluate(rows, CASES, engine, seed=42)
    assert p.existing and p.existing.threshold == 0.99
    assert report["final"]["selected"] == p.existing.selected
    assert report["final"]["precision"] == 1.0
    assert report["incorrect_applications"] == []


@pytest.mark.parametrize(
    "post,keep",
    [
        (Post(caption="Product Other"), False),
        (Post(caption="Product", hashtags=["Other"]), False),
        (Post(caption="Other", hashtags=["Product"]), False),
        (Post(caption="unrelated", hashtags=["Other"]), False),
        (Post(caption="Productだけ"), True),
        (Post(caption="unrelated", hashtags=["Ｐｒｏｄｕｃｔ"]), True),
        (Post(caption="p r o d u c t", hashtags=["OTHER"]), False),
        (Post(caption="unrelated", account_name="Product"), False),
    ],
)
def test_other_campaign_anywhere_in_caption_or_hashtags_blocks_full_match(
    post: Post, keep: bool
) -> None:
    # Haiku quotes only its preferred campaign; the guard must inspect the unquoted source too.
    raw = prediction(evidence=[dict(field="caption", quote="Product")])
    applied = application_prediction(raw, CASES, post)
    assert applied.advertiser == "Brand"
    assert applied.campaign == ("Product" if keep else None)


def test_v2_policy_requires_reevaluation_for_whole_source_rule() -> None:
    old = policy(Inference(client(), max_cost_usd=1)).model_dump()
    old["application_version"] = "v2"
    with pytest.raises(ValueError):
        Policy.model_validate(old)
