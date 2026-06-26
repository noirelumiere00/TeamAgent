"""chunks の embedding を再計算する一括スクリプト（spec matrix ingest#24）。

Embedder のモデル差替（e5→Titan 等）や次元/前処理変更の後、既存 chunks を
新 embedder で再 embed して `chunks.embedding` を UPDATE する。

設計:
- 既定 **dry-run**（--commit で初めて UPDATE）。
- バッチ処理（--batch-size 単位で SELECT→embed→UPDATE）でメモリ/トランザクションを制御。
- 進捗は構造化ログ。本文は出さない（CLAUDE.md 6-bis・text_len のみ）。
- DB は DATABASE_URL（load_secrets.sh が組み立て）。embedder は LocalE5Embedder。

QW-1（e5 passage プレフィックス）の本番反映はこのスクリプトがゲート:
- 平常時は env を立てず（USE_E5_PASSAGE_PREFIX 未設定）embed_passage() も "query: " を
  付与する＝既存コーパスと同一サブ空間（後方互換）。
- passage 化するときは **USE_E5_PASSAGE_PREFIX=1 を立てて本スクリプトで全 chunks を
  --commit 再 embed** し、コーパス全体を "passage: " に切替える（検索側 embed() は常に
  "query: "）。中途半端に一部だけ切ると空間不整合になるため、必ず全走させること。

Usage:
    set -a; source .env.production; set +a; source scripts/load_secrets.sh
    python scripts/reembed_chunks.py --dry-run         # 件数だけ
    # passage 化する本番反映時のみ:
    USE_E5_PASSAGE_PREFIX=1 python scripts/reembed_chunks.py --commit --batch-size 200
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

import structlog  # noqa: E402

logger = structlog.get_logger(__name__)


# -----------------------------------------------------------
# 純関数（DB/embedder 非依存・テスト対象）
# -----------------------------------------------------------
def batched(rows: Sequence[Any], size: int) -> Iterator[list[Any]]:
    """rows を size 件ずつのバッチに分割する。size<=0 は ValueError。"""
    if size <= 0:
        raise ValueError("batch size must be positive")
    for i in range(0, len(rows), size):
        yield list(rows[i : i + size])


def build_update_params(chunk_id: Any, embedding: list[float]) -> tuple[str, tuple[Any, ...]]:
    """1 chunk 分の UPDATE 文とパラメータを組み立てる（pgvector は list をそのまま渡す）。"""
    sql = "UPDATE chunks SET embedding = %s WHERE id = %s"
    return sql, (embedding, chunk_id)


# -----------------------------------------------------------
# DB 実行部
# -----------------------------------------------------------
def reembed(
    *,
    dsn: str,
    embedder: Any,
    batch_size: int,
    commit: bool,
    limit: int | None = None,
) -> dict[str, int]:
    """chunks を再 embed する。戻り値: 集計 dict。"""
    import psycopg

    stats = {"scanned": 0, "updated": 0}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            q = "SELECT id, content FROM chunks ORDER BY id"
            if limit:
                q += f" LIMIT {int(limit)}"
            cur.execute(q)
            rows = cur.fetchall()

        logger.info("reembed_start", total=len(rows), batch_size=batch_size, commit=commit)
        for batch in batched(rows, batch_size):
            for chunk_id, content in batch:
                stats["scanned"] += 1
                vec = embedder.embed_passage(content or "")
                if commit:
                    sql, params = build_update_params(chunk_id, vec)
                    with conn.cursor() as cur:
                        cur.execute(sql, params)
                    stats["updated"] += 1
            if commit:
                conn.commit()
            logger.info("reembed_batch_done", scanned=stats["scanned"], updated=stats["updated"])
    logger.info("reembed_done", **stats)
    return stats


def main() -> int:
    from teamagent.observability.logging_config import configure_logging

    configure_logging()

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--limit", type=int, default=None, help="先頭 N 件のみ（検証用）")
    p.add_argument("--commit", action="store_true", help="既定 dry-run。指定時のみ UPDATE")
    args = p.parse_args()

    import os

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("[ERROR] DATABASE_URL 未設定（load_secrets.sh を source）", file=sys.stderr)
        return 2

    from teamagent.adapters.embeddings_client import LocalE5Embedder

    try:
        stats = reembed(
            dsn=dsn,
            embedder=LocalE5Embedder(),
            batch_size=args.batch_size,
            commit=args.commit,
            limit=args.limit,
        )
    except Exception as e:
        print(f"[ERROR] reembed failed: {e}", file=sys.stderr)
        return 2
    print(f"scanned={stats['scanned']} updated={stats['updated']} commit={args.commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
