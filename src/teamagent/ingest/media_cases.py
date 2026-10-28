"""Media corpus on the existing documents/chunks tables; no connector migration."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from teamagent.adapters.embeddings_client import Embedder, resolve_embedding_column
from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.identity import shared_company_domains_from_env
from teamagent.ingest.repository import ChunkUpsert, DocumentUpsert, IngestRepository

CORPUS = "media_case"
STATE_CORPUS = "media_case_state"
OWNER = "media-case-ingest@internal.invalid"


class MediaCaseStore:
    def __init__(self, pg: PgVectorClient, embedder: Embedder) -> None:
        self.pg = pg
        self.embedder = embedder
        self.repository = IngestRepository(pg, owner_email=OWNER)
        self.column = resolve_embedding_column()

    @contextmanager
    def worker_lock(self) -> Iterator[Callable[[], None] | None]:
        """A dedicated session lock serializes workers across all MCP replicas."""
        # No pooled session retains a lock, and no long-running transaction pins old rows.
        with PgVectorClient(self.pg.dsn).connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(1847264301) AS acquired")
                row = cur.fetchone()
            conn.commit()
            if not row or not row["acquired"]:
                yield None
                return

            connection_lock = threading.Lock()

            def alive() -> None:
                with connection_lock:
                    conn.execute("SELECT 1")
                    conn.commit()

            try:
                yield alive
            finally:
                conn.execute("SELECT pg_advisory_unlock(1847264301)")
                conn.commit()

    def read(self, external_id: str) -> dict[str, Any]:
        with self.pg.connection(app_role="teamagent_app", user_email=OWNER) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT metadata FROM documents WHERE source_type='other' AND external_id=%s",
                    (external_id,),
                )
                row = cur.fetchone()
        return dict(row["metadata"]) if row else {}

    def save_state(self, handle: str, state: dict[str, Any]) -> None:
        self.repository.upsert_document_with_chunks(
            DocumentUpsert(
                source_type="other",
                external_id=f"media_case_state:{handle}",
                owner_email=OWNER,
                metadata={**state, "corpus": STATE_CORPUS},
            ),
            [],
            request_id="media-case-state",
        )

    def state(self, handle: str) -> dict[str, Any]:
        return self.read(f"media_case_state:{handle}")

    def post(self, post_id: str) -> dict[str, Any]:
        return self.read(f"media_case:tiktok:{post_id}")

    def save_post(self, metadata: dict[str, Any], text: str, request_id: str) -> None:
        groups = sorted(shared_company_domains_from_env() or ())
        if not groups:
            raise ValueError("company sharing scope unavailable")
        self.repository.upsert_document_with_chunks(
            DocumentUpsert(
                source_type="other",
                external_id=f"media_case:tiktok:{metadata['post_id']}",
                owner_email=OWNER,
                acl_groups=groups,
                title=str(metadata["title"]),
                source_uri=metadata["post_url"],
                modified_at=metadata.get("posted_at"),
                metadata={**metadata, "corpus": CORPUS},
            ),
            [
                ChunkUpsert(
                    chunk_idx=0,
                    content=text,
                    embedding=self.embedder.embed_passage(text),
                    embedding_column=self.column,
                )
            ]
            if text.strip()
            else [],
            request_id=request_id,
        )

    def update_metrics(self, post_id: str, metrics: dict[str, Any], observed_at: str) -> None:
        with self.pg.connection(app_role="teamagent_app", user_email=OWNER) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE documents SET metadata=metadata || %s::jsonb "
                    "WHERE source_type='other' AND external_id=%s AND metadata->>'corpus'=%s",
                    (
                        json.dumps({"metrics": metrics, "metrics_observed_at": observed_at}),
                        f"media_case:tiktok:{post_id}",
                        CORPUS,
                    ),
                )

    def coverage(self, handles: list[str], identity: dict[str, Any]) -> int:
        if not identity.get("identity_verified") or not identity.get("user_email"):
            raise PermissionError("verified caller required")
        with self.pg.connection(
            app_role="teamagent_app",
            user_email=identity["user_email"],
            user_groups=list(identity.get("user_groups") or []),
            user_role="member",
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS count FROM documents WHERE source_type='other' "
                    "AND metadata->>'corpus'='media_case' "
                    "AND metadata->>'deleted' IS DISTINCT FROM 'true' "
                    "AND metadata->>'account_handle'=ANY(%s)",
                    (handles,),
                )
                row = cur.fetchone()
        return int(row["count"]) if row else 0

    def search(
        self, query: str, filters: dict[str, Any], identity: dict[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        if not identity.get("identity_verified") or not identity.get("user_email"):
            raise PermissionError("verified caller required")
        with self.pg.connection(
            app_role="teamagent_app",
            user_email=identity["user_email"],
            user_groups=list(identity.get("user_groups") or []),
            user_role="member",
        ) as conn:
            return self.pg.search_media_cases(
                conn,
                self.embedder.embed(query),
                filters=filters,
                query=query,
                embedding_col=self.column,
                limit=limit,
            )
