"""既存 documents に名寄せタグ（cls_entities）を backfill する（2026-07-14）。

DB に既にある本文（chunks）を読み、Haiku で関係者エンティティを抽出して
documents.metadata->cls_entities に書き戻す。Slack/Drive からの再取得はしない。

使い方（SSM トンネル前提・migrate_tunneled.sh と同じ接続経路を想定）:
  DATABASE_URL=... uv run python scripts/backfill_entities.py --dry-run   # 見積り（書込なし）
  DATABASE_URL=... uv run python scripts/backfill_entities.py             # 実適用
  DATABASE_URL=... uv run python scripts/backfill_entities.py --limit 50  # 一部だけ

方針:
  - 対象は cls_entities 未設定の documents（再実行で二重処理しない・冪等）。
  - 1 資料 = 代表チャンク（chunk_idx 昇順で連結・上限 4000 字）を LLM に渡す。
  - admin role（app.user_role='admin'）で RLS を通す。metadata の jsonb_set で追記。
  - dry-run は LLM を呼ばず対象件数と推定コストのみ出す（Bedrock 課金を発生させない）。
  - fail-open: 1 件の抽出失敗は skip して次へ（全体は止めない）。
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg
import structlog

logger = structlog.get_logger(__name__)

_SAMPLE_CHARS = 4000
# 見積り用の 1 資料あたり概算トークン（入力=本文抜粋、出力=短い JSON）と Haiku 4.5 単価。
_EST_INPUT_TOKENS = 1500
_EST_OUTPUT_TOKENS = 80
_HAIKU_IN_PER_M = 1.0
_HAIKU_OUT_PER_M = 5.0


def _estimate_cost_usd(n_docs: int) -> float:
    return (
        n_docs * _EST_INPUT_TOKENS / 1_000_000 * _HAIKU_IN_PER_M
        + n_docs * _EST_OUTPUT_TOKENS / 1_000_000 * _HAIKU_OUT_PER_M
    )


def _fetch_targets(cur: object, limit: int | None) -> list[tuple[str, str, str]]:
    """(document_id, title, sample_text) を返す。cls_entities 未設定のみ・本文は代表連結。"""
    # LEFT(...,4000) を SQL 側で切り、巨大 doc の全チャンクを Python へ転送しない（M5）。
    sql = (
        "SELECT d.id, COALESCE(d.title, ''), "
        "  LEFT(string_agg(c.content, E'\\n' ORDER BY c.chunk_idx), 4000) "
        "FROM documents d JOIN chunks c ON c.document_id = d.id "
        "WHERE (d.metadata->>'cls_entities') IS NULL "
        "GROUP BY d.id, d.title "
    )
    if limit:
        sql += f"LIMIT {int(limit)}"
    cur.execute(sql)  # type: ignore[attr-defined]
    out: list[tuple[str, str, str]] = []
    for row in cur.fetchall():  # type: ignore[attr-defined]
        out.append((str(row[0]), str(row[1] or ""), str(row[2] or "")[:_SAMPLE_CHARS]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dry-run", action="store_true", help="対象件数と推定コストのみ（書込・LLMなし）"
    )
    ap.add_argument("--limit", type=int, default=None, help="処理件数の上限")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("DATABASE_URL 未設定", file=sys.stderr)
        return 2

    conn = psycopg.connect(dsn, connect_timeout=15, client_encoding="UTF8")
    cur = conn.cursor()
    cur.execute("SET app.user_role = 'admin'")
    targets = _fetch_targets(cur, args.limit)
    print(
        f"対象（cls_entities 未設定）: {len(targets)} 件 / 推定コスト ${_estimate_cost_usd(len(targets)):.2f}"
    )
    if args.dry_run:
        print("dry-run: 書き込み・LLM 呼び出しなしで終了")
        conn.close()
        return 0

    from teamagent.adapters.bedrock_client import BedrockClient
    from teamagent.ingest.entity_extract import extract_entities

    bedrock = BedrockClient.from_env()
    tagged = 0
    empty = 0
    failed = 0
    for i, (doc_id, title, sample) in enumerate(targets):
        try:
            ents = extract_entities(
                title=title, text=sample, bedrock=bedrock, request_id=f"backfill-{i}"
            )
        except Exception:
            failed += 1
            continue
        # 空でも cls_entities="" を書く（NULL のままだと再実行で毎回 LLM 再課金＝M4）。
        value = ",".join(ents)
        try:
            cur.execute(
                "UPDATE documents SET metadata = jsonb_set("
                "  COALESCE(metadata, '{}'::jsonb), '{cls_entities}', to_jsonb(%s::text)) "
                "WHERE id = %s",
                (value, doc_id),
            )
            conn.commit()
            if ents:
                tagged += 1
            else:
                empty += 1
        except Exception:
            conn.rollback()
            failed += 1
        if (i + 1) % 50 == 0:
            print(f"  進捗 {i + 1}/{len(targets)}  tagged={tagged} empty={empty} failed={failed}")

    print(f"完了: tagged={tagged} empty={empty} failed={failed} / 対象={len(targets)}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
