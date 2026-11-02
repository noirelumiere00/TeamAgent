"""Real document RLS/list queries; disposable schema, no vector extension required."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.client_identity import client_identity_key

_DSN = os.environ.get("TEAMAGENT_TEST_DB_DSN")
_MIGRATIONS = Path(__file__).resolve().parents[2] / "infra/migrations"
pytestmark = pytest.mark.skipif(
    not _DSN and not os.environ.get("CI"),
    reason="Actual DB validation requires TEAMAGENT_TEST_DB_DSN",
)


class TransactionPg:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def connection(self, email: str) -> Iterator[Any]:
        PgVectorClient._apply_session(self.conn, "teamagent_app", email, ["Example.COM"], "user")
        state = self.conn.execute("""
            SELECT current_user AS role, current_setting('app.user_email') AS email,
                   current_setting('app.user_groups') AS groups,
                   current_setting('app.user_role') AS user_role,
                   rolsuper, rolbypassrls
            FROM pg_roles WHERE rolname = current_user
        """).fetchone()
        assert state == {
            "role": "teamagent_app",
            "email": email,
            "groups": "Example.COM",
            "user_role": "user",
            "rolsuper": False,
            "rolbypassrls": False,
        }
        yield self.conn


@pytest.fixture
def db() -> Iterator[Any]:
    assert _DSN, "CI must configure TEAMAGENT_TEST_DB_DSN; RLS checks must not skip"
    with psycopg.connect(_DSN, row_factory=dict_row) as conn:
        schema = f"knowledge_test_{uuid.uuid4().hex}"
        try:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            conn.execute(
                sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(schema))
            )
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO teamagent_app").format(sql.Identifier(schema))
            )
            conn.execute("""
                CREATE TABLE documents (
                    id uuid PRIMARY KEY, title text, modified_at timestamptz,
                    source_uri text, source_type text, metadata jsonb NOT NULL,
                    owner_email text, acl_emails text[], acl_groups text[]
                );
                CREATE TABLE chunks (
                    id uuid PRIMARY KEY, document_id uuid REFERENCES documents(id),
                    chunk_idx int, content text, metadata jsonb
                );
                GRANT SELECT ON documents, chunks TO teamagent_app;
            """)
            # Copy the production ENABLE/FORCE and parent-document policy verbatim.
            migration = (_MIGRATIONS / "0001_unified_documents.sql").read_text()
            policies = migration.split("ALTER TABLE documents ENABLE ROW LEVEL SECURITY;", 1)[1]
            policies = policies.split("-- 後方互換:", 1)[0]
            conn.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY;" + policies)
            conn.execute((_MIGRATIONS / "0010_rls_email_case_insensitive.sql").read_text())
            yield conn
        finally:
            conn.rollback()


def seed(
    conn: Any,
    name: str,
    *,
    owner: str = "third@example.com",
    emails: tuple[str, ...] = (),
    groups: tuple[str, ...] = (),
    fb: bool = False,
    modified: str | None = "2026-01-01T00:00:00Z",
    extra: dict[str, Any] | None = None,
    document_id: uuid.UUID | None = None,
) -> str:
    doc_id = document_id or uuid.uuid4()
    metadata = {"client_name": name, "cls_doc_type": "提案書", **(extra or {})}
    if fb:
        metadata["is_sales_fb"] = "true"
    conn.execute(
        """INSERT INTO documents VALUES (%s,%s,%s,%s,'gdrive',%s,%s,%s,%s)""",
        [
            doc_id,
            name + (" FBタイトル" if fb else " 資料タイトル"),
            modified,
            f"gdrive://{doc_id}",
            Jsonb(metadata),
            owner,
            list(emails),
            list(groups),
        ],
    )
    for i in range(2):
        conn.execute(
            "INSERT INTO chunks VALUES (%s,%s,%s,%s,%s)",
            [uuid.uuid4(), doc_id, i, name + " 本文識別語", Jsonb({})],
        )
    return str(doc_id)


def test_rls_clients_documents_totals_latest_fb_and_session_switch(db: Any) -> None:
    fixtures = [
        ("A所有秘密", "A@Example.COM", (), ()),
        ("Aメール秘密", "third@example.com", ("A@EXAMPLE.COM",), ()),
        ("B所有秘密", "B@Example.COM", (), ()),
        ("Bメール秘密", "third@example.com", ("B@EXAMPLE.COM",), ()),
        ("共通", "third@example.com", (), ("EXAMPLE.com",)),
        ("別group秘密", "third@example.com", (), ("other.com",)),
        ("第三者秘密", "third@example.com", (), ()),
    ]
    ids: dict[str, str] = {}
    for name, owner, emails, groups in fixtures:
        ids[name] = seed(db, name, owner=owner, emails=emails, groups=groups)
        seed(
            db,
            "株式会社 共通" if name == "共通" else name,
            owner=owner,
            emails=emails,
            groups=groups,
            fb=True,
        )
    seed(db, "共通 様", owner="a@example.com", fb=True, modified="2026-03-01T00:00:00Z")
    seed(db, "除外suppressed", groups=("example.com",), extra={"suppressed": "true"})
    seed(db, "除外research", groups=("example.com",), extra={"x_research_tool": "tool"})
    # Hidden/excluded FB must not move the latest visible date either.
    seed(
        db,
        "共通",
        groups=("example.com",),
        fb=True,
        modified="2027-01-01T00:00:00Z",
        extra={"suppressed": "true"},
    )
    seed(
        db,
        "共通",
        groups=("example.com",),
        fb=True,
        modified="2028-01-01T00:00:00Z",
        extra={"x_research_tool": "tool"},
    )
    pg = PgVectorClient("fixture")
    sessions = TransactionPg(db)
    for email, own, other, latest in (
        ("a@example.com", "A", "B", "2026-03-01"),
        ("b@example.com", "B", "A", "2026-01-01"),
        ("A@EXAMPLE.COM", "A", "B", "2026-03-01"),
    ):
        with sessions.connection(email) as conn:
            clients, total = pg.list_knowledge_clients(conn)
            docs, doc_total = pg.list_knowledge_documents(conn)
            assert total == doc_total == 3
            by_key = {client_identity_key(row["name"]): row for row in clients}
            assert set(by_key) == {
                client_identity_key(own + "所有秘密"),
                client_identity_key(own + "メール秘密"),
                "共通",
            }
            assert all(row["document_count"] == 1 for row in clients)
            assert by_key["共通"]["latest_fb_at"] == latest
            assert all(
                row["latest_fb_at"] == (latest if key == "共通" else "2026-01-01")
                for key, row in by_key.items()
            )
            assert {row["id"] for row in docs} == {
                ids[own + "所有秘密"],
                ids[own + "メール秘密"],
                ids["共通"],
            }
            assert all(
                row["modified_at"] == "2026-01-01" and row["doc_type"] == "提案書" for row in docs
            )
            # Check every output field, and the actual parent-inherited chunks policy.
            output = repr((clients, docs, conn.execute("SELECT content FROM chunks").fetchall()))
            for hidden in (other + "所有秘密", other + "メール秘密", "別group秘密", "第三者秘密"):
                assert hidden not in output
            assert "除外" not in repr((clients, docs))
            empty, count = pg.list_knowledge_clients(conn, offset=50)
            assert empty == [] and count == 3
            empty, count = pg.list_knowledge_documents(conn, offset=50)
            assert empty == [] and count == 3


def test_paging_over_existing_caps_ties_aliases_and_null_dates(db: Any) -> None:
    # 601 exceeds both the new page size and the existing graph cap; insert in reverse order.
    for i in reversed(range(601)):
        seed(db, f"顧客{i:04}", groups=("example.com",), document_id=uuid.UUID(int=i + 1))
    seed(
        db,
        "株式会社 顧客0000 様",
        groups=("example.com",),
        modified=None,
        document_id=uuid.UUID(int=602),
    )
    seed(db, "FBのみ", groups=("example.com",), fb=True)
    pg = PgVectorClient("fixture")
    with TransactionPg(db).connection("a@example.com") as conn:
        first, total = pg.list_knowledge_documents(conn)
        second, second_total = pg.list_knowledge_documents(conn, offset=50)
        assert total == second_total == 602
        assert [row["id"] for row in first] == [str(uuid.UUID(int=i)) for i in range(1, 51)]
        assert [row["id"] for row in second] == [str(uuid.UUID(int=i)) for i in range(51, 101)]
        assert pg.list_knowledge_documents(conn, offset=50) == (second, total)
        assert pg.list_knowledge_documents(conn, offset=601)[0][0]["id"] == str(uuid.UUID(int=602))
        assert pg.list_knowledge_documents(conn, offset=650) == ([], total)
        clients, client_total = pg.list_knowledge_clients(conn)
        assert client_total == 602  # 601 normalized names + FB-only client
        assert clients[0] == {"name": "FBのみ", "document_count": 0, "latest_fb_at": "2026-01-01"}
        assert clients[1]["document_count"] == 2  # aliases merged before paging/counting
        second_clients, _ = pg.list_knowledge_clients(conn, offset=50)
        assert [client_identity_key(r["name"]) for r in second_clients] == [
            f"顧客{i:04}" for i in range(49, 99)
        ]
        assert pg.list_knowledge_clients(conn, offset=50)[0] == second_clients
        assert pg.list_knowledge_clients(conn, offset=650) == ([], client_total)


def test_empty_rls_visible_set_and_project_name_fallback(db: Any) -> None:
    seed(db, "他人だけの名前", owner="third@example.com", fb=True)
    seed(db, "他人だけの名前", owner="third@example.com")
    pg = PgVectorClient("fixture")
    sessions = TransactionPg(db)
    with sessions.connection("a@example.com") as conn:
        assert pg.list_knowledge_clients(conn) == ([], 0)
        assert pg.list_knowledge_documents(conn) == ([], 0)
        assert conn.execute("SELECT * FROM chunks").fetchall() == []
    db.execute("RESET ROLE")
    seed(db, "", groups=("example.com",), extra={"cls_project": "株式会社 Project 様"})
    seed(db, "  ", groups=("example.com",))
    with sessions.connection("b@example.com") as conn:
        clients, total = pg.list_knowledge_clients(conn)
        assert total == 1
        assert clients == [
            {"name": "株式会社 Project 様", "document_count": 1, "latest_fb_at": None}
        ]
        docs, doc_total = pg.list_knowledge_documents(conn)
        assert doc_total == 2  # Unnamed documents remain documents, but create no client.
        assert {row["client_name"] for row in docs} == {"株式会社 Project 様", None}
