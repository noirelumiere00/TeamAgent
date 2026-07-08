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


# UPDATE 先の embedding 列の許可リスト（識別子を SQL に埋めるため固定値のみ・injection 防止）。
# embedding=e5（インプレース上書き＝rollback 不能）/ embedding_cohere=Bedrock Cohere の並行列
# （e5 列を残すので env を戻すだけで rollback 可能）。
_ALLOWED_TARGET_COLUMNS: frozenset[str] = frozenset({"embedding", "embedding_cohere"})

# 各 embedding 列の HNSW 索引名（migration の DDL と完全一致・_ALLOWED_TARGET_COLUMNS と 1:1）。
# embedding=0001 の chunks_embedding_hnsw_idx / embedding_cohere=0016 の同名 _cohere。
# 両者とも pgvector 既定パラメータ（WITH 句なし・opclass vector_cosine_ops）。
_HNSW_INDEX_BY_COLUMN: dict[str, str] = {
    "embedding": "chunks_embedding_hnsw_idx",
    "embedding_cohere": "chunks_embedding_cohere_hnsw_idx",
}


def _hnsw_index_ddl(target_column: str) -> tuple[str, str]:
    """(drop_sql, create_sql) を返す。既存 migration(0001/0016)の DDL と完全一致。

    ベクトル列に HNSW 索引がある状態で 1 行ずつ UPDATE すると毎行索引を保守して激遅
    （82K 件で事実上終わらない）ため、一括再 embed では索引を落として書き、完了後に
    作り直す（pgvector 一括ロードの定石）。CREATE 文に WITH 句は付けない（0013 の記録どおり
    m/ef_construction チューニング未実施＝既存定義と一致させるため）。target_column は
    固定許可リスト検証で識別子 injection を防ぐ。

    注意: embedding_cohere を NULL→4KB ベクトルに UPDATE すると行が肥大化して HOT 更新に
    ならず（新タプル生成）、**テーブル上の全 HNSW 索引が新タプルを指すよう再構築**される。
    つまり cohere 列の索引だけ落としても、残る e5 列 embedding の HNSW 索引が毎行 ~1s 保守されて
    遅い（実測で確定）。よって reembed 側は _HNSW_INDEX_BY_COLUMN の**全 HNSW 索引**を落として
    書き、完了後に全て作り直す（この関数は各列 1 本分の DDL を返す純関数のまま）。
    """
    if target_column not in _HNSW_INDEX_BY_COLUMN:
        raise ValueError(
            f"target_column は {sorted(_HNSW_INDEX_BY_COLUMN)} のいずれか (got {target_column!r})"
        )
    idx = _HNSW_INDEX_BY_COLUMN[target_column]
    drop_sql = f"DROP INDEX IF EXISTS {idx}"  # nosec B608
    create_sql = (  # nosec B608
        f"CREATE INDEX IF NOT EXISTS {idx} ON chunks USING hnsw ({target_column} vector_cosine_ops)"
    )
    return drop_sql, create_sql


def build_update_params(
    chunk_id: Any, embedding: list[float], target_column: str = "embedding"
) -> tuple[str, tuple[Any, ...]]:
    """1 chunk 分の UPDATE 文とパラメータを組み立てる（pgvector は list をそのまま渡す）。

    target_column は SQL 識別子として埋め込むため固定許可リスト
    （embedding / embedding_cohere）のみ受け付け、それ以外は ValueError（injection 防止）。
    既定 ``embedding``＝従来挙動（e5 インプレース上書き）。Cohere 並行列に書くときは
    ``embedding_cohere`` を渡す（e5 列を残し rollback 可能にする）。
    """
    if target_column not in _ALLOWED_TARGET_COLUMNS:
        raise ValueError(
            f"target_column は {sorted(_ALLOWED_TARGET_COLUMNS)} のいずれか (got {target_column!r})"
        )
    sql = f"UPDATE chunks SET {target_column} = %s WHERE id = %s"  # nosec B608
    return sql, (embedding, chunk_id)


# -----------------------------------------------------------
# DB 実行部
# -----------------------------------------------------------
def _run_index_ddl(dsn: str, target_column: str, *, action: str) -> None:
    """対象列の HNSW 索引を DROP または CREATE する（別の使い捨て autocommit 接続で実行）。

    書き込み用の通常接続とは分離する（DDL を通常トランザクションに混ぜない）。
    非 CONCURRENTLY: embedding_cohere は本番検索未使用（EMBEDDING_COLUMN 既定 embedding）なので
    短時間の ACCESS EXCLUSIVE を許容し、CONCURRENTLY より速く確実。先例 migrate_to_prod_rds.py。
    """
    import psycopg

    drop_sql, create_sql = _hnsw_index_ddl(target_column)
    sql = drop_sql if action == "drop" else create_sql
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    logger.info("reembed_index_ddl", action=action, target_column=target_column)


def reembed(
    *,
    dsn: str,
    embedder: Any,
    batch_size: int,
    commit: bool,
    limit: int | None = None,
    target_column: str = "embedding",
    only_missing: bool = False,
    keep_index: bool = False,
) -> dict[str, int]:
    """chunks を再 embed する。戻り値: 集計 dict。

    取り込み時ロジック（contextualize: contextualized=prefix+content を embed_passage）と
    一致させるため、``COALESCE(contextualized, content)`` を embed する（contextualized が
    あればそれを優先）。target_column は build_update_params が許可リスト検証する
    （既定 embedding＝従来挙動）。

    only_missing=True で ``target_column IS NULL`` の行だけを対象にする（中断からの再開用。
    2026-07-06: 8万chunk規模の再embedを途中再開できず最初からやり直す事故を防ぐ）。

    embedder が embed_passage_batch を持つ場合（BedrockCohereEmbedder）はバッチAPIで
    まとめて埋め込む（1件ずつの逐次呼び出しだと 8万件で10時間超・バッチなら数十分）。

    keep_index=False（既定）かつ commit のとき、対象列の HNSW 索引を書き込み前に DROP し
    完了後（例外時も finally で）CREATE で作り直す。ベクトル列に索引がある状態で 1 行ずつ
    UPDATE すると毎行索引を保守して激遅（8万件で事実上終わらない）ため。keep_index=True で
    従来どおり索引を触らない（小件数追い足しや本番使用列 embedding 向け）。
    """
    if target_column not in _ALLOWED_TARGET_COLUMNS:
        raise ValueError(
            f"target_column は {sorted(_ALLOWED_TARGET_COLUMNS)} のいずれか (got {target_column!r})"
        )
    import psycopg
    from pgvector.psycopg import register_vector

    stats = {"scanned": 0, "updated": 0}
    batch_fn = getattr(embedder, "embed_passage_batch", None)
    # 一括再 embed の間だけ HNSW 索引を落とす（毎行の索引保守で激遅になる罠の回避）。
    # dry-run（commit 無し）では索引を触らない。--keep-index で従来どおり触らない。
    # 非 HOT 更新はテーブル上の全 HNSW 索引を保守するため、target_column の索引だけでなく
    # chunks の全 HNSW 索引（e5・cohere 両方）を落として書き、完了後に全て作り直す。
    manage_index = commit and not keep_index
    managed_cols = sorted(_HNSW_INDEX_BY_COLUMN) if manage_index else []
    with psycopg.connect(dsn) as conn:
        # 【ハング根治・必須】この生接続に vector 型アダプタを登録する。未登録だと list[float] が
        # float8[] としてバインドされ、pgvector に float8[]→vector の暗黙キャストが無いため、
        # psycopg が prepare/pipeline 化した時点で沈黙ハングする（executemany・per-row 反復とも）。
        # pgvector_client._connect_pg は全接続で register_vector を呼ぶが、reembed は独自接続で
        # 取りこぼしていた（本ハングの根本原因・調査で確定）。
        register_vector(conn)
        for col in managed_cols:
            _run_index_ddl(dsn, col, action="drop")
        try:
            with conn.cursor() as cur:
                # 取り込み時は contextualized（prefix+content）を embed_passage する。再 embed も
                # 同じソースを使い検索/取り込みのサブ空間を一致させる（contextualized 無しは content）。
                q = "SELECT id, COALESCE(contextualized, content) AS src FROM chunks"
                if only_missing:
                    # target_column は許可リスト検証済みの固定識別子（injection 不能）。
                    q += f" WHERE {target_column} IS NULL"  # nosec B608
                q += " ORDER BY id"
                if limit:
                    q += f" LIMIT {int(limit)}"
                cur.execute(q)
                rows = cur.fetchall()

            logger.info(
                "reembed_start",
                total=len(rows),
                batch_size=batch_size,
                commit=commit,
                target_column=target_column,
                only_missing=only_missing,
                batched_api=bool(batch_fn),
                manage_index=manage_index,
            )
            for batch in batched(rows, batch_size):
                if batch_fn is not None:
                    # バッチAPI: DBバッチ単位でまとめて埋め込み（96件/コールは embedder 側が分割）。
                    srcs = [src or "" for _, src in batch]
                    vecs = batch_fn(srcs)
                    pairs = list(zip((cid for cid, _ in batch), vecs, strict=True))
                else:
                    pairs = [(cid, embedder.embed_passage(src or "")) for cid, src in batch]
                stats["scanned"] += len(batch)
                if commit:
                    # per-row UPDATE。索引を落としてあるので毎行の索引保守が無く、VPC内なら高速。
                    # 注記: psycopg3 の executemany を pgvector パラメータで使うと（vector 型の
                    # bulk バインドで）ハングする事象を実測したため採用しない。
                    with conn.cursor() as cur:
                        for chunk_id, vec in pairs:
                            sql, params = build_update_params(chunk_id, vec, target_column)
                            cur.execute(sql, params)
                    stats["updated"] += len(pairs)
                    conn.commit()
                logger.info(
                    "reembed_batch_done", scanned=stats["scanned"], updated=stats["updated"]
                )
        finally:
            # 例外時も落とした全索引を作り直す（落としたまま放置しない）。IF NOT EXISTS で冪等。
            # SIGKILL 等で finally が動かず索引欠落しても、再実行で復旧（本番使用の e5 索引が
            # 一時欠落すると検索は seq scan で遅くなるが動作はする＝夜間の一括作業として許容）。
            for col in managed_cols:
                _run_index_ddl(dsn, col, action="create")
    logger.info("reembed_done", **stats)
    return stats


def main() -> int:
    from teamagent.observability.logging_config import configure_logging

    configure_logging()

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--batch-size", type=int, default=200)
    p.add_argument("--limit", type=int, default=None, help="先頭 N 件のみ（検証用）")
    p.add_argument("--commit", action="store_true", help="既定 dry-run。指定時のみ UPDATE")
    p.add_argument(
        "--only-missing",
        action="store_true",
        help="target_column が NULL の行だけ対象（中断からの再開用）",
    )
    p.add_argument(
        "--keep-index",
        action="store_true",
        help=(
            "対象列の HNSW 索引を落とさず従来どおり実行（既定 False=索引を DROP→再embed→CREATE で"
            "作り直し高速化）。小件数の追い足し（--only-missing で数百件）は索引再ビルドが逆に高コスト、"
            "また --target-column embedding（e5＝本番使用列）は本番検索の索引を落とさないため、"
            "いずれも --keep-index 推奨。"
        ),
    )
    p.add_argument(
        "--target-column",
        default="embedding",
        choices=sorted(_ALLOWED_TARGET_COLUMNS),
        help=(
            "書き込む embedding 列。embedding=e5 インプレース上書き（既定・rollback 不能）/ "
            "embedding_cohere=Bedrock Cohere 並行列（e5 を残し env 戻しで rollback 可能）。"
            "EMBEDDER_BACKEND と整合必須（cohere⇄embedding_cohere / local⇄embedding）。"
            "不整合は main() が起動時 fail-loud で停止する。"
        ),
    )
    args = p.parse_args()

    import os

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("[ERROR] DATABASE_URL 未設定（load_secrets.sh を source）", file=sys.stderr)
        return 2

    # EMBEDDER_BACKEND（既定 local）で local-e5 / Bedrock Cohere を切替（検索/取り込みと同一構築点）。
    # build_embedder_from_env は EMBEDDING_COLUMN との整合を検証するが、再 embed が実際に
    # 書き込むのは --target-column（別ノブ）。両者がズレると Cohere ベクトルを e5 列 embedding に
    # インプレース上書き（rollback 不能）する全壊が成立しうるため、検索側 fail-loud と同じ
    # 不変条件を「書込列」に対しても直接検証する（cohere⇄embedding_cohere / local⇄embedding）。
    from teamagent.adapters.embeddings_client import (
        build_embedder_from_env,
        resolve_embedder_backend,
        validate_embedder_column_pair,
    )

    try:
        validate_embedder_column_pair(resolve_embedder_backend(), args.target_column)
    except ValueError as e:
        print(f"[ERROR] backend と --target-column の不整合: {e}", file=sys.stderr)
        return 2

    try:
        stats = reembed(
            dsn=dsn,
            embedder=build_embedder_from_env(),
            batch_size=args.batch_size,
            commit=args.commit,
            limit=args.limit,
            target_column=args.target_column,
            only_missing=args.only_missing,
            keep_index=args.keep_index,
        )
    except Exception as e:
        print(f"[ERROR] reembed failed: {e}", file=sys.stderr)
        return 2
    print(
        f"scanned={stats['scanned']} updated={stats['updated']} "
        f"commit={args.commit} target_column={args.target_column}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
