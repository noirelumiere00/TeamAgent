"""TeamAgent — Contextual Retrieval（Anthropic 2024 提案手法）

各 chunk に対して、その chunk が属する PDF 全文を Haiku 4.5 に渡し、
「この chunk が文書のどの位置・トピックか」を 50-100 文字で要約。
要約 + 元 chunk を結合して再 embedding し、proposals_chunks_contextual に保存。

Bedrock Prompt Caching を使ってドキュメント全文の input cost を 1/10 に削減。

参考: https://www.anthropic.com/news/contextual-retrieval

Usage:
    python scripts/contextual_retrieval.py

前提:
    - ローカル pgvector が起動済み（docker compose up -d）
    - proposals_chunks に既にデータが入っている
    - AWS_REGION / BEDROCK_HAIKU_MODEL_ID 等が設定済み
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

# プロジェクトルートを sys.path に追加（src/ をインポートできるように）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import psycopg  # noqa: E402
from pgvector.psycopg import register_vector  # type: ignore[import-untyped]  # noqa: E402

# import 順序：sys.path 後に teamagent パッケージを import する必要があるため
from teamagent.adapters.bedrock_client import BedrockClient  # noqa: E402

# ---------- 設定 ----------
DB_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://teamagent:teamagent@localhost:5432/teamagent",
)
SOURCE_TABLE = "proposals_chunks"
TARGET_TABLE = "proposals_chunks_contextual"
HAIKU_MODEL_ID = os.environ.get(
    "BEDROCK_HAIKU_MODEL_ID",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)
EMBED_DIM = 1024

# Haiku に渡すプロンプト（公式 Contextual Retrieval プロンプト準拠）
CONTEXTUALIZE_INSTRUCTION = (
    "<document>\n{document}\n</document>\n"
    "Here is the chunk we want to situate within the whole document:\n"
    "<chunk>\n{chunk}\n</chunk>\n"
    "Please give a short succinct context (50-100 Japanese characters) "
    "to situate this chunk within the overall document for the purposes of "
    "improving search retrieval of the chunk. Answer only with the succinct "
    "context and nothing else. Respond in Japanese."
)


def setup_target_table(conn: psycopg.Connection[Any], truncate: bool = False) -> None:
    """proposals_chunks_contextual テーブルを初期化する。

    truncate=True で全削除、False（デフォルト）で既存データ温存（ON CONFLICT skip）。
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
                id SERIAL PRIMARY KEY,
                source_chunk_id INT NOT NULL,
                file_name TEXT NOT NULL,
                page_num INT NOT NULL,
                chunk_idx INT NOT NULL,
                original_text TEXT NOT NULL,
                context_prefix TEXT NOT NULL,
                contextualized_text TEXT NOT NULL,
                embedding VECTOR({EMBED_DIM}),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (file_name, page_num, chunk_idx)
            );
            """
        )
        if truncate:
            cur.execute(f"TRUNCATE {TARGET_TABLE} RESTART IDENTITY;")
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {TARGET_TABLE}_embedding_idx
            ON {TARGET_TABLE}
            USING hnsw (embedding vector_cosine_ops);
            """
        )
    conn.commit()
    print(f"💾 テーブル {TARGET_TABLE} を {'初期化' if truncate else '準備（既存データ温存）'}")


def fetch_chunks_by_file(
    conn: psycopg.Connection[Any], skip_existing: bool = True
) -> dict[str, list[dict[str, Any]]]:
    """proposals_chunks を file_name 別にグループ化して返す。

    skip_existing=True なら proposals_chunks_contextual に既にある chunks を除外する。
    """
    with conn.cursor() as cur:
        if skip_existing:
            cur.execute(
                f"""
                SELECT s.id, s.file_name, s.page_num, s.chunk_idx, s.text
                FROM {SOURCE_TABLE} s
                LEFT JOIN {TARGET_TABLE} t
                  ON s.file_name = t.file_name
                  AND s.page_num = t.page_num
                  AND s.chunk_idx = t.chunk_idx
                WHERE t.id IS NULL
                ORDER BY s.file_name, s.page_num, s.chunk_idx
                """
            )
        else:
            cur.execute(
                f"""
                SELECT id, file_name, page_num, chunk_idx, text
                FROM {SOURCE_TABLE}
                ORDER BY file_name, page_num, chunk_idx
                """
            )
        rows = cur.fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r[1], []).append(
            {"id": r[0], "page_num": r[2], "chunk_idx": r[3], "text": r[4]}
        )
    return grouped


def fetch_document_text(conn: psycopg.Connection[Any], file_name: str) -> str:
    """ある file_name の全 chunks を結合して document_text として返す。

    fetch_chunks_by_file が skip_existing=True で動いている場合に、未処理 chunks
    だけが返って document_text を作るのに足りない可能性があるので、別途取得する。
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT text
            FROM {SOURCE_TABLE}
            WHERE file_name = %s
            ORDER BY page_num, chunk_idx
            """,
            (file_name,),
        )
        rows = cur.fetchall()
    return "\n\n".join(r[0] for r in rows)


def contextualize_chunk(
    bedrock: BedrockClient,
    document_text: str,
    chunk_text: str,
    request_id: str,
) -> tuple[str, int, int, int, float]:
    """Haiku に「この chunk の context」を生成させる。

    cachePoint を使ってドキュメント全文を cache 化し、コスト削減。

    Returns:
        (context_prefix, input_tokens, output_tokens, cache_read_tokens, cost_usd)
    """
    prompt = CONTEXTUALIZE_INSTRUCTION.format(
        document=document_text,
        chunk=chunk_text,
    )
    resp = bedrock._client.converse(
        modelId=bedrock.model_id,
        messages=[
            {
                "role": "user",
                "content": [
                    {"text": prompt},
                    {"cachePoint": {"type": "default"}},
                ],
            }
        ],
        inferenceConfig={"temperature": 0.1, "maxTokens": 200},
    )
    usage = resp.get("usage", {})
    input_tokens = int(usage.get("inputTokens", 0))
    output_tokens = int(usage.get("outputTokens", 0))
    cache_read = int(usage.get("cacheReadInputTokens", 0))
    cache_write = int(usage.get("cacheWriteInputTokens", 0))

    # ざっくりコスト計算（Haiku 4.5: $1/$5 per Mtok、cache: $0.1/$1.25）
    cost = (
        (input_tokens - cache_read - cache_write) / 1_000_000 * 1.0
        + cache_read / 1_000_000 * 0.1
        + cache_write / 1_000_000 * 1.25
        + output_tokens / 1_000_000 * 5.0
    )

    # 応答テキスト抽出
    text = ""
    for block in resp["output"]["message"]["content"]:
        if "text" in block:
            text = str(block["text"]).strip()
            break

    return text, input_tokens, output_tokens, cache_read, cost


def main() -> int:
    print("🚀 Contextual Retrieval バッチ開始")
    print(f"  source: {SOURCE_TABLE}")
    print(f"  target: {TARGET_TABLE}")
    print(f"  model: {HAIKU_MODEL_ID}")

    # 1. embedder ロード（遅延 import で時間計測しやすく）
    print("📥 embedder ロード中...")
    from teamagent.adapters.embeddings_client import LocalE5Embedder

    embedder = LocalE5Embedder()

    # 2. Bedrock client（Haiku 4.5 固定）
    bedrock = BedrockClient(
        region=os.environ.get("AWS_REGION", "us-east-1"),
        model_id=HAIKU_MODEL_ID,
    )

    # 3. DB 接続
    conn = psycopg.connect(DB_DSN)
    register_vector(conn)

    try:
        # 既存データ温存（再開可能化）
        setup_target_table(conn, truncate=False)
        grouped = fetch_chunks_by_file(conn, skip_existing=True)

        total_chunks = sum(len(v) for v in grouped.values())
        print(f"📊 対象（未処理のみ）: {len(grouped)} files / {total_chunks} chunks")
        if total_chunks == 0:
            print("✅ すべての chunks が既に処理済み。スキップ。")
            return 0

        total_cost = 0.0
        total_input = 0
        total_output = 0
        total_cache_read = 0
        processed = 0
        start_time = time.perf_counter()

        for file_name, chunks in grouped.items():
            # ドキュメント全文は SOURCE_TABLE から取り直す（未処理 chunks だけだと不完全なため）
            document_text = fetch_document_text(conn, file_name)
            doc_len = len(document_text)
            print(f"\n📄 {file_name}: {len(chunks)} 未処理 chunks, doc len={doc_len}")

            for chunk in chunks:
                req_id = f"ctx-{file_name[:10]}-{chunk['chunk_idx']:03d}"
                try:
                    context_prefix, in_tok, out_tok, cache_read, cost = contextualize_chunk(
                        bedrock,
                        document_text,
                        chunk["text"],
                        req_id,
                    )
                except Exception as e:
                    print(f"  ❌ chunk {chunk['id']} failed: {e}")
                    continue

                # 要約 + 元 chunk を結合して embedding
                contextualized = f"{context_prefix}\n\n{chunk['text']}"
                embedding = embedder.embed_passage(contextualized)

                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO {TARGET_TABLE}
                            (source_chunk_id, file_name, page_num, chunk_idx,
                             original_text, context_prefix, contextualized_text, embedding)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (file_name, page_num, chunk_idx) DO NOTHING
                        """,
                        (
                            chunk["id"],
                            file_name,
                            chunk["page_num"],
                            chunk["chunk_idx"],
                            chunk["text"],
                            context_prefix,
                            contextualized,
                            embedding,
                        ),
                    )
                conn.commit()

                total_cost += cost
                total_input += in_tok
                total_output += out_tok
                total_cache_read += cache_read
                processed += 1
                if processed % 10 == 0:
                    elapsed = time.perf_counter() - start_time
                    rate = processed / elapsed
                    print(
                        f"  ✅ {processed}/{total_chunks} done "
                        f"({rate:.1f} chunks/s, cost=${total_cost:.4f})"
                    )

        elapsed = time.perf_counter() - start_time
        print(f"\n🎉 完了: {processed}/{total_chunks} chunks 処理")
        print(f"  実行時間: {elapsed:.1f} 秒")
        print(f"  総コスト: ${total_cost:.4f}")
        print(f"  input tokens: {total_input:,}（うち cache read: {total_cache_read:,}）")
        print(f"  output tokens: {total_output:,}")
        if processed > 0:
            print(f"  平均コスト: ${total_cost / processed:.5f} / chunk")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
