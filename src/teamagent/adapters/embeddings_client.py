"""Embedding adapter。

3層分離の Adapter 層。Skill からは Embedder Protocol 経由で呼ぶ。
sentence-transformers / Bedrock Titan / OpenAI 等のバックエンドを差し替え可能にする。

Sprint 1 時点：multilingual-e5-large（ローカル、1024次元）
Sprint 3+ : Bedrock Titan Embed v2 に差し替え予定（同じ 1024 次元）

Usage:
    embedder = LocalE5Embedder()
    vec = embedder.embed("query: PR代行の業界別実績は？")
"""

from __future__ import annotations

import os
import time
from typing import Any, Protocol

import structlog

logger = structlog.get_logger(__name__)


class Embedder(Protocol):
    """Embedder の共通インターフェース。

    SearchSkill はこの Protocol だけを参照する。
    実装の差し替え（local / Bedrock）が Skill 側で意識されないようにする。
    """

    def embed(self, text: str) -> list[float]:
        """テキストを 1024 次元のベクトルに変換する。"""
        ...


class LocalE5Embedder:
    """multilingual-e5-large（ローカル）を使う Embedder。

    sentence-transformers が必須。初回呼び出し時にモデルをロードする。
    1024 次元のベクトルを返す。
    """

    DEFAULT_MODEL: str = "intfloat/multilingual-e5-large"

    def __init__(self, model_name: str | None = None) -> None:
        # sentence-transformers をモジュール内 import（依存重いので遅延）
        from sentence_transformers import SentenceTransformer

        name = model_name or os.environ.get("LOCAL_EMBED_MODEL", self.DEFAULT_MODEL)
        self.model_name = name
        start = time.perf_counter()
        self._model: Any = SentenceTransformer(name)
        load_ms = int((time.perf_counter() - start) * 1000)
        logger.info("embedder_loaded", model=name, load_ms=load_ms, backend="local-e5")

    def embed(self, text: str) -> list[float]:
        """テキストを 1024 次元ベクトルに変換する。

        e5 系モデルは "query: <text>" のプレフィックスで検索意図を伝えるのが推奨。
        ここでは検索用クエリと判断して prefix を付ける。
        """
        start = time.perf_counter()
        vec = self._model.encode(
            f"query: {text}",
            normalize_embeddings=True,
        ).tolist()
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "embedder_embed",
            model=self.model_name,
            text_len=len(text),
            dim=len(vec),
            latency_ms=latency_ms,
        )
        return list(vec)
