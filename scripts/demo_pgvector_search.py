"""
TeamAgent — ローカル無料モデルで pgvector ベクトル検索を体験するデモ

何をするか:
  1. docs/v3.1/teamagent_subsidiary_questions_v2.md を読み込み
  2. ## と ### のヘッダーで20個の質問チャンクに分割
  3. multilingual-e5-large (1024次元・日本語OK) でベクトル化
  4. pgvector の demo_qa テーブルに INSERT
  5. 任意の質問文で cosine similarity 検索 → top3 取得

実行方法 (TeamAgent ルートから):
  python -m venv .venv
  source .venv/bin/activate
  pip install -U pip
  pip install sentence-transformers psycopg[binary] pgvector
  python scripts/demo_pgvector_search.py

前提:
  - docker compose up -d で teamagent-postgres が起動していること
  - localhost:5432 で接続できること
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import psycopg
from sentence_transformers import SentenceTransformer


# ---------- 設定 ----------
DOC_PATH = Path("docs/v3.1/teamagent_subsidiary_questions_v2.md")
DB_DSN = "host=localhost port=5432 user=teamagent password=teamagent dbname=teamagent"
MODEL_NAME = "intfloat/multilingual-e5-large"  # 1024次元・日本語対応
TABLE_NAME = "demo_qa"
EMBED_DIM = 1024


# ---------- 1. Markdown を読んでチャンク化 ----------
def load_chunks(path: Path) -> list[dict]:
    """### Q1. xxx \n - body... の単位で1チャンク"""
    if not path.exists():
        print(f"❌ ファイルが見つかりません: {path}")
        print(f"  TeamAgent リポジトリのルートから実行してください")
        sys.exit(1)

    text = path.read_text(encoding="utf-8")

    # ### で始まる行で分割
    chunks: list[dict] = []
    current_title: str | None = None
    current_body: list[str] = []

    for line in text.splitlines():
        if line.startswith("### "):
            # 直前のチャンクを保存
            if current_title:
                chunks.append({
                    "title": current_title,
                    "body": "\n".join(current_body).strip(),
                })
            current_title = line[4:].strip()
            current_body = []
        elif current_title:
            current_body.append(line)

    # 最後のチャンク
    if current_title:
        chunks.append({
            "title": current_title,
            "body": "\n".join(current_body).strip(),
        })

    print(f"📄 {path.name} から {len(chunks)} チャンク抽出")
    return chunks


# ---------- 2. embedding モデルをロード ----------
def load_model(name: str) -> SentenceTransformer:
    print(f"🤖 モデルをロード中: {name}")
    print(f"  (初回は ~560MB ダウンロード、2回目以降はキャッシュから瞬時)")
    model = SentenceTransformer(name)
    dim = model.get_sentence_embedding_dimension()
    print(f"  次元数: {dim}")
    assert dim == EMBED_DIM, f"想定 {EMBED_DIM}, 実際 {dim}"
    return model


# ---------- 3. pgvector テーブルを準備 ----------
def setup_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT,
                embedding VECTOR({EMBED_DIM})
            );
        """)
        cur.execute(f"TRUNCATE {TABLE_NAME} RESTART IDENTITY;")
        # HNSW index (cosine similarity 用)
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_embedding_idx
            ON {TABLE_NAME}
            USING hnsw (embedding vector_cosine_ops);
        """)
    conn.commit()
    print(f"💾 テーブル {TABLE_NAME} を初期化 (HNSW index付き)")


# ---------- 4. ベクトル化して INSERT ----------
def insert_chunks(
    conn: psycopg.Connection,
    model: SentenceTransformer,
    chunks: list[dict],
) -> None:
    print(f"\n=== INSERT {len(chunks)} 件 ===")
    with conn.cursor() as cur:
        for c in chunks:
            # e5 モデルは "passage:" prefix が推奨
            embed_text = f"passage: {c['title']}\n{c['body']}"
            vec = model.encode(embed_text, normalize_embeddings=True).tolist()
            cur.execute(
                f"INSERT INTO {TABLE_NAME} (title, body, embedding) "
                f"VALUES (%s, %s, %s)",
                (c["title"], c["body"], str(vec)),
            )
            print(f"  ✓ {c['title'][:50]}")
    conn.commit()


# ---------- 5. 類似検索 ----------
def search(
    conn: psycopg.Connection,
    model: SentenceTransformer,
    query: str,
    top_k: int = 3,
) -> None:
    print(f"\n🔍 検索: 「{query}」")
    qvec = model.encode(f"query: {query}", normalize_embeddings=True).tolist()
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT
              id, title, body,
              1 - (embedding <=> %s::vector) AS similarity
            FROM {TABLE_NAME}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """, (str(qvec), str(qvec), top_k))

        for i, (rid, title, body, sim) in enumerate(cur.fetchall(), 1):
            print(f"\n  [{i}] 類似度 {sim:.3f}  (id={rid})")
            print(f"      タイトル: {title}")
            preview = re.sub(r"\s+", " ", body)[:120]
            print(f"      本文先頭: {preview}...")


# ---------- main ----------
def main() -> None:
    chunks = load_chunks(DOC_PATH)
    model = load_model(MODEL_NAME)

    with psycopg.connect(DB_DSN) as conn:
        setup_table(conn)
        insert_chunks(conn, model, chunks)

        # 検索クエリ複数試す
        for q in [
            "ベテランの暗黙知をどうやって AI に取り込むか",
            "Slack の API レート制限で困った経験",
            "セキュリティ周りで早めにやっておけばよかったこと",
            "コスト最適化のテクニック",
        ]:
            search(conn, model, q, top_k=3)
            print("\n" + "─" * 60)

    print("\n✅ デモ完了！")


if __name__ == "__main__":
    main()
