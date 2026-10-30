"""Model selection and real Bedrock adapter failures without provider calls."""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from structlog.testing import capture_logs

from scripts.compare_media_case_models import compare
from scripts.ingest_media_cases import main as ingest_main
from teamagent.adapters.apify_media_cases import PostAssets
from teamagent.adapters.bedrock_client import BedrockClient, _estimate_cost
from teamagent.adapters.cost_guard import CostGuard, CostLimitExceededError
from teamagent.adapters.gemini_client import GeminiClient
from teamagent.adapters.retry import RetryPolicy, call_with_retry
from teamagent.skills.media_case_search import distill
from teamagent.skills.media_case_search.distill import HAIKU_MODEL_ID, MODELS, CaseDistiller
from tests.adapters.test_apify_media_cases import png
from tests.adapters.test_apify_tiktok_video import _FakeTikTokApify
from tests.skills.media_case_search.test_media_cases import (
    HANDLE,
)
from tests.skills.media_case_search.test_media_cases import (
    extraction as _extraction,
)
from tests.skills.media_case_search.test_media_cases import (
    make_skill as _make_skill,
)
from tests.skills.media_case_search.test_media_cases import (
    raw_post as _raw_post,
)

extraction: Callable[..., dict[str, Any]] = _extraction
make_skill: Callable[..., Any] = _make_skill
raw_post: Callable[..., dict[str, Any]] = _raw_post


class FakeBedrock:
    def __init__(self, replies: list[str | Exception]) -> None:
        self.replies = replies.copy()
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {
            "output": {"message": {"content": [{"text": reply}]}},
            "usage": {"inputTokens": 1700, "outputTokens": 360},
            "stopReason": "end_turn",
        }


def bedrock(fake: FakeBedrock) -> BedrockClient:
    return BedrockClient(
        "ap-northeast-1",
        HAIKU_MODEL_ID,
        client=fake,
        rerank_client=object(),
        retry_policy=RetryPolicy(max_attempts=3, base_delay_s=0),
    )


@pytest.mark.parametrize("selected", [None, *MODELS])
def test_environment_selection(
    ledger: CostGuard, monkeypatch: pytest.MonkeyPatch, selected: str | None
) -> None:
    monkeypatch.setenv("GEMINI_MODEL_ID", "unrelated-model")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "unrelated-model")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-for-test")
    monkeypatch.delenv("GEMINI_USE_VERTEX", raising=False)
    if selected is None:
        monkeypatch.delenv("MEDIA_CASE_MODEL", raising=False)
    else:
        monkeypatch.setenv("MEDIA_CASE_MODEL", selected)
    factory = MagicMock(return_value=bedrock(FakeBedrock([])))
    monkeypatch.setattr(BedrockClient, "from_env", factory)
    result = CaseDistiller.from_env(ledger)
    if selected == "bedrock-haiku":
        factory.assert_called_once_with(model_id_override=HAIKU_MODEL_ID)
        assert result.gemini.model_id == HAIKU_MODEL_ID
        assert result.thinking == "disabled"
    else:
        factory.assert_not_called()
        assert result.gemini.model_id == (selected or "gemini-3.5-flash")
        assert result.thinking == "minimal"


def test_invalid_model_rejected_before_client_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIA_CASE_MODEL", "typo")
    factory = MagicMock()
    monkeypatch.setattr(GeminiClient, "from_env", factory)
    with pytest.raises(ValueError, match="MEDIA_CASE_MODEL"):
        CaseDistiller.from_env(None)
    factory.assert_not_called()


def no_wait_retry(fn: Callable[[], Any], **kwargs: Any) -> Any:
    return call_with_retry(fn, sleep=lambda _: None, **kwargs)


@pytest.mark.parametrize("error_code", ["ThrottlingException", "ServiceUnavailable"])
@pytest.mark.parametrize("exhausted", [False, True])
def test_bedrock_retry_settles_every_attempt_and_records_failure(
    ledger: CostGuard, monkeypatch: pytest.MonkeyPatch, error_code: str, exhausted: bool
) -> None:
    monkeypatch.setenv("BEDROCK_MAX_ATTEMPTS", "3")
    monkeypatch.setattr(distill, "call_with_retry", no_wait_retry)
    error = ClientError({"Error": {"Code": error_code, "Message": "fake"}}, "Converse")
    fake = FakeBedrock([error] * 3 if exhausted else [error, json.dumps(extraction())])
    raw = raw_post()
    skill = make_skill(ledger, _FakeTikTokApify({f"cover-{raw['id']}.png": png()}, [raw]))
    skill._distiller = CaseDistiller(bedrock(fake), ledger)
    with capture_logs() as logs:
        result = skill.ingest_initial([HANDLE])
    assert result["failed"] == int(exhausted)
    assert skill.store.state(HANDLE)["failed_ids"] == ([raw["id"]] if exhausted else [])
    attempts = 3 if exhausted else 2
    assert len(fake.calls) == attempts
    assert fake.calls[0]["messages"][0]["content"][0]["image"] == {
        "format": "png",
        "source": {"bytes": png()},
    }
    assert ledger._ddb is not None
    row = next(v for k, v in ledger._ddb.rows.items() if k.startswith("media_case_gemini#"))
    expected = 0.09 if exhausted else 0.03 + _estimate_cost(HAIKU_MODEL_ID, 1700, 360)
    assert row["units"] == attempts and row["cost_micro"] == round(expected * 1_000_000)
    assert skill._distiller.settled_cost_usd == 0  # ingestion runs in a separate thread
    assert any(log["event"] == "media_case_bedrock_retry" for log in logs)
    if exhausted:
        assert any(log.get("error") == "ClientError" for log in logs)
    else:
        assert skill.store.post(raw["id"])["cost_usd"] == pytest.approx(expected)


@pytest.mark.parametrize("exhausted", [False, True])
def test_bedrock_non_json_repair_and_failure_record(ledger: CostGuard, exhausted: bool) -> None:
    fake = FakeBedrock(["not JSON", "still not JSON" if exhausted else json.dumps(extraction())])
    raw = raw_post()
    skill = make_skill(ledger, _FakeTikTokApify({}, [raw]))
    skill._distiller = CaseDistiller(bedrock(fake), ledger)
    with capture_logs() as logs:
        result = skill.ingest_initial([HANDLE])
    assert result["failed"] == int(exhausted) and len(fake.calls) == 2
    assert skill.store.state(HANDLE)["failed_ids"] == ([raw["id"]] if exhausted else [])
    assert ledger._ddb is not None
    row = next(v for k, v in ledger._ddb.rows.items() if k.startswith("media_case_gemini#"))
    assert row["units"] == 2
    assert row["cost_micro"] == 2 * round(_estimate_cost(HAIKU_MODEL_ID, 1700, 360) * 1_000_000)
    assert fake.calls[1]["messages"][0]["content"][0]["text"].startswith("前回答")
    if exhausted:
        assert any(log.get("error") == "ValidationError" for log in logs)


def test_bedrock_uses_same_evidence_validation(ledger: CostGuard) -> None:
    fake = FakeBedrock(
        [json.dumps(extraction(evidence=[{"source": "caption", "excerpt": "捏造"}]))]
    )
    raw = raw_post()
    skill = make_skill(ledger, _FakeTikTokApify({}, [raw]))
    skill._distiller = CaseDistiller(bedrock(fake), ledger)
    assert skill.ingest_initial([HANDLE])["failed"] == 0
    metadata = skill.store.post(raw["id"])
    assert metadata["items"] == []
    assert metadata["coverage"]["dropped_evidence"] == 1


def test_bedrock_guard_and_output_budget(
    ledger: CostGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBedrock(["{}"])
    model = bedrock(fake)
    with pytest.raises(CostLimitExceededError):
        CaseDistiller(model, None)._call("data", [], "test")
    assert not fake.calls
    CaseDistiller(model, ledger)._call("data", [], "test")
    tokens = fake.calls[0]["inferenceConfig"]["maxTokens"]
    assert _estimate_cost(HAIKU_MODEL_ID, len(b"data") + 1024, tokens) <= 0.03
    monkeypatch.setenv("COST_MEDIA_CASE_GEMINI_MONTHLY_USD", "0.01")
    with pytest.raises(CostLimitExceededError):
        CaseDistiller(model, ledger)._call("data", [], "test")
    assert len(fake.calls) == 1


@pytest.mark.parametrize("code", ["ThrottlingException", "ServiceUnavailable", "non-json"])
def test_comparison_records_failed_bedrock_costs_and_error_codes(
    ledger: CostGuard, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    monkeypatch.setenv("BEDROCK_MAX_ATTEMPTS", "3")
    monkeypatch.setattr(distill, "call_with_retry", no_wait_retry)
    replies: list[str | Exception] = (
        ["not JSON"] * 2
        if code == "non-json"
        else [ClientError({"Error": {"Code": code}}, "Converse")] * 3
    )
    model = CaseDistiller(bedrock(FakeBedrock(replies)), ledger)
    assets = MagicMock()
    assets.for_post.return_value = PostAssets(None, (), "")
    report = compare([raw_post()], assets, {"bedrock-haiku": model})
    result = report["results"]["bedrock-haiku"][0]
    assert result["status"] == "failed"
    expected = 2 * _estimate_cost(HAIKU_MODEL_ID, 1700, 360) if code == "non-json" else 0.09
    assert result["cost_usd"] == pytest.approx(expected)
    if code == "non-json":
        assert result["error"] == "ValidationError"
    else:
        assert result["error_code"] == code


@pytest.mark.parametrize("model_id", MODELS[:2])
def test_gemini_bounded_image_cost_and_minimal_thinking(ledger: CostGuard, model_id: str) -> None:
    fake = MagicMock()
    fake.models.count_tokens.return_value = SimpleNamespace(total_tokens=1700)
    fake.models.generate_content.return_value = SimpleNamespace(
        text="{}",
        usage_metadata=SimpleNamespace(
            prompt_token_count=1700, candidates_token_count=360, thoughts_token_count=0
        ),
    )
    model = CaseDistiller(GeminiClient(model_id=model_id, client=fake), ledger)
    _, cost = model._call("prompt", [(png(), "image/png")], "test")
    assert cost == pytest.approx(0.00579 if model_id == MODELS[0] else 0.002625)
    config = fake.models.generate_content.call_args.kwargs["config"]
    assert config.thinking_config.thinking_level == "MINIMAL"
    assert config.http_options.retry_options.attempts == 1


def test_ingest_preview_model_is_selected_without_clients(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MEDIA_CASE_MODEL", "bedrock-haiku")
    factory = MagicMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(CaseDistiller, "from_env", factory)
    assert ingest_main(["--dataset", "saved"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["model"] == "bedrock-haiku" and result["gemini_thinking"] == "disabled"
    factory.assert_not_called()
