"""retry claim の回数キャップと毒行掃き出しの実 SQL 検証。

``TEAMAGENT_TEST_DB_DSN`` が明示された disposable PostgreSQL だけで実行する。

背景（2026-08-10〜13 本番実測）: 決定論的に失敗するファイル（例: 256MiB 上限超の
download_too_large）が pending のまま毎日 claim され、download が lease(旧600s) を
食い潰して renew 失敗 → durability fail-closed で cursor が永続停止し、gdrive
取り込みが 4 日連続全断した。対策は (1) claim 毎に attempt_count を加算し、
(2) 上限到達行を claim 前に invalid_source へ掃き出す（ログで可視化）。
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator
from typing import Any

import pytest

from teamagent.ingest.repository import (
    _SOURCE_RETRY_MAX_CLAIM_ATTEMPTS,
    IngestRepository,
)

_DB_DSN = os.environ.get("TEAMAGENT_TEST_DB_DSN")
_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_0019 = _ROOT / "infra/migrations/0019_ingest_source_health.sql"
_MIGRATION_0020 = _ROOT / "infra/migrations/0020_ingest_source_retry_upgrade.sql"
_MIGRATION_0021 = _ROOT / "infra/migrations/0021_ingest_source_retry_lease_token.sql"

pytestmark = pytest.mark.skipif(
    _DB_DSN is None,
    reason="disposable PostgreSQL validation requires TEAMAGENT_TEST_DB_DSN",
)


class _TransactionPgVector:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @contextmanager
    def connection(self, **kwargs: Any) -> Iterator[Any]:
        yield self._conn


def _prepare_schema(conn: Any) -> str:
    from psycopg import sql

    schema = f"ingest_retry_cap_{uuid.uuid4().hex}"
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        cur.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema)))
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'teamagent_app'")
        if cur.fetchone() is None:
            pytest.skip("teamagent_app role is required")
        cur.execute("SELECT pg_has_role(current_user, 'teamagent_app', 'MEMBER')")
        if not bool(cur.fetchone()[0]):
            pytest.skip("test role cannot SET ROLE teamagent_app")
        cur.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO teamagent_app").format(sql.Identifier(schema))
        )
    return schema


@contextmanager
def _repository() -> Iterator[tuple[Any, IngestRepository]]:
    import psycopg

    assert _DB_DSN is not None
    with psycopg.connect(_DB_DSN) as conn:
        _prepare_schema(conn)
        with conn.cursor() as cur:
            cur.execute(_MIGRATION_0019.read_text(encoding="utf-8"))
            cur.execute(_MIGRATION_0020.read_text(encoding="utf-8"))
            cur.execute(_MIGRATION_0021.read_text(encoding="utf-8"))
        repository = IngestRepository(  # type: ignore[arg-type]
            _TransactionPgVector(conn),
            app_role=None,
        )
        yield conn, repository
        conn.rollback()


def _enqueue(repository: IngestRepository, conn: Any, external_id: str) -> None:
    assert repository.record_source_retry(
        source_kind="gdrive",
        source_id="folder",
        source_type="gdrive",
        external_id=external_id,
        md5_checksum="0123456789abcdef0123456789abcdef",
        size_bytes=10,
        mime_type="application/test",
        validator_schema_version="ooxml-safe-v2",
        reason="download_too_large",
        request_id="request-enqueue",
        allow_unclaimed=True,
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE ingest_source_retries SET next_attempt_at = now() WHERE external_id = %s",
            (external_id,),
        )


def _expire_lease(conn: Any, external_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ingest_source_retries
            SET lease_expires_at = now() - interval '1 second'
            WHERE external_id = %s
            """,
            (external_id,),
        )


def _row(conn: Any, external_id: str) -> tuple[Any, ...]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, attempt_count, lease_owner, lease_token, lease_expires_at
            FROM ingest_source_retries
            WHERE external_id = %s
            """,
            (external_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return tuple(row)


def test_each_claim_increments_attempt_count() -> None:
    with _repository() as (conn, repository):
        _enqueue(repository, conn, "poison-file")
        _, baseline, *_ = _row(conn, "poison-file")  # enqueue 時点の attempt_count（実測1）

        for i in range(1, _SOURCE_RETRY_MAX_CLAIM_ATTEMPTS - baseline + 1):
            claims = repository.claim_due_source_retries(
                source_kind="gdrive",
                source_id="folder",
                request_id=f"request-{i}",
            )
            assert [c.external_id for c in claims] == ["poison-file"]
            assert claims[0].attempt_count == baseline + i
            # 本番の毒ループ: lease が処理中に切れて解放され、翌日また claim される
            _expire_lease(conn, "poison-file")


def test_exhausted_row_is_swept_to_invalid_source_and_never_claimed_again() -> None:
    with _repository() as (conn, repository):
        _enqueue(repository, conn, "poison-file")
        _, baseline, *_ = _row(conn, "poison-file")

        for i in range(_SOURCE_RETRY_MAX_CLAIM_ATTEMPTS - baseline):
            claims = repository.claim_due_source_retries(
                source_kind="gdrive", source_id="folder", request_id=f"request-{i}"
            )
            assert claims, f"claim {i + 1} 回目が空"
            _expire_lease(conn, "poison-file")

        # 上限到達後の claim: 行は invalid_source へ掃き出され、claim は空になる
        claims = repository.claim_due_source_retries(
            source_kind="gdrive", source_id="folder", request_id="request-final"
        )
        assert claims == []
        status, attempts, owner, token, expires = _row(conn, "poison-file")
        assert status == "resolved"  # 0020 の CHECK は pending|resolved のみ
        assert attempts == _SOURCE_RETRY_MAX_CLAIM_ATTEMPTS
        # 0021 の lease 一貫性制約: 掃き出し時は 3 欄とも NULL
        assert owner is None and token is None and expires is None
        # 指紋は health 表で known-invalid 化され、再遭遇時の再取り込みも suppress される
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, reason FROM ingest_source_health
                WHERE external_id = 'poison-file'
                """
            )
            health = cur.fetchone()
        assert health is not None
        assert health[0] == "invalid_source"
        assert health[1] == "retry_exhausted:download_too_large"


def test_sweep_leaves_actively_leased_rows_alone() -> None:
    with _repository() as (conn, repository):
        _enqueue(repository, conn, "active-file")
        with conn.cursor() as cur:
            # 上限超だが lease が生きている＝いままさに処理中の行は掃かない
            cur.execute(
                """
                UPDATE ingest_source_retries
                SET attempt_count = %s,
                    lease_owner = 'worker-live',
                    lease_token = 'token-live',
                    lease_expires_at = now() + interval '10 minutes'
                WHERE external_id = 'active-file'
                """,
                (_SOURCE_RETRY_MAX_CLAIM_ATTEMPTS + 3,),
            )

        claims = repository.claim_due_source_retries(
            source_kind="gdrive", source_id="folder", request_id="request-live"
        )
        assert claims == []  # lease 中なので claim もされない
        status, *_ = _row(conn, "active-file")
        assert status == "pending"  # 掃かれてもいない


def test_fresh_rows_are_claimed_normally() -> None:
    with _repository() as (conn, repository):
        _enqueue(repository, conn, "fresh-file")
        claims = repository.claim_due_source_retries(
            source_kind="gdrive", source_id="folder", request_id="request-fresh"
        )
        assert [c.external_id for c in claims] == ["fresh-file"]
        status, attempts, *_ = _row(conn, "fresh-file")
        assert status == "pending"
        assert attempts == 2  # enqueue=1 + claim=1（claim は処理着手のカウント）
