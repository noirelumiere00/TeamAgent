#!/usr/bin/env python
"""P0 敵対ハーネス用の実DB fixture を投入/削除する（要 DATABASE_URL・SSMトンネル/承認後）。

会社共有モデルの RLS を実証する最小データ:
  - company1 / company2 @会社ドメイン の doc（acl_groups=[会社ドメイン]）= 会社メンバーに可視。
  - outsider@evil.com の doc（acl_emails=[outsider]・acl_groups=[]）= 会社メンバーに**不可視であるべき**。
    本文に固有トークン(OUTSIDER_ONLY_TOKEN)を含め、attack_mcp.py がどの詐称 vector でも漏れないことを確認。
全 doc に共通検索語(P0HARNESS)を入れ search 候補に載せる（RLS で会社外が落ちるのを観測）。

Usage:
    DATABASE_URL=... TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp \
      python scripts/ingest_test_data.py --commit
    DATABASE_URL=... python scripts/ingest_test_data.py --cleanup
"""

from __future__ import annotations

import argparse
import os
import sys

EXTERNAL_PREFIX = "p0harness:"
QUERY_MARKER = "P0HARNESS"
OUTSIDER_TOKEN = "OUTSIDER_ONLY_TOKEN"


def _company_domain() -> str:
    raw = os.environ.get("TEAMAGENT_SHARED_COMPANY_DOMAINS", "")
    dom = next((d.strip().lower() for d in raw.split(",") if d.strip()), "")
    if not dom:
        print("TEAMAGENT_SHARED_COMPANY_DOMAINS 未設定", file=sys.stderr)
        raise SystemExit(2)
    return dom


def _cleanup() -> int:
    import psycopg

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 未設定", file=sys.stderr)
        return 2
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM documents WHERE external_id LIKE %s", (EXTERNAL_PREFIX + "%",))
        print(f"deleted {cur.rowcount} fixture documents")
        conn.commit()
    return 0


def _commit() -> int:
    from teamagent.adapters.embeddings_client import LocalE5Embedder
    from teamagent.adapters.pgvector_client import PgVectorClient
    from teamagent.ingest.repository import ChunkUpsert, DocumentUpsert, IngestRepository

    dom = _company_domain()
    repo = IngestRepository(
        pgvector=PgVectorClient.from_env(), app_role=None, owner_email=f"ingest@{dom}"
    )
    embedder = LocalE5Embedder()

    # (key, owner_email, acl_emails, acl_groups, text)
    fixtures = [
        (
            "company1",
            f"u1@{dom}",
            [f"u1@{dom}"],
            [dom],
            f"{QUERY_MARKER} 会社ナレッジ doc（user1 担当client）",
        ),
        (
            "company2",
            f"u2@{dom}",
            [f"u2@{dom}"],
            [dom],
            f"{QUERY_MARKER} 会社ナレッジ doc（user2 担当client）",
        ),
        (
            "outsider",
            "outsider@evil.com",
            ["outsider@evil.com"],
            [],
            f"{QUERY_MARKER} {OUTSIDER_TOKEN} 会社外 doc（会社メンバーには不可視であるべき）",
        ),
    ]
    for key, owner, acl_emails, acl_groups, text in fixtures:
        doc = DocumentUpsert(
            source_type="slack",
            external_id=f"{EXTERNAL_PREFIX}{key}",
            source_uri=f"p0harness://{key}",
            title=f"P0 fixture {key}",
            owner_email=owner,
            acl_emails=acl_emails,
            acl_groups=acl_groups,
            metadata={"p0_fixture": True},
            modified_at=None,
        )
        repo.upsert_document_with_chunks(
            doc,
            [ChunkUpsert(chunk_idx=0, content=text, embedding=embedder.embed(text), metadata={})],
            request_id="p0harness",
        )
        print(f"upserted {key}: owner={owner} acl_groups={acl_groups}")
    print(
        "\n次: TEAMAGENT_MCP_BEARER=... python scripts/attack_mcp.py "
        f"--query {QUERY_MARKER} --outsider-needle {OUTSIDER_TOKEN}"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="P0 敵対ハーネス用 実DB fixture 投入/削除")
    ap.add_argument("--commit", action="store_true", help="fixture を投入（要 embedder/DB）")
    ap.add_argument("--cleanup", action="store_true", help="fixture を削除")
    args = ap.parse_args()
    if args.cleanup:
        return _cleanup()
    if args.commit:
        return _commit()
    print("--commit か --cleanup を指定（既定は何もしない＝安全）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
