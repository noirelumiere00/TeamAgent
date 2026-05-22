"""通常検索 vs Contextual Retrieval の精度比較スクリプト。

同じクエリを両テーブルで実行し、top-5 ヒットの similarity score を並べる。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import psycopg  # noqa: E402
from pgvector.psycopg import register_vector  # type: ignore[import-untyped]  # noqa: E402

from teamagent.adapters.embeddings_client import LocalE5Embedder  # noqa: E402

DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://teamagent:teamagent@localhost:5432/teamagent",
)

QUERIES = [
    "PR代行の業界別実績は？",
    "INPEX案件の提案内容を教えて",
    "ショート動画のアルゴリズムについて",
    "Z世代のメディア利用変化",
    "ベクトル社のサービス概要",
]


def search(
    conn: psycopg.Connection,
    query: str,
    embedder: LocalE5Embedder,
    table: str,
    content_col: str,
    limit: int = 5,
) -> list[tuple[int, float, str, str]]:
    """ベクトル検索を実行し、(chunk_id, score, file_name, content_head) を返す。"""
    qvec = embedder.embed(query)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, 1 - (embedding <=> %s::vector) AS score, file_name, {content_col}
            FROM {table}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (qvec, qvec, limit),
        )
        rows = cur.fetchall()
    return [(r[0], float(r[1]), r[2], r[3][:60].replace("\n", " ")) for r in rows]


def main() -> int:
    print("📊 通常 vs Contextual Retrieval 精度比較")
    print(f"  DB: {DB_DSN}")
    embedder = LocalE5Embedder()
    conn = psycopg.connect(DB_DSN)
    register_vector(conn)

    try:
        for i, query in enumerate(QUERIES, 1):
            print(f"\n{'=' * 80}")
            print(f"Q{i}: {query}")
            print("=" * 80)

            normal = search(conn, query, embedder, "proposals_chunks", "text")
            ctx = search(
                conn,
                query,
                embedder,
                "proposals_chunks_contextual",
                "contextualized_text",
            )

            print(f"\n--- 通常検索（proposals_chunks.text）---")
            for j, (cid, score, fname, head) in enumerate(normal, 1):
                print(f"  #{j} score={score:.4f} | {fname[:25]:<25} | {head}")

            print(f"\n--- Contextual（proposals_chunks_contextual.contextualized_text）---")
            for j, (cid, score, fname, head) in enumerate(ctx, 1):
                print(f"  #{j} score={score:.4f} | {fname[:25]:<25} | {head}")

            top_normal = normal[0][1] if normal else 0
            top_ctx = ctx[0][1] if ctx else 0
            diff = (top_ctx - top_normal) * 100
            print(f"\n  📈 top-1 score 差分: {diff:+.2f} ポイント")
    finally:
        conn.close()

    print("\n🎉 比較完了")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
