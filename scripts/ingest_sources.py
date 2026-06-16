"""ingest_sources.yaml に基づく取り込み CLI（Sprint 3 PR-6）。

Usage:
    # 既定は --dry-run（DB 投入しない、件数集計だけ）
    python scripts/ingest_sources.py

    # 特定 source kind だけ
    python scripts/ingest_sources.py --sources slack
    python scripts/ingest_sources.py --sources slack,gsheets

    # 実投入（本番 RDS に commit）
    python scripts/ingest_sources.py --commit --sources slack \
        --owner-email shogo@vectorinc.co.jp

前提:
    - DATABASE_URL 設定済（load_secrets.sh 経由推奨）
    - SLACK_BOT_TOKEN 設定済（slack 取り込み時）
    - GCP credentials 設定済（gdrive/gsheets 取り込み時、Sprint 3 では NotImplementedError）

Exit code:
    0: 成功
    1: ingest 中に 1 件以上のエラー
    2: 設定エラー（DATABASE_URL 未設定 / yaml 構文エラー / 必須 env 不在）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import structlog  # noqa: E402

logger = structlog.get_logger(__name__)

DEFAULT_YAML = PROJECT_ROOT / "data" / "ingest_sources.yaml"


def main() -> int:
    # 構造化ログの出力形式を確定（STRUCTLOG_FORMAT=json で CloudWatch 向け JSON）。
    from teamagent.observability.logging_config import configure_logging

    configure_logging()

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--yaml", default=str(DEFAULT_YAML), help="ingest_sources.yaml のパス")
    p.add_argument(
        "--sources",
        default="all",
        help="取り込み対象（カンマ区切り）: slack / gdrive / gsheets / all",
    )
    p.add_argument(
        "--commit",
        action="store_true",
        help="既定は --dry-run。--commit を渡したときだけ DB に投入",
    )
    p.add_argument(
        "--owner-email",
        default=os.environ.get("INGEST_OWNER_EMAIL", "noreply@vectorinc.co.jp"),
        help="documents.owner_email として記録する email",
    )
    p.add_argument(
        "--app-role",
        default="teamagent_app",
        help="SET ROLE で切り替える Postgres role（None で SET ROLE しない）",
    )
    args = p.parse_args()

    # 設定読み込み
    from teamagent.adapters.embeddings_client import LocalE5Embedder
    from teamagent.adapters.pgvector_client import PgVectorClient
    from teamagent.ingest.loader import load_ingest_sources
    from teamagent.ingest.pipeline import IngestRunner
    from teamagent.ingest.repository import IngestRepository

    if not args.commit:
        logger.info("ingest_cli_mode", mode="DRY-RUN", hint="--commit で実投入")
    else:
        logger.warning("ingest_cli_mode", mode="COMMIT", hint="本番 RDS に書き込みます")

    try:
        sources = load_ingest_sources(Path(args.yaml))
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    if not os.environ.get("DATABASE_URL"):
        print(
            "[ERROR] DATABASE_URL が未設定。scripts/load_secrets.sh を source してください",
            file=sys.stderr,
        )
        return 2

    # adapter 初期化
    pgvector = PgVectorClient.from_env()
    repository = IngestRepository(
        pgvector=pgvector,
        app_role=args.app_role if args.app_role.lower() != "none" else None,
        owner_email=args.owner_email,
    )
    embedder = LocalE5Embedder()

    kinds_raw = args.sources.lower()
    if kinds_raw in ("", "all"):
        kinds = None
    else:
        kinds = [k.strip() for k in kinds_raw.split(",") if k.strip()]

    runner = IngestRunner(
        repository=repository,
        embedder=embedder,
        owner_email=args.owner_email,
        dry_run=not args.commit,
    )

    result = runner.run(sources, kinds=kinds)

    # 集計表示
    print("\n=== Ingest Result ===")
    for kind, stats in result.by_kind.items():
        print(
            f"  [{kind}] documents={stats.documents_upserted} "
            f"chunks={stats.chunks_inserted} "
            f"sources_processed={stats.sources_processed} "
            f"sources_skipped={stats.sources_skipped} "
            f"errors={len(stats.errors)}"
        )
        for err in stats.errors[:5]:
            print(f"    error: {err[:200]}")
    print(f"  TOTAL documents={result.total_documents()} errors={result.total_errors()}")
    if not args.commit:
        print("  [DRY-RUN] No DB writes. Use --commit to persist.")

    return 1 if result.total_errors() > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
