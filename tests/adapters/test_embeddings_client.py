"""LocalE5Embedder の非対称プレフィックス（query/passage）と env gate のテスト。

intfloat/multilingual-e5-large は非対称学習モデルで、検索クエリは "query: "、文書／
パッセージは "passage: " のプレフィックスを付けるのが正しい。実モデル
（sentence-transformers）は CI に無いため、``__init__`` を介さず ``_model`` を fake に
差し替え、プレフィックス付与ロジックと E5_PASSAGE_PREFIX gate だけを検証する。
"""

from __future__ import annotations

import pytest

from teamagent.adapters.embeddings_client import (
    LocalE5Embedder,
    _env_flag,
    _passage_prefix_from_env,
)


class _Vec:
    """sentence-transformers の encode 戻り値（ndarray）の .tolist() を模す。"""

    def __init__(self, data: list[float]) -> None:
        self._data = data

    def tolist(self) -> list[float]:
        return list(self._data)


class _FakeModel:
    """encode() に渡された (text, normalize) を記録する fake モデル。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def encode(self, text: str, normalize_embeddings: bool = False) -> _Vec:
        self.calls.append((text, normalize_embeddings))
        return _Vec([0.1, 0.2, 0.3])


def _make(*, passage_enabled: bool) -> tuple[LocalE5Embedder, _FakeModel]:
    """重い __init__（モデルロード）を回避して fake モデルを注入した embedder を作る。"""
    emb = LocalE5Embedder.__new__(LocalE5Embedder)
    model = _FakeModel()
    emb._model = model
    emb.model_name = "fake-e5"
    emb._passage_prefix_enabled = passage_enabled
    return emb, model


def test_embed_uses_query_prefix_and_normalizes() -> None:
    emb, model = _make(passage_enabled=False)
    out = emb.embed("猫がかわいい")
    assert model.calls[-1] == ("query: 猫がかわいい", True)
    assert out == [0.1, 0.2, 0.3]


def test_embed_query_prefix_even_when_passage_gate_on() -> None:
    # クエリ側は gate に関係なく常に "query: "（検索面は不変）。
    emb, model = _make(passage_enabled=True)
    emb.embed("検索クエリ")
    assert model.calls[-1][0] == "query: 検索クエリ"


def test_embed_passage_falls_back_to_query_when_gate_off() -> None:
    # 後方互換: gate OFF の間は passage 指定でも "query: "（既存コーパスと同一サブ空間）。
    emb, model = _make(passage_enabled=False)
    emb.embed_passage("当社のPR代行は…")
    assert model.calls[-1][0] == "query: 当社のPR代行は…"


def test_embed_passage_uses_passage_prefix_when_gate_on() -> None:
    emb, model = _make(passage_enabled=True)
    emb.embed_passage("当社のPR代行は…")
    assert model.calls[-1][0] == "passage: 当社のPR代行は…"


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on", " On "])
def test_env_flag_truthy(monkeypatch: pytest.MonkeyPatch, truthy: str) -> None:
    monkeypatch.setenv("E5_PASSAGE_PREFIX", truthy)
    assert _env_flag("E5_PASSAGE_PREFIX") is True


@pytest.mark.parametrize("falsy", ["0", "false", "no", "off", ""])
def test_env_flag_falsy(monkeypatch: pytest.MonkeyPatch, falsy: str) -> None:
    monkeypatch.setenv("E5_PASSAGE_PREFIX", falsy)
    assert _env_flag("E5_PASSAGE_PREFIX") is False


def test_env_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("E5_PASSAGE_PREFIX", raising=False)
    assert _env_flag("E5_PASSAGE_PREFIX") is False


def test_encode_does_not_double_add_query_prefix() -> None:
    """QW-1: 既に "query: " で始まる文字列には二重付与しない。"""
    emb, model = _make(passage_enabled=False)
    emb.embed("query: 既に付与済み")
    assert model.calls[-1][0] == "query: 既に付与済み"


def test_encode_does_not_double_add_passage_prefix() -> None:
    """QW-1: 既に "passage: " で始まる文字列には（gate ON でも）二重付与しない。"""
    emb, model = _make(passage_enabled=True)
    emb.embed_passage("passage: 既に付与済み")
    assert model.calls[-1][0] == "passage: 既に付与済み"


def test_encode_passage_prefix_not_confused_for_query_side() -> None:
    """QW-1: gate ON で passage: 付与済みの文字列を query 側に渡しても二重付与しない。

    （二重付与ガードは prefix 種別を問わず先頭一致で判定するため、クロスでも安全）。
    """
    emb, model = _make(passage_enabled=False)
    emb.embed("passage: クロス入力")
    assert model.calls[-1][0] == "passage: クロス入力"


def test_passage_prefix_gate_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """QW-1: 両 env 名が未設定なら passage gate は OFF（既定で全 query:＝後方互換）。"""
    monkeypatch.delenv("USE_E5_PASSAGE_PREFIX", raising=False)
    monkeypatch.delenv("E5_PASSAGE_PREFIX", raising=False)
    assert _passage_prefix_from_env() is False


def test_passage_prefix_gate_canonical_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """QW-1: 仕様の正準名 USE_E5_PASSAGE_PREFIX=1 で gate が ON になる。"""
    monkeypatch.delenv("E5_PASSAGE_PREFIX", raising=False)
    monkeypatch.setenv("USE_E5_PASSAGE_PREFIX", "1")
    assert _passage_prefix_from_env() is True


def test_passage_prefix_gate_legacy_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """QW-1: 旧名 E5_PASSAGE_PREFIX=1 でも gate が ON（後方互換エイリアス）。"""
    monkeypatch.delenv("USE_E5_PASSAGE_PREFIX", raising=False)
    monkeypatch.setenv("E5_PASSAGE_PREFIX", "true")
    assert _passage_prefix_from_env() is True


def test_passage_prefix_gate_either_name_enables(monkeypatch: pytest.MonkeyPatch) -> None:
    """QW-1: どちらか一方が真なら ON（OR 結合）。一方 OFF・他方 ON でも有効。"""
    monkeypatch.setenv("USE_E5_PASSAGE_PREFIX", "0")
    monkeypatch.setenv("E5_PASSAGE_PREFIX", "yes")
    assert _passage_prefix_from_env() is True


def test_local_e5_embedder_satisfies_embedder_protocol() -> None:
    # Embedder Protocol（embed + embed_passage）を構造的に満たすことを確認。
    from teamagent.adapters.embeddings_client import Embedder

    emb, _ = _make(passage_enabled=False)
    ref: Embedder = emb
    assert callable(ref.embed) and callable(ref.embed_passage)
