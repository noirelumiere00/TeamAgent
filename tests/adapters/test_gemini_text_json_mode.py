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
