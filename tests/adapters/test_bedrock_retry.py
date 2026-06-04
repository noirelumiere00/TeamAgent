"""BedrockClient のリトライ挙動テスト（P0②・課金0・実待ち0）。

ThrottlingException / 5xx / 接続断を「一過性」と分類して指数バックオフで自動リトライし、
ValidationException 等の恒久エラーは即座に上げることを、boto3 をモックして検証する。
バックオフ秒は 0 に設定して決定論かつ高速にする（time.sleep(0) は実質ノーウェイト）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from teamagent.adapters.bedrock_client import BedrockClient, _is_bedrock_retryable
from teamagent.runtime.retry import RetryPolicy

# テスト用: 待ち時間ゼロ・最大3試行（初回 + 2リトライ）
_NO_WAIT = RetryPolicy(max_attempts=3, base_delay_s=0.0, max_delay_s=0.0)


def _client_error(code: str, status: int, op: str = "Converse") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


def _ok_response() -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": "やあ"}]}},
        "stopReason": "end_turn",
        "usage": {
            "inputTokens": 10,
            "outputTokens": 5,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 0,
        },
    }


def _make_client(converse_side_effect: Any) -> tuple[BedrockClient, MagicMock]:
    mock_client = MagicMock()
    mock_client.converse.side_effect = converse_side_effect
    client = BedrockClient(
        region="ap-northeast-1",
        model_id="jp.anthropic.claude-sonnet-4-6",
        client=mock_client,
        rerank_client=MagicMock(),
        retry_policy=_NO_WAIT,
    )
    return client, mock_client


# -----------------------------------------------------------
# converse のリトライ
# -----------------------------------------------------------
def test_converse_retries_throttle_then_succeeds() -> None:
    """ThrottlingException が2回 → 3回目成功。テキスト/usage は正しくマップされる。"""
    throttle = _client_error("ThrottlingException", 429)
    client, mock = _make_client([throttle, throttle, _ok_response()])

    resp = client.converse(
        messages=[{"role": "user", "content": [{"text": "x"}]}],
        request_id="req-throttle",
    )
    assert resp.text == "やあ"
    assert resp.usage.input_tokens == 10
    assert mock.converse.call_count == 3  # 初回 + 2リトライ


def test_converse_retries_on_5xx_status_even_if_code_unknown() -> None:
    """error code が未知でも HTTP 503 なら一過性として1回リトライ後に成功。"""
    server_err = _client_error("SomethingTransient", 503)
    client, mock = _make_client([server_err, _ok_response()])

    resp = client.converse(
        messages=[{"role": "user", "content": [{"text": "x"}]}],
        request_id="req-503",
    )
    assert resp.text == "やあ"
    assert mock.converse.call_count == 2


def test_converse_exhausts_retries_and_raises() -> None:
    """毎回 ThrottlingException → max_attempts(3) 回呼んで最後の例外を送出。"""
    throttle = _client_error("ThrottlingException", 429)
    client, mock = _make_client(throttle)  # 単一例外＝毎回送出

    with pytest.raises(ClientError) as ei:
        client.converse(
            messages=[{"role": "user", "content": [{"text": "x"}]}],
            request_id="req-exhaust",
        )
    assert ei.value.response["Error"]["Code"] == "ThrottlingException"
    assert mock.converse.call_count == 3  # 無限リトライしない


def test_converse_does_not_retry_validation_error() -> None:
    """ValidationException（恒久エラー）はリトライせず即送出（1回だけ呼ぶ）。"""
    validation = _client_error("ValidationException", 400)
    client, mock = _make_client(validation)

    with pytest.raises(ClientError) as ei:
        client.converse(
            messages=[{"role": "user", "content": [{"text": "x"}]}],
            request_id="req-validation",
        )
    assert ei.value.response["Error"]["Code"] == "ValidationException"
    assert mock.converse.call_count == 1  # リトライしていない


def test_converse_does_not_retry_access_denied() -> None:
    """AccessDeniedException（恒久）もリトライしない。"""
    denied = _client_error("AccessDeniedException", 403)
    client, mock = _make_client(denied)

    with pytest.raises(ClientError):
        client.converse(
            messages=[{"role": "user", "content": [{"text": "x"}]}],
            request_id="req-denied",
        )
    assert mock.converse.call_count == 1


# -----------------------------------------------------------
# 回帰防止: botocore 内部リトライの無効化（CRITICAL 修正のガード）
# -----------------------------------------------------------
def test_botocore_internal_retries_disabled_total_attempts_one() -> None:
    """botocore 内部リトライが total_max_attempts=1（初回のみ）に解決されること。

    Config で ``max_attempts=1`` を使うと解決値は total_max_attempts=2（初回+1リトライ）になり、
    自前 call_with_retry と二重化して待ち時間が掛け算になる。これを防ぐため total_max_attempts=1 を
    使う。モッククライアントは Config を解釈しないので、ここは**実 boto3 クライアント**で検証する。
    tcp_keepalive / read_timeout も併せて固定されていることを確認（VPC/NAT 無言切断・長文生成対策）。
    """
    bc = BedrockClient(region="ap-northeast-1", model_id="jp.anthropic.claude-sonnet-4-6")
    for cli in (bc._client, bc._rerank_client):
        cfg = cli.meta.config
        assert cfg.retries.get("total_max_attempts") == 1, cfg.retries
        assert cfg.tcp_keepalive is True
        assert cfg.read_timeout == 120


# -----------------------------------------------------------
# rerank のリトライ（同じ機構が rerank にも掛かること）
# -----------------------------------------------------------
def test_rerank_retries_throttle_then_succeeds() -> None:
    """rerank も ThrottlingException を1回リトライして成功する。"""
    throttle = _client_error("ThrottlingException", 429, op="Rerank")
    mock_rerank = MagicMock()
    mock_rerank.rerank.side_effect = [
        throttle,
        {"results": [{"index": 0, "relevanceScore": 0.9}]},
    ]
    client = BedrockClient(
        region="ap-northeast-1",
        model_id="jp.anthropic.claude-sonnet-4-6",
        client=MagicMock(),
        rerank_client=mock_rerank,
        retry_policy=_NO_WAIT,
    )
    resp = client.rerank(query="q", documents=["d0"], request_id="req-rr")
    assert resp.results[0].index == 0
    assert mock_rerank.rerank.call_count == 2


# -----------------------------------------------------------
# 分類述語 _is_bedrock_retryable の単体検証
# -----------------------------------------------------------
@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        ("ThrottlingException", 429, True),
        ("TooManyRequestsException", 429, True),
        ("ServiceUnavailableException", 503, True),
        ("InternalServerException", 500, True),
        ("ModelTimeoutException", 408, True),  # code がリトライ対象なら status 不問
        ("SomethingNew", 502, True),  # code 未知でも 5xx/429 ならリトライ
        ("ValidationException", 400, False),
        ("AccessDeniedException", 403, False),
        ("ResourceNotFoundException", 404, False),
    ],
)
def test_is_bedrock_retryable_classification(code: str, status: int, expected: bool) -> None:
    assert _is_bedrock_retryable(_client_error(code, status)) is expected


def test_is_bedrock_retryable_botocore_transient() -> None:
    """BotoCoreError 配下（接続断・読取りタイムアウト等）は一過性＝リトライ可。"""
    assert _is_bedrock_retryable(BotoCoreError()) is True


def test_is_bedrock_retryable_unknown_exception_is_not_retried() -> None:
    """想定外の例外（バグ等）はリトライしない（嵐を起こさない・早く気づく）。"""
    assert _is_bedrock_retryable(ValueError("bug")) is False
