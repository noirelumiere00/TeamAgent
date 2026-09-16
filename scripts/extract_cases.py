"""Vault（documents）から事例レコードを抽出して ``case_records`` へ入れる（設計 §3 の一括抽出）。

設計: Artifacts/aico-inputs-20260911/case_knowledge_design_20260916.md v2。
1 文書 → n 事例を Bedrock（Sonnet 想定・model_id は必ず明示）で抽出し、要約の埋め込みを
既存 embedder（``build_embedder_from_env``＝chunks と同じ次元・同じ backend）で付けて upsert する。

安全装置:
- ``--dry-run`` は抽出だけ行い **DB へ書かない**（件数と例を表示・``--out`` で JSON 保存可）。
- 文書の読み出しは ``PgVectorClient.list_documents_with_text``（repository 層）だけ。
  このスクリプトに生 SQL は書かない。
- 対象は ``--doc-types``（既定 提案書,報告書,施策実績）・``--since``・``--limit`` で必ず絞る。
- model_id は ``--model-id`` / ``CASE_EXTRACT_MODEL_ID`` / ``BEDROCK_MODEL_ID`` の順。未設定なら
  起動しない（暗黙の Haiku 落ち・暗黙の Sonnet 課金を避ける）。

Usage:
    # 件数と例だけ（書かない・Bedrock は呼ぶ＝課金あり）
    python scripts/extract_cases.py --limit 5 --dry-run --out /tmp/cases.json
    # 本番書き込み（人間ゲート・裁定後）
    python scripts/extract_cases.py --limit 200 --since 2025-01-01 --doc-types 提案書,報告書

前提: DATABASE_URL（または --dsn）・AWS 認証・BEDROCK_MODEL_ID 等の env。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

import structlog  # noqa: E402

from teamagent.cases.extract import (  # noqa: E402
    CaseDocumentMeta,
    CaseExtractionError,
    extract_cases,
)
from teamagent.cases.schema import CaseRecord  # noqa: E402
from teamagent.skills.search.skill import SearchSkill  # noqa: E402

logger = structlog.get_logger(__name__)

DEFAULT_DOC_TYPES = "提案書,報告書,施策実績"
_EXTERNAL_USE_VALUES = frozenset({"ok", "ng", "unknown"})


# -----------------------------------------------------------
# 純関数（DB / Bedrock 非依存・テスト対象）
# -----------------------------------------------------------
def parse_doc_types(raw: str) -> list[str]:
    """``提案書,報告書`` → ``["提案書", "報告書"]``（空要素・重複を除く）。"""
    out: list[str] = []
    for item in raw.replace("，", ",").replace("、", ",").split(","):
        text = item.strip()
        if text and text not in out:
            out.append(text)
    return out


def parse_since(raw: str | None) -> str | None:
    """``YYYY-MM-DD`` を検証して返す（None/空は None）。不正は ValueError。"""
    if raw is None or not raw.strip():
        return None
    return date.fromisoformat(raw.strip()).isoformat()


def document_url(row: Mapping[str, Any]) -> str:
    """source_uri から開ける URL を組む（SearchSkill._doc_url を再利用）。無ければ source_uri。"""
    url = SearchSkill._doc_url({"source_uri": row.get("source_uri")})
    return url or str(row.get("source_uri") or "")


def document_meta_from_row(row: Mapping[str, Any]) -> CaseDocumentMeta:
    """``list_documents_with_text`` の 1 行 → 抽出メタ（external_use は metadata の値を写す）。"""
    external_use = str(row.get("case_external_use") or "unknown").strip().lower()
    if external_use not in _EXTERNAL_USE_VALUES:
        external_use = "unknown"
    return CaseDocumentMeta(
        external_id=str(row.get("external_id") or row.get("document_id") or ""),
        url=document_url(row),
        title=str(row.get("title") or ""),
        doc_type=str(row.get("cls_doc_type") or ""),
        client_name=str(row.get("client_name") or row.get("cls_project") or ""),
        external_use=external_use,  # type: ignore[arg-type]
        modified_at=str(row.get("modified_at") or ""),
    )


def summary_text(record: CaseRecord) -> str:
    """埋め込みに使う要約（product＋result_masked＋winpattern・社名は含めない）。"""
    parts = [
        f"業種: {record.sector}",
        f"目的: {'、'.join(record.purpose)}",
        f"商品状態: {record.product_state}",
    ]
    if record.product:
        parts.append(f"商材: {record.product}")
    if record.result_masked:
        parts.append(f"結果: {record.result_masked}")
    if record.winpattern:
        parts.append(f"勝ち筋: {record.winpattern}")
    return "\n".join(parts)


@dataclass
class RunStats:
    docs_scanned: int = 0
    docs_with_cases: int = 0
    docs_failed: int = 0
    records: int = 0
    upserted: int = 0


def render_summary(
    stats: RunStats, records: Sequence[CaseRecord], *, dry_run: bool, examples: int = 3
) -> str:
    """dry-run / 本番共通の集計表示（社名は client_masked のみ・URL は出す）。"""
    lines = [
        f"mode: {'dry-run（DB へ書かない）' if dry_run else 'write'}",
        f"documents scanned: {stats.docs_scanned}",
        f"documents with cases: {stats.docs_with_cases}",
        f"documents failed: {stats.docs_failed}",
        f"case records: {stats.records}",
        f"upserted: {stats.upserted}",
    ]
    for record in list(records)[: max(0, examples)]:
        lines.append("---")
        lines.append(f"case_id: {record.case_id}")
        lines.append(
            f"{record.client_masked} / {record.sector} / {'、'.join(record.purpose)} / "
            f"{record.product_state} / traits={'、'.join(record.traits) or '-'}"
        )
        lines.append(f"result: {record.result_masked[:120]}")
        lines.append(
            "metrics: "
            + ("／".join(f"{m.name} {m.value}{m.unit}" for m in record.metrics[:4]) or "-")
        )
        lines.append(f"source: {record.sources[0].url}")
    return "\n".join(lines)


def records_to_json(records: Sequence[CaseRecord]) -> str:
    return json.dumps(
        [record.model_dump(mode="json") for record in records], ensure_ascii=False, indent=1
    )


# -----------------------------------------------------------
# 実行部（DB / Bedrock）
# -----------------------------------------------------------
def run(
    *,
    dsn: str,
    model_id: str,
    doc_types: list[str],
    since: str | None,
    limit: int,
    dry_run: bool,
    out_path: Path | None,
    max_repair: int,
) -> tuple[RunStats, list[CaseRecord]]:
    from teamagent.adapters.bedrock_client import BedrockClient
    from teamagent.adapters.pgvector_client import PgVectorClient
    from teamagent.cases.store import upsert_case_records

    stats = RunStats()
    records: list[CaseRecord] = []
    bedrock = BedrockClient.from_env(model_id_override=model_id)
    pg = PgVectorClient(dsn)
    run_id = f"cases-{uuid.uuid4().hex[:8]}"
    try:
        with pg.connection(application_name="extract_cases") as conn:
            rows = pg.list_documents_with_text(
                conn, doc_types=doc_types, since=since, limit=limit, request_id=run_id
            )
        stats.docs_scanned = len(rows)
        for index, row in enumerate(rows, start=1):
            meta = document_meta_from_row(row)
            request_id = f"{run_id}-{index}"
            try:
                extracted = extract_cases(
                    str(row.get("full_text") or ""),
                    document_meta=meta,
                    bedrock=bedrock,
                    model_id=model_id,
                    request_id=request_id,
                    max_repair=max_repair,
                )
            except CaseExtractionError:
                stats.docs_failed += 1
                logger.warning("extract_cases_document_failed", request_id=request_id)
                continue
            if extracted:
                stats.docs_with_cases += 1
            records.extend(extracted)
        stats.records = len(records)

        if not dry_run and records:
            from teamagent.adapters.embeddings_client import (
                build_embedder_from_env,
                resolve_embedder_backend,
            )

            embedder = build_embedder_from_env()
            backend = resolve_embedder_backend()
            embeddings = {r.case_id: embedder.embed_passage(summary_text(r)) for r in records}
            with pg.connection(application_name="extract_cases") as conn:
                stats.upserted = upsert_case_records(
                    conn,
                    records,
                    embeddings=embeddings,
                    embedding_backend=backend,
                    request_id=run_id,
                )
    finally:
        pg.close()

    if out_path is not None:
        out_path.write_text(records_to_json(records), encoding="utf-8")
    return stats, records


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vault 文書から事例レコードを抽出する")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--limit", type=int, default=50, help="読む文書数の上限（既定 50）")
    parser.add_argument("--since", default=None, help="YYYY-MM-DD 以降に更新された文書だけ")
    parser.add_argument(
        "--doc-types", default=DEFAULT_DOC_TYPES, help=f"cls_doc_type（既定 {DEFAULT_DOC_TYPES}）"
    )
    parser.add_argument("--dry-run", action="store_true", help="抽出だけ行い DB へ書かない")
    parser.add_argument("--out", default=None, help="抽出結果 JSON の保存先（eval 用）")
    parser.add_argument(
        "--model-id",
        default=os.environ.get("CASE_EXTRACT_MODEL_ID") or os.environ.get("BEDROCK_MODEL_ID"),
    )
    parser.add_argument("--max-repair", type=int, default=1)
    args = parser.parse_args(argv)

    if not args.dsn:
        print("DATABASE_URL または --dsn が必要です", file=sys.stderr)
        return 2
    if not args.model_id:
        print(
            "--model-id / CASE_EXTRACT_MODEL_ID / BEDROCK_MODEL_ID のいずれかが必要です",
            file=sys.stderr,
        )
        return 2
    doc_types = parse_doc_types(args.doc_types)
    if not doc_types:
        print("--doc-types が空です", file=sys.stderr)
        return 2
    try:
        since = parse_since(args.since)
    except ValueError:
        print("--since は YYYY-MM-DD", file=sys.stderr)
        return 2
    if args.limit < 1:
        print("--limit は 1 以上", file=sys.stderr)
        return 2

    stats, records = run(
        dsn=args.dsn,
        model_id=args.model_id,
        doc_types=doc_types,
        since=since,
        limit=args.limit,
        dry_run=args.dry_run,
        out_path=Path(args.out).expanduser() if args.out else None,
        max_repair=max(0, args.max_repair),
    )
    print(render_summary(stats, records, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
