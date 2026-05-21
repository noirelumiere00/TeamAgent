"""
TeamAgent — 実資料 (PDF) をベクトル化＆検索するデモ

特徴:
  - data/proposals/ 配下の PDF を全件処理
  - pdfplumber でページ単位にテキスト抽出
  - チャンク分割: ~500文字 + 100文字オーバーラップ (句読点境界尊重)
  - multilingual-e5-large (1024次元・日本語) でベクトル化
  - proposals_chunks テーブルに INSERT (ファイル名・ページ番号付き)
  - 対話検索モード (Ctrl+C で終了)

実行:
  source .venv/bin/activate
  pip install pdfplumber
  mkdir -p data/proposals
  # ここで data/proposals/ に PDF を3つ置く
  python scripts/demo_pdf_vectorize.py

注意:
  - data/ は .gitignore で除外推奨 (機密資料を git に push しないため)
  - 初回は multilingual-e5-large の 2.24GB ダウンロード
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import psycopg
from sentence_transformers import SentenceTransformer

try:
    import pdfplumber
except ImportError:
    print("❌ pdfplumber 未インストール")
    print("  → pip install pdfplumber を実行してください")
    sys.exit(1)


# ---------- 設定 ----------
DATA_DIR = Path("data/proposals")
DB_DSN = "host=localhost port=5432 user=teamagent password=teamagent dbname=teamagent"
MODEL_NAME = "intfloat/multilingual-e5-large"
TABLE_NAME = "proposals_chunks"
EMBED_DIM = 1024
CHUNK_SIZE = 500
CHUNK_OVERLAP = 100


# ---------- 1. PDF からテキスト抽出 ----------
def extract_pdf_pages(pdf_path: Path) -> list[tuple[int, str]]:
    """戻り値: [(ページ番号 1-indexed, テキスト), ...]"""
    pages: list[tuple[int, str]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            # 連続空白を1スペースに圧縮
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                pages.append((i, text))
    return pages


# ---------- 2. テキストをチャンクに分割 ----------
def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """文字数ベース + 句読点境界尊重"""
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        # 末尾を句読点まで戻す (最低でも size/2 は使う)
        if end < len(text):
            for punct in ["。", "！", "？", "\n", "、"]:
                idx = text.rfind(punct, start, end)
                if idx > start + size // 2:
                    end = idx + 1
                    break
        chunks.append(text[start:end].strip())
        if end == len(text):
            break
        start = end - overlap
    return [c for c in chunks if c]


# ---------- 3. PDF 群を読み込んで chunk のリストを作る ----------
def collect_chunks(data_dir: Path) -> list[dict]:
    if not data_dir.exists():
        print(f"❌ {data_dir} がありません")
        print(f"  → mkdir -p {data_dir} して、PDF を配置してください")
        sys.exit(1)

    pdfs = sorted(data_dir.glob("*.pdf"))
    if not pdfs:
        print(f"❌ {data_dir} に PDF がありません")
        sys.exit(1)

    print(f"📄 {len(pdfs)} 個の PDF を発見:")
    for p in pdfs:
        print(f"  - {p.name}")

    all_chunks: list[dict] = []
    for pdf_path in pdfs:
        try:
            pages = extract_pdf_pages(pdf_path)
        except Exception as e:
            print(f"  ⚠ {pdf_path.name} の処理でエラー: {e}")
            continue
        print(f"\n  📖 {pdf_path.name}: {len(pages)} ページ")
        for page_num, page_text in pages:
            chunks = chunk_text(page_text)
            for idx, chunk in enumerate(chunks):
                all_chunks.append({
                    "file_name": pdf_path.name,
                    "page_num": page_num,
                    "chunk_idx": idx,
                    "text": chunk,
                })
    print(f"\n  → 合計 {len(all_chunks)} chunk 抽出完了")
    return all_chunks


# ---------- 4. モデルロード ----------
def load_model() -> SentenceTransformer:
    print(f"\n🤖 モデルをロード中: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME)
    dim = model.get_sentence_embedding_dimension()
    print(f"  次元数: {dim}")
    assert dim == EMBED_DIM, f"想定 {EMBED_DIM}, 実際 {dim}"
    return model


# ---------- 5. テーブル準備 ----------
def setup_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id SERIAL PRIMARY KEY,
                file_name TEXT NOT NULL,
                page_num INT NOT NULL,
                chunk_idx INT NOT NULL,
                text TEXT NOT NULL,
                embedding VECTOR({EMBED_DIM}),
                UNIQUE (file_name, page_num, chunk_idx)
            );
        """)
        cur.execute(f"TRUNCATE {TABLE_NAME} RESTART IDENTITY;")
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_embedding_idx
            ON {TABLE_NAME}
            USING hnsw (embedding vector_cosine_ops);
        """)
    conn.commit()
    print(f"💾 テーブル {TABLE_NAME} を初期化")


# ---------- 6. ベクトル化＆INSERT (バッチ) ----------
def insert_chunks(
    conn: psycopg.Connection,
    model: SentenceTransformer,
    chunks: list[dict],
) -> None:
    print(f"\n=== INSERT {len(chunks)} chunk ===")
    texts = [f"passage: {c['text']}" for c in chunks]
    print(f"  ベクトル化中... (バッチ8)")
    vecs = model.encode(
        texts,
        normalize_embeddings=True,
        batch_size=8,
        show_progress_bar=True,
    )

    with conn.cursor() as cur:
        for c, v in zip(chunks, vecs):
            cur.execute(
                f"INSERT INTO {TABLE_NAME} "
                f"(file_name, page_num, chunk_idx, text, embedding) "
                f"VALUES (%s, %s, %s, %s, %s)",
                (c["file_name"], c["page_num"], c["chunk_idx"], c["text"], str(v.tolist())),
            )
    conn.commit()
    print(f"  ✓ INSERT 完了")


# ---------- 7. 検索 ----------
def search(
    conn: psycopg.Connection,
    model: SentenceTransformer,
    query: str,
    top_k: int = 5,
) -> None:
    print(f"\n🔍 「{query}」")
    qvec = model.encode(f"query: {query}", normalize_embeddings=True).tolist()
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT
              file_name, page_num, chunk_idx, text,
              1 - (embedding <=> %s::vector) AS similarity
            FROM {TABLE_NAME}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """, (str(qvec), str(qvec), top_k))

        rows = cur.fetchall()
        if not rows:
            print("  → 結果なし")
            return
        for i, (fname, pnum, cidx, text, sim) in enumerate(rows, 1):
            print(f"\n  [{i}] 類似度 {sim:.3f}  📄 {fname}  p{pnum} (chunk {cidx})")
            preview = text[:200] + ("..." if len(text) > 200 else "")
            print(f"      {preview}")


# ---------- 対話モード ----------
def interactive_loop(conn: psycopg.Connection, model: SentenceTransformer) -> None:
    print("\n" + "=" * 70)
    print("🔍 対話検索モード (Ctrl+C or 空 Enter で終了)")
    print("=" * 70)
    try:
        while True:
            print()
            q = input("質問> ").strip()
            if not q:
                print("👋 終了します")
                break
            search(conn, model, q, top_k=5)
    except (KeyboardInterrupt, EOFError):
        print("\n\n👋 終了します")


# ---------- main ----------
def main() -> None:
    chunks = collect_chunks(DATA_DIR)
    model = load_model()

    with psycopg.connect(DB_DSN) as conn:
        setup_table(conn)
        insert_chunks(conn, model, chunks)
        interactive_loop(conn, model)


if __name__ == "__main__":
    main()
