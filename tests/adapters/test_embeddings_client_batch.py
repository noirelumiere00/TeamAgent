"""LocalE5Embedder のパッセージ一括埋め込みテスト。"""

from __future__ import annotations

import pytest

from teamagent.adapters.embeddings_client import LocalE5Embedder


class _Vector:
    """sentence-transformers の単一 encode 戻り値を模す。"""

    def __init__(self, data: list[float]) -> None:
        self._data = data

    def tolist(self) -> list[float]:
        return list(self._data)


class _Matrix:
    """sentence-transformers のバッチ encode 戻り値を模す。"""

    def __init__(self, data: list[list[float]]) -> None:
        self._data = data

    def tolist(self) -> list[list[float]]:
        return [list(row) for row in self._data]


class _FakeModel:
    """単一・リスト入力の両方を受け、入力ごとに決定的なベクトルを返す fake。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str | list[str], bool]] = []

    @staticmethod
    def _vector(text: str) -> list[float]:
        return [float(len(text)), float(sum(ord(char) for char in text) % 1000)]

    def encode(
        self,
        text: str | list[str],
        normalize_embeddings: bool = False,
    ) -> _Vector | _Matrix:
        self.calls.append((text, normalize_embeddings))
        if isinstance(text, list):
            return _Matrix([self._vector(item) for item in text])
        return _Vector(self._vector(text))


def _make(*, passage_enabled: bool) -> tuple[LocalE5Embedder, _FakeModel]:
    """重いモデルロードを避け、バッチ対応 fake モデルを注入する。"""
    embedder = LocalE5Embedder.__new__(LocalE5Embedder)
    model = _FakeModel()
    embedder._model = model
    embedder.model_name = "fake-e5"
    embedder._passage_prefix_enabled = passage_enabled
    return embedder, model


@pytest.mark.parametrize(
    ("passage_enabled", "expected_prefix"),
    [(False, "query"), (True, "passage")],
)
def test_embed_passage_batch_matches_sequential_and_preserves_prefix(
    passage_enabled: bool,
    expected_prefix: str,
) -> None:
    """バッチと逐次で同一ベクトルを返し、既存の prefix gate を変えない。"""
    texts = ["一つ目", "二つ目の文書", "query: 付与済み", "passage: 付与済み"]
    sequential, sequential_model = _make(passage_enabled=passage_enabled)
    batched, batch_model = _make(passage_enabled=passage_enabled)

    expected = [sequential.embed_passage(text) for text in texts]
    actual = batched.embed_passage_batch(texts)

    expected_inputs = [
        f"{expected_prefix}: 一つ目",
        f"{expected_prefix}: 二つ目の文書",
        "query: 付与済み",
        "passage: 付与済み",
    ]
    assert actual == expected
    assert [call[0] for call in sequential_model.calls] == expected_inputs
    # 文字列リストを 1 回で encode し、正規化指定も逐次経路と同じにする。
    assert batch_model.calls == [(expected_inputs, True)]


def test_embed_passage_batch_empty_does_not_call_model() -> None:
    embedder, model = _make(passage_enabled=False)

    assert embedder.embed_passage_batch([]) == []
    assert model.calls == []
