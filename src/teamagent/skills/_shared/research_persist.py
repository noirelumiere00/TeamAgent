"""カタログ系スキルの成果物を pgvector documents へ永続化する（Part1・外部脳化）。

x_research(声集め/ニーズ/バズ) と tiktok_comment_mining が生成した「構造化要約 markdown」を
IngestRepository 経由で **1 document = 1 chunk** として保存する。目的＝「過去にどんな施策研究/
提案をしたか」を Aico Vault(Obsidian)/@Aico 検索で振り返れる外部脳にすること。

設計:
- 書込はユーザー応答の**ホットパス外**（module-level ThreadPoolExecutor に fire-and-forget）。
  MCP プロセスは常駐なのでワーカースレッドは完走する。失敗は fail-open（専用ログキーで可観測化）。
- embedding は MCP 常駐の LocalE5 を再利用（無課金）。backend が local/e5⇄embedding 列でなければ
  no-op（cohere 空間で embedding 列を汚染しない fail-closed）。
- source_type='other'（ENUM 新値不可）、metadata.cls_project=商材名（Vault のクライアント anchor
  の必須条件）。is_sales_fb/suppressed/stale は付けない（export_vault の除外条件を回避）。
- external_id=xresearch:{tool}:{商材slug}:{JST日付} で同日同商材同ツールは UPDATE＝1件に集約。
- 段階公開ゲート USE_RESEARCH_PERSIST は factory 側で persister を None/有効に切替（本モジュールは
  persister が skill へ注入された時だけ動く＝OFF なら完全 no-op・後方互換）。
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt
import hashlib
import unicodedata
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# 直列・fire-and-forget の単一ワーカー（順序保証不要・同時 DB 接続を1本に抑える）。
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="research-persist"
)

_JST = _dt.timezone(_dt.timedelta(hours=9))


def _jst_today() -> str:
    return _dt.datetime.now(_JST).strftime("%Y-%m-%d")


def _product_key(name: str) -> str:
    """商材名の external_id 用識別キー（正規化名の sha256[:16]）。

    可読 slug でなくハッシュにするのは、external_id が IngestRepository のログに出るため商材名
    （機密でありうる）を平文で残さないため（通常資料の external_id も gdrive:// 等の ID で名前を
    含まない＝同じ姿勢）。同一名は同一キー（冪等）・lossy 衝突無し（正規化名全体を hash）。
    """
    norm = unicodedata.normalize("NFKC", (name or "").strip())
    return hashlib.sha256(("product:" + norm).encode("utf-8")).hexdigest()[:16]


def _owner_hash(email: str) -> str:
    """owner_email の照合用短ハッシュ。external_id をユーザー別にして、同日同商材を別々の営業が

    実行しても互いの本文/owner/ACL を上書きしないようにする（email は平文で持たない）。
    """
    norm = (email or "").strip().lower()
    return hashlib.sha256(("owner:" + norm).encode("utf-8")).hexdigest()[:8]


def _backend_ok() -> bool:
    """既存コーパスと同じ空間（local/e5 ⇄ embedding 列）でのみ永続化を許す。

    cohere 等に切り替わっている環境では no-op（embedding 列を別空間ベクトルで汚染しない）。
    """
    from teamagent.adapters.embeddings_client import (
        resolve_embedder_backend,
        resolve_embedding_column,
    )

    return resolve_embedder_backend() == "local" and resolve_embedding_column() == "embedding"


class ResearchPersister:
    """カタログ成果物を documents へ永続化する（fire-and-forget・fail-open）。"""

    def __init__(
        self,
        *,
        pgvector: Any,
        embedder: Any,
        executor: concurrent.futures.Executor | None = None,
    ) -> None:
        self._pgvector = pgvector  # 常駐 PgVectorClient（.connection() 毎回新規＝スレッド安全）
        self._embedder = embedder  # MCP 常駐の LocalE5（embed_passage を再利用・無課金）
        self._executor = executor or _EXECUTOR

    def schedule(
        self,
        *,
        tool: str,
        product_name: str,
        title: str,
        body_md: str,
        owner_email: str,
        request_id: str,
        cls_solution: str,
        cls_doc_type: str,
        dedup_key: str | None = None,
        source_uri: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        """永続化を非同期に予約する（即 return・ユーザー応答を遅らせない）。

        product_name/body_md が空、または backend が local/embedding でない場合は no-op。
        商材名(cls_project)が空だと export_vault が拾えないため、空なら記録しない。
        dedup_key: external_id の識別子（未指定なら JST日付＝1日1件集約）。buzz は再ポーリングで
        日を跨いでも重複しないよう job_id を渡す（同一 job は同一 doc を UPDATE）。
        source_uri: Vault ノートに載せる元レポートへのリンク（短縮URL 等）。本文全文がノートに
        入る前提の**補助**リンク（失効しても本文は残る）。
        """
        if not (product_name or "").strip() or not (body_md or "").strip():
            return
        if not _backend_ok():
            logger.info("research_persist_skipped_backend", request_id=request_id, tool=tool)
            return
        try:
            self._executor.submit(
                self._persist,
                tool=tool,
                product_name=product_name.strip(),
                title=title,
                body_md=body_md,
                owner_email=owner_email,
                request_id=request_id,
                cls_solution=cls_solution,
                cls_doc_type=cls_doc_type,
                dedup_key=dedup_key,
                source_uri=source_uri,
                extra_metadata=dict(extra_metadata or {}),
            )
        except Exception:  # executor shutdown 等でも本処理は落とさない
            logger.warning("research_persist_submit_failed", request_id=request_id, tool=tool)

    def _persist(
        self,
        *,
        tool: str,
        product_name: str,
        title: str,
        body_md: str,
        owner_email: str,
        request_id: str,
        cls_solution: str,
        cls_doc_type: str,
        dedup_key: str | None,
        source_uri: str | None,
        extra_metadata: dict[str, Any],
    ) -> None:
        try:
            from teamagent.ingest.pipeline import _company_acl_groups
            from teamagent.ingest.repository import (
                ChunkUpsert,
                DocumentUpsert,
                IngestRepository,
            )

            embedding = self._embedder.embed_passage(body_md)
            owner = (owner_email or "").strip()
            metadata: dict[str, Any] = {
                "cls_project": product_name,  # Vault クライアント anchor（clients/<商材>.md）
                "cls_solution": cls_solution,  # /app の 施策/ タグ
                "cls_doc_type": cls_doc_type,  # /app の 資料種別/ タグ
                "x_research_tool": tool,
            }
            metadata.update(extra_metadata)  # 界隈タグ等（Phase C）を後付けできる
            # external_id にツール・商材・**owner**・日付/ジョブを含める。owner を入れることで
            # 同日同商材を別々の営業が実行しても互いの doc/owner/ACL を上書きしない（衝突回避）。
            key_id = dedup_key or _jst_today()
            ext_id = f"xresearch:{tool}:{_product_key(product_name)}:{_owner_hash(owner)}:{key_id}"
            doc = DocumentUpsert(
                source_type="other",  # document_source_type ENUM は新値不可
                external_id=ext_id,
                owner_email=owner,
                acl_emails=[owner] if owner else [],
                acl_groups=_company_acl_groups(),  # §G 会社横断（未設定なら []）
                title=title,
                # 元レポートへの補助リンク（本文全文はノートに入るので失効しても記録は残る）。
                source_uri=(source_uri or None),
                metadata=metadata,
                modified_at=_dt.datetime.now(_JST).isoformat(),
            )
            chunk = ChunkUpsert(chunk_idx=0, content=body_md, embedding=embedding)
            repo = IngestRepository(self._pgvector)
            doc_id = repo.upsert_document_with_chunks(doc, [chunk], request_id=request_id)
            # 商材名(機密でありうる)は CloudWatch に残さない。tool と document_id のみ記録。
            logger.info(
                "research_persist_done", request_id=request_id, tool=tool, document_id=doc_id
            )
        except Exception as e:  # fail-open: 記録失敗はユーザー応答に影響させない
            logger.warning(
                "research_persist_failed", request_id=request_id, tool=tool, error=type(e).__name__
            )


__all__ = ["ResearchPersister"]
