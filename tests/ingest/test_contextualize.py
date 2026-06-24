"""ingest.contextualize: Contextual Retrieval ingest のテスト（ネットワーク無し）。

fake bedrock（prefix を返す）＋ fake embedder（固定ベクトル）で:
- contextualized が前置され、embedding が差し替わること。
- 例外時に元 chunk を返す（fail-open）こと。
を検証する。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from teamagent.ingest.contextualize import (
    ChunkContextualizer,
    build_contextualizer_from_env,
)
from teamagent.ingest.repository import ChunkUpsert


class _FakeBedrock:
    """converse() で固定の prefix を text に詰めて返す fake。"""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self.calls = 0

    def converse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(text=self._prefix)


class _RaisingBedrock:
    """converse() が常に例外を投げる fake（fail-open 検証用）。"""

    def converse(self, **kwargs: Any) -> SimpleNamespace:
        raise RuntimeError("bedrock down")


class _FakeEmbedder:
    """embed() が固定ベクトルを返す fake。受け取ったテキストを記録する。"""

    def __init__(self, vec: list[float]) -> None:
        self._vec = vec
        self.seen: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.seen.append(text)
        return list(self._vec)


class _RaisingEmbedder:
    """embed() が常に例外を投げる fake（fail-open 検証用）。"""

    def embed(self, text: str) -> list[float]:
        raise RuntimeError("embed failed")


_ORIG_EMBED = [0.1] * 1024
_NEW_EMBED = [0.9] * 1024


def _chunk(idx: int = 0, content: str = "PR代行の業界別実績") -> ChunkUpsert:
    return ChunkUpsert(chunk_idx=idx, content=content, embedding=list(_ORIG_EMBED))


def test_contextualized_prefixed_and_embedding_replaced() -> None:
    bedrock = _FakeBedrock(prefix="本資料は食品業界向けPR提案書の実績パートである")
    embedder = _FakeEmbedder(_NEW_EMBED)
    ctx = ChunkContextualizer(bedrock, embedder)

    chunk = _chunk(idx=2, content="アース製薬の事例")
    out = ctx.contextualize_chunks(
        doc_title="提案.pdf",
        full_text="文書全文……" * 50,
        chunks=[chunk],
        request_id="req-1",
    )

    assert len(out) == 1
    result = out[0]
    # 前置詞 + 元 content の結合
    assert (
        result.contextualized
        == "本資料は食品業界向けPR提案書の実績パートである\n\nアース製薬の事例"
    )
    # embedding は contextualized テキストで差し替わっている
    assert result.embedding == _NEW_EMBED
    assert result.embedding != _ORIG_EMBED
    # 他フィールドは保持
    assert result.chunk_idx == 2
    assert result.content == "アース製薬の事例"
    # embedder には contextualized テキストが渡る
    assert embedder.seen == [result.contextualized]


def test_multiple_chunks_all_contextualized() -> None:
    bedrock = _FakeBedrock(prefix="文脈")
    embedder = _FakeEmbedder(_NEW_EMBED)
    ctx = ChunkContextualizer(bedrock, embedder)

    chunks = [_chunk(idx=i, content=f"c{i}") for i in range(3)]
    out = ctx.contextualize_chunks(
        doc_title="d.pdf",
        full_text="全文",
        chunks=chunks,
        request_id="req-2",
    )

    assert [c.contextualized for c in out] == ["文脈\n\nc0", "文脈\n\nc1", "文脈\n\nc2"]
    assert all(c.embedding == _NEW_EMBED for c in out)
    assert bedrock.calls == 3


def test_fail_open_on_bedrock_error_keeps_original() -> None:
    bedrock = _RaisingBedrock()
    embedder = _FakeEmbedder(_NEW_EMBED)
    ctx = ChunkContextualizer(bedrock, embedder)

    chunk = _chunk(content="元のまま")
    out = ctx.contextualize_chunks(
        doc_title="d.pdf",
        full_text="全文",
        chunks=[chunk],
        request_id="req-3",
    )

    assert len(out) == 1
    # 元 chunk がそのまま返る（contextualized 据え置き＝None、embedding 不変）
    assert out[0] is chunk
    assert out[0].contextualized is None
    assert out[0].embedding == _ORIG_EMBED
    # embed は呼ばれない
    assert embedder.seen == []


def test_fail_open_on_embed_error_keeps_original() -> None:
    bedrock = _FakeBedrock(prefix="文脈")
    embedder = _RaisingEmbedder()
    ctx = ChunkContextualizer(bedrock, embedder)

    chunk = _chunk(content="保持される")
    out = ctx.contextualize_chunks(
        doc_title="d.pdf",
        full_text="全文",
        chunks=[chunk],
        request_id="req-4",
    )

    assert out[0] is chunk
    assert out[0].contextualized is None
    assert out[0].embedding == _ORIG_EMBED


def test_empty_prefix_keeps_original() -> None:
    bedrock = _FakeBedrock(prefix="   ")  # 空白のみ → 文脈付与せず据え置き
    embedder = _FakeEmbedder(_NEW_EMBED)
    ctx = ChunkContextualizer(bedrock, embedder)

    chunk = _chunk()
    out = ctx.contextualize_chunks(
        doc_title="d.pdf",
        full_text="全文",
        chunks=[chunk],
        request_id="req-5",
    )
    assert out[0] is chunk
    assert embedder.seen == []


def test_empty_full_text_returns_chunks_unchanged() -> None:
    bedrock = _FakeBedrock(prefix="文脈")
    embedder = _FakeEmbedder(_NEW_EMBED)
    ctx = ChunkContextualizer(bedrock, embedder)

    chunks = [_chunk()]
    out = ctx.contextualize_chunks(
        doc_title="d.pdf",
        full_text="   ",  # 空白のみ → 文書本文なし
        chunks=chunks,
        request_id="req-6",
    )
    assert out == chunks
    assert out[0].contextualized is None


def test_cache_system_enabled_in_converse_call() -> None:
    # 文書全文を cachePoint 化するため cache_system=True が渡ることを確認。
    captured: dict[str, Any] = {}

    class _CapturingBedrock:
        def converse(self, **kwargs: Any) -> SimpleNamespace:
            captured.update(kwargs)
            return SimpleNamespace(text="文脈")

    ctx = ChunkContextualizer(_CapturingBedrock(), _FakeEmbedder(_NEW_EMBED))
    ctx.contextualize_chunks(
        doc_title="d.pdf",
        full_text="文書全文テキスト",
        chunks=[_chunk()],
        request_id="req-7",
    )
    assert captured.get("cache_system") is True
    # 文書全文は system に載る
    assert "文書全文テキスト" in captured.get("system", "")


def test_build_from_env_disabled_returns_none(monkeypatch: Any) -> None:
    monkeypatch.delenv("USE_CONTEXTUAL_INGEST", raising=False)
    assert build_contextualizer_from_env() is None

    monkeypatch.setenv("USE_CONTEXTUAL_INGEST", "false")
    assert build_contextualizer_from_env() is None
