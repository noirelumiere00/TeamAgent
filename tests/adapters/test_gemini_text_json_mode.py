"""generate_text の JSON モードと従来の既定挙動をフェイクで確かめる。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gemini_client import GeminiClient


@pytest.mark.parametrize("json_mode", [False, True])
def test_generate_text_passes_json_mode_to_shared_generation(json_mode: bool) -> None:
    pytest.importorskip("google.genai")
    fake = MagicMock()
    fake.models.generate_content.return_value = SimpleNamespace(
        text='{"value": 1}',
        usage_metadata=SimpleNamespace(prompt_token_count=20, candidates_token_count=5),
    )
    client = GeminiClient(api_key="fake-key", client=fake)
    result = client.generate_text("構造化してください", "req-json", json_mode=json_mode)
    assert result.text == '{"value": 1}'
    config = fake.models.generate_content.call_args.kwargs["config"]
    if json_mode:
        assert config.response_mime_type == "application/json"
    else:
        assert config is None


def test_generate_text_without_json_argument_keeps_default_config() -> None:
    pytest.importorskip("google.genai")
    fake = MagicMock()
    fake.models.generate_content.return_value = SimpleNamespace(
        text="従来の自由文", usage_metadata=None
    )
    client = GeminiClient(api_key="fake-key", client=fake)
    assert client.generate_text("まとめてください", "req-default").text == "従来の自由文"
    assert fake.models.generate_content.call_args.kwargs["config"] is None


@pytest.mark.parametrize("use_vertex", [False, True])
def test_bounded_multimodal_counts_images_and_disables_paid_retries(use_vertex: bool) -> None:
    fake = MagicMock()
    fake.models.count_tokens.return_value = SimpleNamespace(total_tokens=1000)
    fake.models.generate_content.return_value = SimpleNamespace(
        text='{"items": []}',
        usage_metadata=SimpleNamespace(prompt_token_count=1000, candidates_token_count=100),
    )
    client = GeminiClient(api_key="fake-key", client=fake, use_vertex=use_vertex)
    result = client.analyze_images_bytes(
        [(b"png", "image/png")], "extract", "req", max_cost_usd=0.03
    )
    assert result.text == '{"items": []}'
    count_args = fake.models.count_tokens.call_args.kwargs
    assert any(
        p.inline_data and p.inline_data.data == b"png" for p in count_args["contents"][0].parts
    )
    config = fake.models.generate_content.call_args.kwargs["config"]
    assert (count_args["config"].generation_config is not None) is use_vertex
    if use_vertex:
        assert count_args["config"].generation_config.media_resolution == config.media_resolution
    else:
        assert config.media_resolution is None
    assert config.response_mime_type == "application/json"
    assert 256 <= config.max_output_tokens <= 8192
    assert config.http_options.retry_options.attempts == 1
    assert config.http_options.timeout == 90_000
    fake.models.generate_content.reset_mock()
    fake.models.generate_content.side_effect = RuntimeError("503 unavailable")
    with pytest.raises(RuntimeError):
        client.analyze_images_bytes([(b"png", "image/png")], "extract", "req", max_cost_usd=0.03)
    assert fake.models.generate_content.call_count == 1
