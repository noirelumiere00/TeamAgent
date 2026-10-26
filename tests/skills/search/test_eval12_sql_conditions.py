"""実adapterのSQL条件をSQLite fixtureで評価する。PostgreSQL RLS/ANNの実証ではない。"""

import json
import re
import sqlite3
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import PgVectorClient

ME = "pilot@example.test"
DOC = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def corpus():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    identity = {"email": ME, "groups": []}
    db.create_function("current_email", 0, lambda: identity["email"])
    db.create_function("current_groups", 0, lambda: json.dumps(identity["groups"]))
    db.create_function("regexp", 2, lambda pattern, value: bool(re.search(pattern, value or "")))
    db.executescript("""
        CREATE TABLE stored_documents (
            id TEXT, title TEXT, source_type TEXT, external_id TEXT, source_uri TEXT,
            metadata TEXT, owner_email TEXT, acl_emails TEXT, acl_groups TEXT
        );
        CREATE TABLE chunks (document_id TEXT, metadata TEXT);
        -- 本人/メールACL/グループACLをモデル化。PostgreSQLの実policyは変更しない。
        CREATE VIEW documents AS SELECT * FROM stored_documents
        WHERE lower(owner_email) = lower(current_email())
           OR EXISTS (SELECT 1 FROM json_each(acl_emails) WHERE lower(value) = lower(current_email()))
           OR EXISTS (SELECT 1 FROM json_each(acl_groups) g JOIN json_each(current_groups()) u
                      ON lower(g.value) = lower(u.value));
    """)
    db.execute(
        "INSERT INTO stored_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (DOC, "青空社の資料", "gdrive", "FILE1", "gdrive://FILE1", "{}", ME, "[]", "[]"),
    )
    db.execute("INSERT INTO chunks VALUES (?, ?)", (DOC, "{}"))
    yield db, identity
    db.close()


def sqlite_sql(sql):
    sql = re.sub(r"= ANY\(%s(?:::text\[\])?\)", "IN (SELECT value FROM json_each(?))", sql)
    sql = re.sub(
        r"COALESCE\(\(([^)]+)\)::bool, false\)", r"(COALESCE(\1, 'false') IN ('true', 1))", sql
    )
    return (
        sql.replace("::text", "")
        .replace("::uuid", "")
        .replace(" ~ ", " REGEXP ")
        .replace("ILIKE", "LIKE")
        .replace("%s", "?")
    )


def eligible_count(db, **kwargs):
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []
    PgVectorClient(dsn="postgresql://unused").search_similar_new_schema(
        conn, [0.1] * 1024, filter_source_types=["gdrive"], **kwargs
    )
    sql, params = cursor.execute.call_args.args
    where = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    values = [json.dumps(p) if isinstance(p, list) else p for p in params[1:-2]]
    return db.execute(
        "SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id WHERE "
        + sqlite_sql(where),
        values,
    ).fetchone()[0]


@pytest.mark.parametrize(
    "access", ["owner", "email_acl", "group_acl", "missing_group", "other_user"]
)
def test_gdrive_eligibility_requires_person_rls_fixture(corpus, access):
    db, identity = corpus
    if access != "owner":
        db.execute("UPDATE stored_documents SET owner_email = 'another@example.test'")
    if access == "email_acl":
        db.execute("UPDATE stored_documents SET acl_emails = ?", (json.dumps([ME.upper()]),))
    if access in {"group_acl", "missing_group"}:
        db.execute("UPDATE stored_documents SET acl_groups = '[\"example.test\"]'")
        identity["groups"] = ["EXAMPLE.TEST"] if access == "group_acl" else []
    assert eligible_count(db) == int(access in {"owner", "email_acl", "group_acl"})


@pytest.mark.parametrize("source_type", ["gdrive", "other", "gsheets", "slack"])
def test_gdrive_floor_does_not_include_other_source_types(corpus, source_type):
    db, _ = corpus
    db.execute("UPDATE stored_documents SET source_type = ?", (source_type,))
    assert eligible_count(db) == int(source_type == "gdrive")


@pytest.mark.parametrize(
    ("flag", "document_meta", "chunk_meta"),
    [
        ("exclude_boilerplate", {}, {"boilerplate": "true"}),
        ("exclude_templates", {"cls_is_template": "true"}, {}),
        ("exclude_recurring", {"cls_is_recurring": "true"}, {}),
    ],
)
def test_exclusion_flags_can_zero_the_visible_drive_pool(corpus, flag, document_meta, chunk_meta):
    db, _ = corpus
    db.execute("UPDATE stored_documents SET metadata = ?", (json.dumps(document_meta),))
    db.execute("UPDATE chunks SET metadata = ?", (json.dumps(chunk_meta),))
    assert eligible_count(db, **{flag: True}) == 0
    assert eligible_count(db, **{flag: False}) == 1


def test_explicit_client_and_sticky_filters_can_zero_drive_pool(corpus):
    db, _ = corpus
    assert eligible_count(db, metadata_contains={"__client__": "若葉社"}) == 0
    assert eligible_count(db, metadata_contains={"__client__": "青空社"}) == 1
    assert eligible_count(db, sticky_filters={"cls_doc_type": "提案書"}) == 0


def test_reauthorization_id_namespace_and_suppression_are_distinct_from_acl(corpus):
    db, _ = corpus
    db.execute("UPDATE stored_documents SET source_type = 'other', external_id = 'FILE1:case:hash'")
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []
    client = PgVectorClient(dsn="postgresql://unused")
    for ids, expected in [({}, 0), ({"document_ids": [DOC]}, 1)]:
        client.list_authorized_document_references(conn, drive_file_ids=["FILE1"], **ids)
        sql, params = cursor.execute.call_args.args
        rows = db.execute(sqlite_sql(sql), [json.dumps(p) for p in params]).fetchall()
        assert len(rows) == expected
    db.execute('UPDATE stored_documents SET metadata = \'{"suppressed":"true"}\'')
    assert db.execute(sqlite_sql(sql), [json.dumps(p) for p in params]).fetchall() == []


@pytest.mark.parametrize("canonical_visible", [False, True])
def test_duplicate_exclusion_depends_on_canonical_visibility(corpus, canonical_visible):
    db, _ = corpus
    canonical = "00000000-0000-0000-0000-000000000002"
    db.execute(
        "UPDATE stored_documents SET metadata = ?",
        (json.dumps({"suppressed": "true", "duplicate_of": canonical}),),
    )
    db.execute(
        "INSERT INTO stored_documents VALUES (?, '正本', 'other', 'canonical', '', '{}', ?, '[]', '[]')",
        (canonical, ME if canonical_visible else "another@example.test"),
    )
    assert eligible_count(db, exclude_duplicates=True) == int(not canonical_visible)
    assert eligible_count(db, exclude_duplicates=False) == 1


@pytest.mark.parametrize("embedding_col", ["embedding", "embedding_cohere"])
def test_null_score_is_an_adapter_error_not_a_successful_zero(embedding_col):
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [{"chunk_id": 1, "content": "資料", "score": None}]
    with pytest.raises(TypeError):
        PgVectorClient(dsn="postgresql://unused").search_similar_new_schema(
            conn, [0.1] * 1024, filter_source_types=["gdrive"], embedding_col=embedding_col
        )
    sql = cursor.execute.call_args.args[0]
    assert f"1 - (c.{embedding_col} <=> %s::vector) AS score" in sql
    assert f"ORDER BY c.{embedding_col} <=> %s::vector" in sql
