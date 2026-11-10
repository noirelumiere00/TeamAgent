"""送信される model ID を検査する（AWS はすべてフェイク）。"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from structlog.testing import capture_logs

from teamagent.adapters.bedrock_client import DEFAULT_MODEL_ID, BedrockClient
from teamagent.adapters.retry import RetryPolicy
from tests.adapters.test_bedrock_client import _fake_bedrock_response

SONNET_MODELS = [
    f"{prefix}.anthropic.claude-{generation}"
    for prefix in ("jp", "us", "global")
    for generation in ("sonnet-4-6", "sonnet-5", "SoNnEt-5-5")
]


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    client.converse.return_value = _fake_bedrock_response()
    monkeypatch.setattr("teamagent.adapters.bedrock_client.boto3.client", lambda *a, **kw: client)
    monkeypatch.setenv("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
    return client


@pytest.mark.parametrize("model_id", SONNET_MODELS)
@pytest.mark.parametrize("route", ["env", "override", "direct", "reassigned"])
def test_bedrock_env_sonnet_never_reaches_converse(
    monkeypatch: pytest.MonkeyPatch, runtime: MagicMock, model_id: str, route: str
) -> None:
    if route == "env":
        monkeypatch.setenv("BEDROCK_MODEL_ID", model_id)
        client = BedrockClient.from_env()
    elif route == "override":
        client = BedrockClient.from_env(model_id_override=model_id)
    elif route == "direct":
        client = BedrockClient(region="ap-northeast-1", model_id=model_id)
    else:
        client = BedrockClient.from_env()
        client.model_id = model_id
    response = client.converse(messages=[], request_id="model-ban")
    assert runtime.converse.call_args.kwargs["modelId"] == DEFAULT_MODEL_ID
    assert response.model_id == client.model_id == DEFAULT_MODEL_ID
    assert response.usage.cost_usd == pytest.approx(0.000341)


@pytest.mark.parametrize("fallback", [None, "", "global.anthropic.SONNET-5-5", "claude-opus-4"])
def test_bedrock_invalid_fallback_uses_default_haiku(
    monkeypatch: pytest.MonkeyPatch, runtime: MagicMock, fallback: str | None
) -> None:
    if fallback is None:
        monkeypatch.delenv("BEDROCK_MODEL_ID")
    else:
        monkeypatch.setenv("BEDROCK_MODEL_ID", fallback)
    client = BedrockClient.from_env(model_id_override=SONNET_MODELS[0])
    client.converse(messages=[], request_id="fallback")
    assert runtime.converse.call_args.kwargs["modelId"] == DEFAULT_MODEL_ID


def test_bedrock_haiku_passes_through_and_is_used_as_replacement(
    monkeypatch: pytest.MonkeyPatch, runtime: MagicMock
) -> None:
    haiku = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    monkeypatch.setenv("BEDROCK_MODEL_ID", haiku)
    client = BedrockClient.from_env()
    with capture_logs() as logs:
        client.converse(messages=[], request_id="haiku")
    assert runtime.converse.call_args.kwargs["modelId"] == haiku
    assert not any(event["event"] == "bedrock_model_banned" for event in logs)
    client = BedrockClient.from_env(model_id_override=SONNET_MODELS[0])
    client.converse(messages=[], request_id="replacement")
    assert runtime.converse.call_args.kwargs["modelId"] == haiku


def test_bedrock_ban_logs_once_without_body_even_with_retry(runtime: MagicMock) -> None:
    client = BedrockClient.from_env(model_id_override=SONNET_MODELS[0])
    client._retry_policy = RetryPolicy(max_attempts=2, base_delay_s=0, max_delay_s=0)
    runtime.converse.side_effect = [
        ClientError({"Error": {"Code": "ThrottlingException"}}, "Converse"),
        _fake_bedrock_response(),
        _fake_bedrock_response(),
    ]
    with capture_logs() as logs:
        for _ in range(2):
            client.converse(
                messages=[{"role": "user", "content": [{"text": "private body"}]}],
                system="private system",
                request_id="log",
            )
    banned = [event for event in logs if event["event"] == "bedrock_model_banned"]
    assert banned == [
        {
            "event": "bedrock_model_banned",
            "log_level": "warning",
            "original_model_id": SONNET_MODELS[0],
            "replacement_model_id": DEFAULT_MODEL_ID,
            "caller": "BedrockClient.converse",
        }
    ]
    assert all(
        call.kwargs["modelId"] == DEFAULT_MODEL_ID for call in runtime.converse.call_args_list
    )


@pytest.mark.parametrize("operation", ["embed", "rerank"])
def test_bedrock_sonnet_in_non_chat_config_fails_before_network(
    runtime: MagicMock, operation: str
) -> None:
    client = BedrockClient.from_env()
    with pytest.raises(ValueError, match="Sonnet is banned"):
        if operation == "embed":
            client.embed_model_id = SONNET_MODELS[0]
            client.embed_texts(
                ["private body"], request_id="embed-ban", input_type="search_document"
            )
        else:
            client.rerank_model_arn = (
                f"arn:aws:bedrock:us-east-1::foundation-model/{SONNET_MODELS[0]}"
            )
            client.rerank(
                query="private query", documents=["private body"], request_id="rerank-ban"
            )
    runtime.invoke_model.assert_not_called()
    runtime.rerank.assert_not_called()
