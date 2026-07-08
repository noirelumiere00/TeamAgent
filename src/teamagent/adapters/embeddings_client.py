"""Embedding adapter。

3層分離の Adapter 層。Skill からは Embedder Protocol 経由で呼ぶ。
sentence-transformers / Bedrock Titan / OpenAI 等のバックエンドを差し替え可能にする。

Sprint 1 時点：multilingual-e5-large（ローカル、1024次元）
Sprint 3+ : Bedrock Titan Embed v2 に差し替え予定（同じ 1024 次元）

e5 系は **非対称** 学習モデル。検索クエリは "query: "、文書／パッセージは "passage: "
のプレフィックスを付けるのが正しい。クエリは ``embed()``、取り込み資料は
``embed_passage()`` を使い分ける（プレフィックスは内部で付与するので呼び出し側は素の
テキストを渡す）。

Usage:
    embedder = LocalE5Embedder()
    qvec = embedder.embed("PR代行の業界別実績は？")     # 検索クエリ → "query: "
    pvec = embedder.embed_passage("当社のPR代行は…")    # 取り込み資料 → "passage: "
"""

from __future__ import annotations

import os
import time
import uuid
from typing import TYPE_CHECKING, Any, Protocol

import structlog

if TYPE_CHECKING:
    from teamagent.adapters.bedrock_client import BedrockClient

logger = structlog.get_logger(__name__)


def _env_flag(name: str) -> bool:
    """環境変数を真偽値として読む（"1"/"true"/"yes"/"on" を真、既定 False）。"""
    return os.environ.get(name, "false").strip().lower() in ("1", "true", "yes", "on")


# QW-1: passage プレフィックスを切る env フラグ名。仕様の正準名は ``USE_E5_PASSAGE_PREFIX``。
# 既存の取り込み/再 embed の運用ドキュメント・テストは旧名 ``E5_PASSAGE_PREFIX`` に依存して
# いるため、両名を許可（どちらかが真なら有効）して後方互換を保つ。どちらも未設定なら OFF。
_PASSAGE_PREFIX_ENV_NAMES = ("USE_E5_PASSAGE_PREFIX", "E5_PASSAGE_PREFIX")


def _passage_prefix_from_env() -> bool:
    """passage プレフィックス gate を env から解決（正準名＋旧名エイリアス、既定 OFF）。"""
    return any(_env_flag(name) for name in _PASSAGE_PREFIX_ENV_NAMES)


class Embedder(Protocol):
    """Embedder の共通インターフェース。

    SearchSkill は検索クエリの埋め込みに ``embed()`` を、取り込み（ingest）側は文書／
    パッセージの埋め込みに ``embed_passage()`` を使う。e5 系の非対称性に合わせ両方を持つ。
    実装の差し替え（local / Bedrock）が Skill 側で意識されないようにする。
    """

    def embed(self, text: str) -> list[float]:
        """検索クエリを 1024 次元ベクトルに変換する（"query: " プレフィックス）。"""
        ...

    def embed_passage(self, text: str) -> list[float]:
        """文書／パッセージを 1024 次元ベクトルに変換する（"passage: " プレフィックス）。

        e5 系は非対称学習のため、取り込み側はクエリと別プレフィックスで埋め込む。
        """
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
        # e5 の非対称プレフィックス（passage 側）は **コーパス全体の再取込** が前提。
        # USE_E5_PASSAGE_PREFIX（正準名・旧名 E5_PASSAGE_PREFIX もエイリアス）が無効な間は
        # embed_passage() でも "query: " を付与し、既存コーパス（"query: " で埋め込み済み）と
        # 同一サブ空間を保つ＝完全後方互換。段階導入: 取り込みプロセスで
        # USE_E5_PASSAGE_PREFIX=1 にして全コーパスを再 embed し、検索側（embed()=常に
        # "query: "）と非対称ペアを成立させてから本番反映する。
        self._passage_prefix_enabled = _passage_prefix_from_env()
        start = time.perf_counter()
        self._model: Any = SentenceTransformer(name)
        load_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "embedder_loaded",
            model=name,
            load_ms=load_ms,
            backend="local-e5",
            passage_prefix=self._passage_prefix_enabled,
        )

    def embed(self, text: str) -> list[float]:
        """検索クエリを 1024 次元ベクトルに変換する（常に "query: " プレフィックス）。"""
        return self._encode(text, "query")

    def embed_passage(self, text: str) -> list[float]:
        """文書／パッセージを 1024 次元ベクトルに変換する（取り込み側）。

        USE_E5_PASSAGE_PREFIX（旧名 E5_PASSAGE_PREFIX）が有効なときだけ "passage: " を
        付与する。無効時は後方互換のため "query: " を付与する（既存コーパスと同一サブ空間を維持）。
        """
        prefix = "passage" if self._passage_prefix_enabled else "query"
        return self._encode(text, prefix)

    def _encode(self, text: str, prefix: str) -> list[float]:
        """``"{prefix}: {text}"`` を埋め込み、正規化済み 1024 次元ベクトルを返す。

        QW-1: 二重付与ガード。既に ``"query: "`` / ``"passage: "`` で始まる文字列には
        プレフィックスを足さない（呼び出し側が誤って付与済みの値を渡しても安全）。
        """
        prefixed = text if text.startswith(("query: ", "passage: ")) else f"{prefix}: {text}"
        start = time.perf_counter()
        vec = self._model.encode(
            prefixed,
            normalize_embeddings=True,
        ).tolist()
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "embedder_embed",
            model=self.model_name,
            prefix=prefix,
            text_len=len(text),
            dim=len(vec),
            latency_ms=latency_ms,
        )
        return list(vec)


class BedrockCohereEmbedder:
    """Bedrock Cohere Embed multilingual v3 を使う Embedder（1024 次元・L2 正規化済）。

    Cohere v3 は **非対称** 埋め込みを ``input_type`` でネイティブに表現する。e5 の
    "query: "/"passage: " プレフィックスに相当する区別をここで閉じる:
    - ``embed()``        → input_type="search_query"   （検索クエリ）
    - ``embed_passage()``→ input_type="search_document"（取り込み資料）

    非対称をクラス境界に閉じることで、検索側と取り込み側のサブ空間整合がクラス内で
    保証される（env を跨いで片方だけ切替わる事故を構造的に防ぐ）。boto3 直叩きは禁止
    （CLAUDE.md 3層）のため、リクエストは ``BedrockClient.embed_texts()`` に委譲する。
    """

    def __init__(self, bedrock: BedrockClient | None = None) -> None:
        # 重い import（boto3 経由）を遅延させ、本モジュール import 自体は軽量に保つ。
        from teamagent.adapters.bedrock_client import BedrockClient

        self._bedrock = bedrock or BedrockClient.from_env()
        self.model_id = self._bedrock.embed_model_id
        logger.info(
            "embedder_loaded",
            model=self.model_id,
            backend="bedrock-cohere",
        )

    def embed(self, text: str) -> list[float]:
        """検索クエリを 1024 次元ベクトルに変換する（input_type=search_query）。"""
        return self._one(text, "search_query")

    def embed_passage(self, text: str) -> list[float]:
        """文書／パッセージを 1024 次元ベクトルに変換する（input_type=search_document）。"""
        return self._one(text, "search_document")

    def embed_passage_batch(self, texts: list[str]) -> list[list[float]]:
        """複数のパッセージを一括で埋め込む（再 embed の往復削減用・Protocol 外）。

        ``embed_passage()`` と同じ input_type=search_document。空入力は空リストを返す。
        BedrockClient.embed_texts が 96 件超を内部で分割する。
        """
        if not texts:
            return []
        resp = self._bedrock.embed_texts(
            list(texts),
            request_id=f"embed-batch-{uuid.uuid4().hex[:8]}",
            input_type="search_document",
        )
        return resp.embeddings

    def _one(self, text: str, input_type: str) -> list[float]:
        """単一テキストを embed_texts 経由で 1 ベクトルに変換する。"""
        resp = self._bedrock.embed_texts(
            [text],
            request_id=f"embed-{uuid.uuid4().hex[:8]}",
            input_type=input_type,
        )
        if not resp.embeddings:
            raise RuntimeError("BedrockCohereEmbedder: embeddings が空で返りました")
        return resp.embeddings[0]


# ----------------------------------------------------------------------------
# 単一構築点（EMBEDDER_BACKEND）＋ backend×column ペア整合バリデーション
# ----------------------------------------------------------------------------

# EMBEDDER_BACKEND と、その backend が書く/読む chunks の embedding 列の正準ペア。
# local（e5）⇄ embedding 列 / cohere ⇄ embedding_cohere 列。
# 検索側クエリ空間と列のベクトル空間がこのペアで一致する。不一致は全コーパスが別空間に
# なり検索全壊するため、起動時に fail-loud で落とす（下の validate_embedder_column_pair）。
_BACKEND_COLUMN_PAIR: dict[str, str] = {
    "local": "embedding",
    "cohere": "embedding_cohere",
}
_DEFAULT_BACKEND = "local"
# EMBEDDING_COLUMN として SQL 識別子に埋め込んでよい許可リスト（injection 防止）。
ALLOWED_EMBEDDING_COLUMNS: frozenset[str] = frozenset(_BACKEND_COLUMN_PAIR.values())


def resolve_embedder_backend() -> str:
    """``EMBEDDER_BACKEND`` を解決する（既定 ``local``＝後方互換）。"""
    return os.environ.get("EMBEDDER_BACKEND", _DEFAULT_BACKEND).strip().lower() or _DEFAULT_BACKEND


def resolve_embedding_column() -> str:
    """``EMBEDDING_COLUMN`` を解決する（既定 ``embedding``＝後方互換・許可リスト検証付き）。"""
    col = os.environ.get("EMBEDDING_COLUMN", "embedding").strip() or "embedding"
    if col not in ALLOWED_EMBEDDING_COLUMNS:
        raise ValueError(
            f"EMBEDDING_COLUMN は {sorted(ALLOWED_EMBEDDING_COLUMNS)} のいずれか (got {col!r})"
        )
    return col


def validate_embedder_column_pair(backend: str, column: str) -> None:
    """backend と embedding 列のペア整合を検証し、不一致なら ValueError で落とす。

    cohere ⇄ embedding_cohere / local ⇄ embedding 以外は、クエリ空間と列のベクトル空間が
    食い違い検索が全壊するため、起動時 fail-loud で停止させる（設定不変条件チェックの系譜）。
    """
    expected = _BACKEND_COLUMN_PAIR.get(backend)
    if expected is None:
        raise ValueError(
            f"EMBEDDER_BACKEND は {sorted(_BACKEND_COLUMN_PAIR)} のいずれか (got {backend!r})"
        )
    if column != expected:
        raise ValueError(
            "EMBEDDER_BACKEND と EMBEDDING_COLUMN のペアが不整合です: "
            f"backend={backend!r} は列 {expected!r} を要求しますが EMBEDDING_COLUMN={column!r} "
            "が設定されています。検索クエリと列のベクトル空間が食い違い検索が全壊します。"
            "（cohere⇄embedding_cohere / local⇄embedding でペア指定してください）"
        )


def build_embedder_from_env() -> Embedder:
    """``EMBEDDER_BACKEND``（既定 local）から Embedder を構築する**単一構築点**。

    併せて ``EMBEDDING_COLUMN`` とのペア整合を起動時に検証する（不一致は fail-loud）。
    既定（EMBEDDER_BACKEND 未設定＝local / EMBEDDING_COLUMN 未設定＝embedding）では
    ``LocalE5Embedder()`` を返し、従来挙動とバイト等価（後方互換）。
    """
    backend = resolve_embedder_backend()
    column = resolve_embedding_column()
    validate_embedder_column_pair(backend, column)
    if backend == "cohere":
        return BedrockCohereEmbedder()
    return LocalE5Embedder()
