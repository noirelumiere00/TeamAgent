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
                    chunk_idx int, content text, metadata jsonb, contextualized text
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
            "INSERT INTO chunks (id, document_id, chunk_idx, content, metadata) VALUES (%s,%s,%s,%s,%s)",
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
        assert {key: clients[0][key] for key in ("name", "document_count", "latest_fb_at")} == {
            "name": "FBのみ",
            "document_count": 0,
            "latest_fb_at": "2026-01-01",
        }
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
        assert [
            {key: row[key] for key in ("name", "document_count", "latest_fb_at")} for row in clients
        ] == [{"name": "株式会社 Project 様", "document_count": 1, "latest_fb_at": None}]
        docs, doc_total = pg.list_knowledge_documents(conn)
        assert doc_total == 2  # Unnamed documents remain documents, but create no client.
        assert {row["client_name"] for row in docs} == {"株式会社 Project 様", None}


class HttpPg(PgVectorClient):
    """Use production queries/session injection inside the fixture's rollback transaction."""

    def __init__(self, conn: Any) -> None:
        super().__init__("fixture")
        self.conn = conn

    @contextmanager
    def connection(self, **kwargs: Any) -> Iterator[Any]:
        assert kwargs["app_role"] == "teamagent_app" and kwargs["user_role"] == "user"
        PgVectorClient._apply_session(self.conn, **kwargs)
        yield self.conn


def test_detail_http_rls_private_and_missing_are_identical(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from teamagent.connect_web.app import create_app
    from teamagent.dashboard.auth import make_session
    from tests.connect_web.test_search_routes import _SECRET, _config

    private = seed(db, "A私有タイトル", owner="a@example.com", extra={"cls_industry": "私有タグ"})
    shared = seed(db, "会社共有", groups=("example.com",), extra={"cls_industry": "食品"})
    excluded = [
        seed(db, "除外重複", groups=("example.com",), extra={"suppressed": "true"}),
        seed(db, "除外研究", groups=("example.com",), extra={"x_research_tool": "tool"}),
        seed(db, "除外FB", groups=("example.com",), fb=True),
    ]
    db.execute(
        "UPDATE chunks SET metadata = %s WHERE document_id = %s AND chunk_idx = 0",
        [Jsonb({"boilerplate": True}), shared],
    )
    db.execute(
        "UPDATE chunks SET content = 'plain text', contextualized = %s WHERE document_id = %s AND chunk_idx = 1",
        ["文脈本文" + "あ" * 2500, shared],
    )
    pg = HttpPg(db)
    monkeypatch.setattr(PgVectorClient, "from_env", lambda: pg)
    client = TestClient(create_app(search_config=_config()))
    cookie_b = {"ta_search_session": make_session("b@example.com", _SECRET, ttl_s=3600)}
    blocked = client.get(f"/search?view=doc&id={private}", cookies=cookie_b)
    missing = client.get(f"/search?view=doc&id={uuid.uuid4()}", cookies=cookie_b)
    assert blocked.status_code == missing.status_code == 404
    assert blocked.text == missing.text
    assert "見つからないか、閲覧できません" in blocked.text
    assert "A私有" not in blocked.text and "私有タグ" not in blocked.text
    for document_id in excluded:
        response = client.get(f"/search?view=doc&id={document_id}", cookies=cookie_b)
        assert response.status_code == 404 and response.text == missing.text
    visible = client.get(f"/search?view=doc&id={shared}", cookies=cookie_b)
    assert visible.status_code == 200 and "会社共有" in visible.text
    state = db.execute(
        "SELECT current_user AS role, current_setting('app.user_email') AS email, current_setting('app.user_role') AS user_role"
    ).fetchone()
    assert state == {"role": "teamagent_app", "email": "b@example.com", "user_role": "user"}
    detail = pg.get_knowledge_document(db, shared)
    assert detail["excerpt"] == ("文脈本文" + "あ" * 2500)[:2000]
    assert detail["industry"] == "食品" and detail["doc_type"] == "提案書"
    assert detail["modified_at"] == "2026-01-01" and detail["id"] == shared
    cookie_a = {"ta_search_session": make_session("a@example.com", _SECRET, ttl_s=3600)}
    response = client.get(f"/search?view=doc&id={private}", cookies=cookie_a)
    assert response.status_code == 200 and "A私有タイトル" in response.text
    assert "私有タグ" in response.text and "本文識別語" in response.text
    # Reusing the exact connection for B must not retain A's visibility.
    assert client.get(f"/search?view=doc&id={private}", cookies=cookie_b).text == missing.text


def test_tags_rls_all_visible_documents_and_shared_counts(db: Any) -> None:
    seed(
        db,
        "A私有",
        owner="a@example.com",
        extra={"cls_industry": "Aだけの業界", "cls_doc_type": "Aだけの種類"},
    )
    seed(db, "B私有", owner="b@example.com", extra={"cls_industry": "Bだけの業界"})
    for _ in range(2):
        seed(db, "共有", groups=("example.com",), extra={"cls_industry": "食品"})
    seed(db, "A共有タグ私有", owner="a@example.com", extra={"cls_industry": "食品"})
    for extra in ({"suppressed": "true"}, {"x_research_tool": "tool"}, {"is_sales_fb": "true"}):
        seed(db, "対象外", groups=("example.com",), extra={"cls_industry": "除外業界", **extra})
    seed(db, "他人のみ", extra={"cls_industry": "他人のみの業界"})
    pg = PgVectorClient("fixture")
    for email, own, other, food_count, total in (
        ("a@example.com", "A", "B", 3, 4),
        ("b@example.com", "B", "A", 2, 3),
    ):
        with TransactionPg(db).connection(email) as conn:
            tags = pg.list_knowledge_tags(conn)
            indexed = {(r["axis"], r["value"]): r["document_count"] for r in tags}
            assert indexed[("industry", "食品")] == food_count
            assert indexed[("industry", own + "だけの業界")] == 1
            assert indexed[("client_name", "共有")] == 2  # two chunks per document, still two docs
            assert indexed[("source_type", "gdrive")] == total
            assert other + "だけの" not in repr(tags)
            assert "除外業界" not in repr(tags) and "他人のみ" not in repr(tags)
            for row in tags:
                docs, count = pg.list_knowledge_documents(conn, tag=(row["axis"], row["value"]))
                assert count == row["document_count"] == len(docs)
                assert "対象外" not in repr(docs)


def test_tag_filtered_rls_totals_paging_exact_values_and_axis_isolation(db: Any) -> None:
    shared_ids = [
        seed(db, "共有", groups=("example.com",), extra={"cls_industry": "食品"}) for _ in range(51)
    ]
    private = seed(db, "Aのみ", owner="a@example.com", extra={"cls_industry": "食品"})
    other_axis = seed(
        db,
        "別軸",
        groups=("example.com",),
        extra={"cls_industry": "別業界", "cls_doc_type": "食品"},
    )
    seed(db, "部分一致除外", groups=("example.com",), extra={"cls_industry": "食品加工"})
    percent = seed(db, "百分率", groups=("example.com",), extra={"cls_industry": "50%_O'Reilly"})
    for extra in ({"suppressed": "true"}, {"x_research_tool": "tool"}, {"is_sales_fb": "true"}):
        seed(db, "除外", groups=("example.com",), extra={"cls_industry": "食品", **extra})
    pg = PgVectorClient("fixture")
    for email, expected in (("a@example.com", 52), ("b@example.com", 51)):
        with TransactionPg(db).connection(email) as conn:
            first, count = pg.list_knowledge_documents(conn, tag=("industry", "食品"))
            second, total = pg.list_knowledge_documents(conn, tag=("industry", "食品"), offset=50)
            assert count == total == expected
            assert len(first) == 50 and len(second) == expected - 50
            visible_ids = shared_ids + ([private] if email.startswith("a") else [])
            assert [r["id"] for r in first + second] == sorted(visible_ids)
            assert pg.list_knowledge_documents(conn, tag=("industry", "食品"), offset=100) == (
                [],
                expected,
            )
            assert (
                pg.list_knowledge_documents(conn, tag=("industry", "食品"), offset=50)[0] == second
            )
            by_type, count = pg.list_knowledge_documents(conn, tag=("doc_type", "食品"))
            assert count == 1 and [r["id"] for r in by_type] == [other_axis]
            exact, count = pg.list_knowledge_documents(conn, tag=("industry", "50%_O'Reilly"))
            assert count == 1 and [r["id"] for r in exact] == [percent]
            assert pg.list_knowledge_documents(conn, tag=("industry", "不存在")) == ([], 0)


def test_operator_rls_private_words_tags_and_negation(db: Any) -> None:
    private = seed(db, "Aだけの提案", owner="a@example.com", extra={"cls_industry": "私有タグ"})
    shared = seed(db, "会社提案", groups=("example.com",), extra={"cls_industry": "食品"})
    draft = seed(db, "会社提案 下書き", groups=("example.com",), extra={"cls_industry": "食品"})
    minutes = seed(
        db,
        "会議メモ",
        groups=("example.com",),
        extra={"cls_industry": "IT", "cls_doc_type": "議事録"},
    )
    for extra in ({"suppressed": "true"}, {"x_research_tool": "tool"}, {"is_sales_fb": "true"}):
        seed(db, "会社提案", groups=("example.com",), extra={"cls_industry": "食品", **extra})
    db.execute(
        "UPDATE chunks SET contextualized=%s WHERE document_id=%s", ["本文にしかない秘密", private]
    )
    pg = PgVectorClient("fixture")
    for email in ("a@example.com", "b@example.com"):
        with TransactionPg(db).connection(email) as conn:
            for q in ("Aだけ", "本文にしかない秘密", "tag:私有タグ", "file:Aだけ"):
                rows, total = pg.list_knowledge_documents(conn, q=q)
                assert total == (1 if email.startswith("a") else 0)
                assert [r["id"] for r in rows] == ([private] if email.startswith("a") else [])
            rows, total = pg.list_knowledge_documents(conn, q="食品 file:提案 -下書き")
            assert total == 1 and [r["id"] for r in rows] == [shared]
            rows, total = pg.list_knowledge_documents(conn, q="tag:食品 -file:下書き")
            assert total == 1 and [r["id"] for r in rows] == [shared]
            rows, total = pg.list_knowledge_documents(conn, q="-tag:食品 file:会議")
            assert total == 1 and [r["id"] for r in rows] == [minutes]
            rows, total = pg.list_knowledge_documents(conn, q="file:下書き")
            assert total == 1 and [r["id"] for r in rows] == [draft]
            rows, total = pg.list_knowledge_documents(conn, q="tag:gdrive -tag:食品 -tag:私有タグ")
            assert total == 1 and [r["id"] for r in rows] == [minutes]


def test_operator_literal_wildcards_normalization_phrase_and_excerpt(db: Any) -> None:
    special = seed(db, r"50%_売上\案", groups=("example.com",))
    seed(db, "50XX売上x案", groups=("example.com",))
    normalized = seed(
        db,
        "ＦＵＬＬ－ＷＩＤＴＨ 提案",
        groups=("example.com",),
        extra={"cls_industry": "ＣＡＳＥ ＳＴＵＤＹ"},
    )
    seed(db, "跨越", groups=("example.com",), extra={"cls_industry": "字段"})
    parent = seed(db, "親", groups=("example.com",), extra={"cls_industry": "食品"})
    seed(db, "子", groups=("example.com",), extra={"cls_industry": "食品/飲料"})
    excerpt = seed(db, "本文資料", groups=("example.com",))
    db.execute(
        "UPDATE chunks SET metadata=%s, content=%s WHERE document_id=%s AND chunk_idx=0",
        [Jsonb({"title_only": True}), "テンプレだけの語", excerpt],
    )
    db.execute(
        "UPDATE chunks SET contextualized=%s WHERE document_id=%s AND chunk_idx=1",
        ["代表本文の固有語" + "あ" * 2100 + "上限外の語", excerpt],
    )
    pg = PgVectorClient("fixture")
    with TransactionPg(db).connection("b@example.com") as conn:
        for q in ("50%", "50%_", r"売上\案", "file:50%_"):
            rows, total = pg.list_knowledge_documents(conn, q=q)
            assert total == 1 and [r["id"] for r in rows] == [special]
        for q in ("fullwidth", "ＦＵＬＬＷＩＤＴＨ", '"full width"', 'tag:"case study"'):
            rows, total = pg.list_knowledge_documents(conn, q=q)
            assert total == 1 and [r["id"] for r in rows] == [normalized]
        assert pg.list_knowledge_documents(conn, q='"跨越字段"') == ([], 0)
        rows, total = pg.list_knowledge_documents(conn, q="tag:食品")
        assert total == 1 and [r["id"] for r in rows] == [
            parent
        ]  # flat tags, no hierarchy expansion
        rows, total = pg.list_knowledge_documents(conn, q="代表本文の固有語")
        assert total == 1 and [r["id"] for r in rows] == [excerpt]
        assert pg.list_knowledge_documents(conn, q="テンプレだけの語") == ([], 0)
        assert pg.list_knowledge_documents(conn, q="上限外の語") == ([], 0)


def test_operator_and_quick_filtered_totals_paging_and_recent(db: Any) -> None:
    from datetime import timedelta

    now = db.execute("SELECT CURRENT_TIMESTAMP AS now").fetchone()["now"]
    shared = [
        seed(db, "対象提案", groups=("example.com",), extra={"cls_industry": "食品"})
        for _ in range(51)
    ]
    private = seed(db, "対象提案", owner="a@example.com", extra={"cls_industry": "食品"})
    seed(db, "対象提案 下書き", groups=("example.com",), extra={"cls_industry": "食品"})
    seed(
        db,
        "対象提案",
        groups=("example.com",),
        extra={"cls_industry": "食品", "cls_doc_type": "議事録"},
    )
    recent = seed(
        db, "最近の資料", groups=("example.com",), modified=(now - timedelta(days=1)).isoformat()
    )
    old = seed(
        db, "古い資料", groups=("example.com",), modified=(now - timedelta(days=90)).isoformat()
    )
    future = seed(
        db, "未来の資料", groups=("example.com",), modified=(now + timedelta(days=1)).isoformat()
    )
    # Independent time cases should not share the proposal tag.
    pg = PgVectorClient("fixture")
    for email, total in (("a@example.com", 52), ("b@example.com", 51)):
        with TransactionPg(db).connection(email) as conn:
            filters = {"q": "file:対象 -下書き", "tag": ("industry", "食品"), "quick": "proposal"}
            first, count = pg.list_knowledge_documents(conn, **filters)
            second, second_count = pg.list_knowledge_documents(conn, offset=50, **filters)
            assert count == second_count == total and len(first) == 50
            assert len(second) == total - 50
            assert [r["id"] for r in first + second] == sorted(
                shared + ([private] if total == 52 else [])
            )
            assert pg.list_knowledge_documents(conn, offset=100, **filters) == ([], total)
            assert pg.list_knowledge_documents(conn, offset=50, **filters)[0] == second
            rows, count = pg.list_knowledge_documents(conn, q="file:資料", quick="recent")
            assert count == 1 and [r["id"] for r in rows] == [recent]
            assert old not in repr(rows) and future not in repr(rows)
            rows, count = pg.list_knowledge_documents(conn, q="file:対象", quick="minutes")
            assert count == 1 and rows[0]["doc_type"] == "議事録"


def test_client_attributes_counts_filters_and_http_are_scoped_by_real_rls(
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date, timedelta
    from urllib.parse import urlencode

    from fastapi.testclient import TestClient

    from teamagent.connect_web.app import create_app
    from teamagent.dashboard.auth import make_session
    from tests.connect_web.test_search_routes import _SECRET, _config

    today = date.today()
    name = "属性検証顧客"
    doc_date = (today - timedelta(days=450)).isoformat()
    for i in range(55):
        fb_date = (today - timedelta(days=550 - i)).isoformat()
        fid = seed(
            db,
            name,
            groups=("example.com",),
            fb=True,
            modified=fb_date,
            extra={"negative_reaction": f"長い懸念の記録 {i}", "next_action": "共有の次の連絡"},
        )
        db.execute(
            "UPDATE chunks SET contextualized=%s WHERE document_id=%s",
            ["本文" * 200 + "\n送信者: 沖井 タイムスタンプ: 2024/1/1 10:00", fid],
        )
        seed(db, name, groups=("example.com",), modified=doc_date, extra={"cls_industry": "食品"})
    private = seed(
        db,
        name,
        owner="a@example.com",
        fb=True,
        modified=today.isoformat(),
        extra={"negative_reaction": "特になし", "next_action": "私有の別アクション"},
    )
    db.execute(
        "UPDATE chunks SET contextualized=%s WHERE document_id=%s",
        ["本文" * 200 + "\n送信者: 私有担当 タイムスタンプ: 2026/1/1 10:00", private],
    )
    seed(
        db,
        name,
        groups=("example.com",),
        modified=(today + timedelta(days=100)).isoformat(),
        extra={"client_name": "別取引先"},
    )  # title-only relationship must not alter activity dates/counts
    for extra in ({"suppressed": "true"}, {"x_research_tool": "tool"}):
        seed(
            db,
            name,
            groups=("example.com",),
            fb=True,
            modified=(today + timedelta(days=200)).isoformat(),
            extra=extra,
        )
    db.execute("""
        UPDATE documents SET metadata = jsonb_set(metadata, '{is_sales_fb}', 'true'::jsonb)
        WHERE metadata->>'is_sales_fb' = 'true'
    """)
    pg = HttpPg(db)
    monkeypatch.setattr(PgVectorClient, "from_env", lambda: pg)
    client = TestClient(create_app(search_config=_config()))
    observed_b = None
    for email in ("b@example.com", "a@example.com", "b@example.com"):
        cookie = {"ta_search_session": make_session(email, _SECRET, ttl_s=3600)}
        response = client.get("/api/v1/client/" + name, cookies=cookie)
        assert response.status_code == 200
        payload = response.json()
        attrs = payload["attributes"]
        assert attrs["doc_count"] == 55 and attrs["fb_count"] == (
            56 if email.startswith("a") else 55
        )
        assert len(payload["timeline"]) == len(payload["documents"]) == 50
        assert payload["header"]["fb_count"] == payload["header"]["doc_count"] == 50
        if email.startswith("b"):
            assert attrs["owners"] == ["沖井智哉"]
            assert attrs["temperature"] == "ネガ優勢" and attrs["last_contact"] == doc_date
            assert attrs["next_action"] == "共有の次の連絡"
            assert attrs["has_homework"] and attrs["followup_overdue"]
            assert "私有" not in repr(payload)
            if observed_b is not None:
                assert attrs == observed_b
            observed_b = attrs
        else:
            assert set(attrs["owners"]) == {"私有担当", "沖井智哉"}
            assert attrs["temperature"] == "高" and attrs["last_contact"] == today.isoformat()
            assert attrs["next_action"] == "私有の別アクション"
            assert not attrs["has_homework"] and not attrs["followup_overdue"]
        listing, count = pg.list_knowledge_clients(db)
        assert count == 2
        row = next(row for row in listing if row["name"] == name)
        assert row == attrs
        for quick in ("homework", "negative", "overdue", "high"):
            selected, total = pg.list_knowledge_clients(db, quick=quick)
            expected = (quick == "high") == email.startswith("a")
            assert (any(row["name"] == name for row in selected)) == expected
            assert total == (1 if expected else 0)
            assert pg.list_knowledge_clients(db, quick=quick, offset=50) == ([], total)
            page = client.get(
                "/search?" + urlencode({"view": "clients", "quick": quick}), cookies=cookie
            )
            assert page.status_code == 200
            if email.startswith("b"):
                assert "私有" not in page.text
