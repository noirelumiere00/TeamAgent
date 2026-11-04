"""Register the two case aliases decided on 2026-11-04; existing decisions are preserved."""

from __future__ import annotations

import argparse
import uuid

import structlog

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.ingest.video_library_case_repository import CaseRepository
from teamagent.observability.logging_config import configure_logging

INITIAL_ALIASES = (
    ("TikTok", "20260717", "ラフォーレ", "セール"),
    ("トップバリュ", "昭和レトロ", "AEON", None),
    # 11/04 小俣さん: メディアで紹介した内容がアプリキャンペーン／ばりスゴはホークスとのコラボ企画の商品
    ("アップガレージ", "メディア", "アップガレージ", "アプリキャンペーン"),
    ("ローソン九州", "ばりスゴ", "ローソン九州", "ホークス応援"),
)


def register_initial_aliases(repository: CaseRepository, *, decided_by: str) -> int:
    if not decided_by.strip():
        raise ValueError("missing_decided_by")
    with repository.connection() as conn:
        return sum(
            conn.execute(
                """INSERT INTO vl_case_aliases
                (from_advertiser,from_campaign,to_advertiser,to_campaign,decided_by)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (from_advertiser,from_campaign) DO NOTHING""",
                (*alias, decided_by),
            ).rowcount
            for alias in INITIAL_ALIASES
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decided-by", required=True)
    args = parser.parse_args(argv)
    configure_logging()
    logger = structlog.get_logger(__name__)
    request_id = uuid.uuid4().hex
    try:
        count = register_initial_aliases(
            CaseRepository(PgVectorClient.from_env()), decided_by=args.decided_by
        )
        logger.info("case_aliases_registered", request_id=request_id, count=count)
        return 0
    except Exception as exc:
        logger.error("case_aliases_failed", request_id=request_id, error_code=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
