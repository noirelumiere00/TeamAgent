"""BedrockCohereEmbedder / build_embedder_from_env / backend×column ペア整合のテスト。

boto3（bedrock-runtime）の invoke_model をモックして、input_type のマッピング
（embed=search_query / embed_passage=search_document）・1024 次元の返却・バッチ分割・
バックエンドと embedding 列のペア整合バリデーション（不一致で fail-loud）を検証する。
実 AWS には触れない（CI/オフライン）。
"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.adapters.embeddings_client import (
    ALLOWED_EMBEDDING_COLUMNS,
    BedrockCohereEmbedder,
    Embedder,
    LocalE5Embedder,
    build_embedder_from_env,
    resolve_embedding_column,
    validate_embedder_column_pair,
)


def _fake_invoke_response(embeddings: list[list[float]]) -> dict[str, Any]:
    """Bedrock InvokeModel の戻り値（body は read() できる stream 風オブジェクト）。"""
    body = json.dumps({"embeddings": embeddings}).encode("utf-8")
    return {"body": BytesIO(body)}


def _make_client(mock_runtime: MagicMock) -> BedrockClient:
    return BedrockClient(
        region="ap-northeast-1",
        model_id="jp.anthropic.claude-sonnet-4-6",
        client=mock_runtime,
        rerank_client=MagicMock(),
    )


# ── BedrockClient.embed_texts ──────────────────────────────────────────────


def test_embed_texts_maps_input_type_and_returns_vectors() -> None:
    mock_runtime = MagicMock()
    mock_runtime.invoke_model.return_value = _fake_invoke_response([[0.1] * 1024])
    client = _make_client(mock_runtime)

    resp = client.embed_texts(["こんにちは"], request_id="r1", input_type="search_query")

    assert len(resp.embeddings) == 1
    assert len(resp.embeddings[0]) == 1024
    call = mock_runtime.invoke_model.call_args.kwargs
    assert call["modelId"] == "cohere.embed-multilingual-v3"
    body = json.loads(call["body"])
    assert body["input_type"] == "search_query"
    assert body["texts"] == ["こんにちは"]
    assert body["truncate"] == "END"


def test_embed_texts_rejects_bad_input_type() -> None:
    client = _make_client(MagicMock())
    with pytest.raises(ValueError, match="input_type"):
        client.embed_texts(["x"], request_id="r", input_type="classification")


def test_embed_texts_rejects_empty() -> None:
    client = _make_client(MagicMock())
    with pytest.raises(ValueError, match="空"):
        client.embed_texts([], request_id="r", input_type="search_query")


def test_embed_texts_splits_batches_over_96() -> None:
    """97 件は 96 + 1 の 2 回に分割して invoke_model する。"""
    mock_runtime = MagicMock()
    # 1 回目 96 件、2 回目 1 件分のベクトルを返す。
    mock_runtime.invoke_model.side_effect = [
        _fake_invoke_response([[0.0] * 1024 for _ in range(96)]),
        _fake_invoke_response([[0.0] * 1024]),
    ]
    client = _make_client(mock_runtime)

    resp = client.embed_texts(
        [f"t{i}" for i in range(97)], request_id="r", input_type="search_document"
    )
    assert mock_runtime.invoke_model.call_count == 2
    assert len(resp.embeddings) == 97


# ── BedrockCohereEmbedder（Protocol 準拠・非対称 input_type） ────────────────


def test_cohere_embedder_query_uses_search_query() -> None:
    mock_runtime = MagicMock()
    mock_runtime.invoke_model.return_value = _fake_invoke_response([[0.2] * 1024])
    embedder = BedrockCohereEmbedder(bedrock=_make_client(mock_runtime))

    vec = embedder.embed("検索クエリ")

    assert len(vec) == 1024
    body = json.loads(mock_runtime.invoke_model.call_args.kwargs["body"])
    assert body["input_type"] == "search_query"


def test_cohere_embedder_passage_uses_search_document() -> None:
    mock_runtime = MagicMock()
    mock_runtime.invoke_model.return_value = _fake_invoke_response([[0.3] * 1024])
    embedder = BedrockCohereEmbedder(bedrock=_make_client(mock_runtime))

    embedder.embed_passage("取り込み資料の本文")

    body = json.loads(mock_runtime.invoke_model.call_args.kwargs["body"])
    assert body["input_type"] == "search_document"


def test_cohere_embedder_passage_batch_uses_search_document() -> None:
    mock_runtime = MagicMock()
    mock_runtime.invoke_model.return_value = _fake_invoke_response([[0.1] * 1024, [0.2] * 1024])
    embedder = BedrockCohereEmbedder(bedrock=_make_client(mock_runtime))

    out = embedder.embed_passage_batch(["a", "b"])

    assert len(out) == 2
    body = json.loads(mock_runtime.invoke_model.call_args.kwargs["body"])
    assert body["input_type"] == "search_document"


def test_cohere_embedder_passage_batch_empty_no_call() -> None:
    mock_runtime = MagicMock()
    embedder = BedrockCohereEmbedder(bedrock=_make_client(mock_runtime))
    assert embedder.embed_passage_batch([]) == []
    mock_runtime.invoke_model.assert_not_called()


def test_cohere_embedder_satisfies_protocol() -> None:
    embedder = BedrockCohereEmbedder(bedrock=_make_client(MagicMock()))
    ref: Embedder = embedder
    assert callable(ref.embed) and callable(ref.embed_passage)


# ── build_embedder_from_env / ペア整合バリデーション ───────────────────────


def _clear_embed_env(mp: pytest.MonkeyPatch) -> None:
    mp.delenv("EMBEDDER_BACKEND", raising=False)
    mp.delenv("EMBEDDING_COLUMN", raising=False)


def test_build_embedder_default_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定（env 未設定）は LocalE5Embedder（後方互換）。__init__ をモデルロードさせず確認。"""
    _clear_embed_env(monkeypatch)
    created: dict[str, bool] = {}

    def _fake_local_init(self: Any, model_name: str | None = None) -> None:
        created["local"] = True

    monkeypatch.setattr(LocalE5Embedder, "__init__", _fake_local_init)
    emb = build_embedder_from_env()
    assert isinstance(emb, LocalE5Embedder)
    assert created.get("local") is True


def test_build_embedder_cohere_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """cohere⇄embedding_cohere の正しいペアで BedrockCohereEmbedder を返す。"""
    _clear_embed_env(monkeypatch)
    monkeypatch.setenv("EMBEDDER_BACKEND", "cohere")
    monkeypatch.setenv("EMBEDDING_COLUMN", "embedding_cohere")
    created: dict[str, bool] = {}

    def _fake_cohere_init(self: Any, bedrock: Any | None = None) -> None:
        created["cohere"] = True
        self.model_id = "cohere.embed-multilingual-v3"

    monkeypatch.setattr(BedrockCohereEmbedder, "__init__", _fake_cohere_init)
    emb = build_embedder_from_env()
    assert isinstance(emb, BedrockCohereEmbedder)
    assert created.get("cohere") is True


def test_build_embedder_mismatch_cohere_with_e5_column_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cohere backend なのに embedding(e5) 列＝空間不整合 → fail-loud。"""
    _clear_embed_env(monkeypatch)
    monkeypatch.setenv("EMBEDDER_BACKEND", "cohere")
    monkeypatch.setenv("EMBEDDING_COLUMN", "embedding")
    with pytest.raises(ValueError, match="不整合"):
        build_embedder_from_env()


def test_build_embedder_mismatch_local_with_cohere_column_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """local backend なのに embedding_cohere 列 → fail-loud。"""
    _clear_embed_env(monkeypatch)
    monkeypatch.setenv("EMBEDDER_BACKEND", "local")
    monkeypatch.setenv("EMBEDDING_COLUMN", "embedding_cohere")
    with pytest.raises(ValueError, match="不整合"):
        build_embedder_from_env()


def test_resolve_embedding_column_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_COLUMN", "embedding; DROP TABLE chunks")
    with pytest.raises(ValueError):
        resolve_embedding_column()


def test_validate_pair_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="EMBEDDER_BACKEND"):
        validate_embedder_column_pair("titan", "embedding")


def test_allowed_columns_are_the_two_known() -> None:
    assert ALLOWED_EMBEDDING_COLUMNS == frozenset({"embedding", "embedding_cohere"})
