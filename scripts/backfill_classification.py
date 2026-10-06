"""分類（cls_*）が 1 つも付いていない documents を、DB の本文から分類し直す（2026-10-02）。

なぜ: Bedrock の月予算 80% で Budgets の自動 action が ingest タスクロールへ
AiLa-CostCap-DenyBedrock を付け（09-25〜）、その間に取り込んだ文書は分類が全件失敗した
（doc_classify_bedrock_failed・10-01 は 87/87）。差分取り込みは本文が変わらない限り
同じ文書を再処理しないため、未分類のまま残り続ける（本番実測: 09-25 以降の新規 143 件・
Slack 投稿 257/786 件が cls_* を 1 つも持たない）。

使い方（SSM トンネル前提・backfill_entities.py と同じ接続経路）:
  DATABASE_URL=... uv run python scripts/backfill_classification.py --dry-run
  DATABASE_URL=... uv run python scripts/backfill_classification.py --since 2026-09-25
  DATABASE_URL=... uv run python scripts/backfill_classification.py --limit 20

方針:
  - 対象は cls_project / cls_industry / cls_doc_type / cls_phase / cls_solution の
    **どれも持たず**、まだ backfill 済みの印（cls_backfill_at）も無い文書（冪等）
  - 分類は ingest と同じ DocClassifier（タイトル＋本文の先頭）。Slack/Drive から取り直さない
  - **LLM に届いたときだけ書く**。Bedrock が失敗した文書は書かない（ルールだけの分類で
    「分類済み」に見せかけると、二度と対象にならない）。3 件続けて失敗したら止める
    （費用上限の deny が解除されていない等。止めずに回すと全件が rules-only で終わる）
  - 結果が空でも cls_backfill_at だけは書く（再実行で同じ文書に毎回課金しない）。
    cls_* に空文字は書かない（空文字は「別の値」扱いで自動フィルタに必ず落ちる・#508）
  - metadata は || でマージ（既存キーは消さない）。UPDATE は「まだ未分類」を条件にする
  - admin role（app.user_role='admin'）で RLS を通す
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

CLS_KEYS = ("cls_project", "cls_industry", "cls_doc_type", "cls_phase", "cls_solution")
MARKER_KEY = "cls_backfill_at"
MAX_CONSECUTIVE_FAILURES = 3
_SAMPLE_CHARS = 4000
# 見積り（1 文書あたりの概算トークンと Haiku 4.5 単価・backfill_entities.py と同じ置き方）。
_EST_INPUT_TOKENS = 1700
_EST_OUTPUT_TOKENS = 120
_HAIKU_IN_PER_M = 1.0
_HAIKU_OUT_PER_M = 5.0

_UNCLASSIFIED_SQL = " AND ".join(f"(d.metadata->>'{k}') IS NULL" for k in CLS_KEYS)


def estimate_cost_usd(n_docs: int) -> float:
    return (
        n_docs * _EST_INPUT_TOKENS / 1_000_000 * _HAIKU_IN_PER_M
        + n_docs * _EST_OUTPUT_TOKENS / 1_000_000 * _HAIKU_OUT_PER_M
    )


def target_sql(*, since: str | None, limit: int | None) -> tuple[str, list[Any]]:
    """対象文書（id, title, 本文の先頭）を引く SQL。since は ingested_at（JST 日付）。"""
    sql = (
        "SELECT d.id, COALESCE(d.title, ''), "
        "  LEFT(string_agg(c.content, E'\\n' ORDER BY c.chunk_idx), %s) "
        "FROM documents d JOIN chunks c ON c.document_id = d.id "
        f"WHERE {_UNCLASSIFIED_SQL} AND (d.metadata->>'{MARKER_KEY}') IS NULL "
    )
    params: list[Any] = [_SAMPLE_CHARS]
    if since:
        sql += "AND d.ingested_at >= (%s::date AT TIME ZONE 'Asia/Tokyo') "
        params.append(since)
    sql += "GROUP BY d.id, d.title ORDER BY d.id "
    if limit:
        sql += "LIMIT %s"
        params.append(int(limit))
    return sql, params


UPDATE_SQL = (
    "UPDATE documents SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb "
    f"WHERE id = %s AND {_UNCLASSIFIED_SQL.replace('d.metadata', 'metadata')}"
)


class _CountingBedrock:
    """DocClassifier に渡す Bedrock の薄い包み。この文書で converse が例外を出したかを数える。

    DocClassifier は Bedrock の失敗を握ってルールだけの分類を返すため、戻り値からは
    「LLM に届いたか」が分からない。届いていない文書を書かないために、ここで観測する。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.failed = False

    def reset(self) -> None:
        self.failed = False

    def converse(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._inner.converse(*args, **kwargs)
        except Exception:
            self.failed = True
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@dataclass
class Outcome:
    classified: int = 0
    empty: int = 0
    failed: int = 0
    aborted: bool = False


def patch_for(classification: Any, today: str) -> dict[str, str]:
    """書き込む metadata の差分。空の分類でも印だけは付ける。cls_* に空文字は入れない。"""
    patch: dict[str, str] = {}
    if classification is not None:
        patch.update({k: v for k, v in classification.as_metadata().items() if v})
    patch[MARKER_KEY] = today
    return patch


def run(
    targets: list[tuple[str, str, str]],
    *,
    classifier: Any,
    bedrock: _CountingBedrock,
    write: Any,
    today: str,
    progress: Any = print,
) -> Outcome:
    """targets を順に分類して write(doc_id, patch) する。LLM に届かなかった文書は書かない。"""
    out = Outcome()
    streak = 0
    for i, (doc_id, title, sample) in enumerate(targets):
        bedrock.reset()
        cls = classifier.classify(title=title, text=sample, request_id=f"backfill-cls-{i}")
        if bedrock.failed:
            out.failed += 1
            streak += 1
            if streak >= MAX_CONSECUTIVE_FAILURES:
                out.aborted = True
                break
            continue
        streak = 0
        write(doc_id, patch_for(cls, today))
        if cls is not None and not cls.is_empty():
            out.classified += 1
        else:
            out.empty += 1
        if (i + 1) % 25 == 0:
            progress(f"  進捗 {i + 1}/{len(targets)}  {out}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="対象件数と推定コストのみ")
    ap.add_argument("--since", default=None, help="ingested_at の下限（JST・YYYY-MM-DD）")
    ap.add_argument("--limit", type=int, default=None, help="処理件数の上限")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("DATABASE_URL 未設定", file=sys.stderr)
        return 2

    import psycopg

    conn = psycopg.connect(dsn, connect_timeout=15, client_encoding="UTF8")
    cur = conn.cursor()
    cur.execute("SET app.user_role = 'admin'")
    sql, params = target_sql(since=args.since, limit=args.limit)
    cur.execute(sql, params)
    targets = [(str(r[0]), str(r[1] or ""), str(r[2] or "")) for r in cur.fetchall()]
    print(
        f"対象（未分類・未処理）: {len(targets)} 件 / 推定 ${estimate_cost_usd(len(targets)):.2f}"
    )
    if args.dry_run or not targets:
        conn.close()
        return 0

    from teamagent.adapters.bedrock_client import BedrockClient
    from teamagent.ingest.classify import DocClassifier

    bedrock = _CountingBedrock(BedrockClient.from_env())
    classifier = DocClassifier(bedrock)

    def write(doc_id: str, patch: dict[str, str]) -> None:
        cur.execute(UPDATE_SQL, (json.dumps(patch, ensure_ascii=False), doc_id))
        conn.commit()

    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date()
    out = run(targets, classifier=classifier, bedrock=bedrock, write=write, today=str(today))
    conn.close()
    print(
        f"完了: classified={out.classified} empty={out.empty} failed={out.failed} "
        f"/ 対象={len(targets)}"
    )
    if out.aborted:
        print(
            f"中断: Bedrock が {MAX_CONSECUTIVE_FAILURES} 件続けて失敗（費用上限の deny が残っている"
            "可能性）。失敗した文書は書いていないので、解除後にもう一度流せば続きから処理する。",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
