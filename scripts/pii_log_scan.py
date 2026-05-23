"""CloudWatch Logs から PII / 機密情報の漏洩を grep するスクリプト。

Sprint 2 / 2.7 「ログから PII 漏洩スキャン」タスク。

CLAUDE.md 6-bis の Don't：
- エラーログに生入力（提案 PDF 全文・顧客名・会話履歴）を入れない

このスクリプトは過去 N 時間の CloudWatch Logs を Logs Insights で走査し、
明らかな漏洩パターン（顧客名・PDF 全文っぽい長文・xoxb-/xapp- トークン・
sk-ant- API キー）が出ていないかを集計する。

Usage:
    # ローカルから（AWS 認証済）
    python scripts/pii_log_scan.py --hours 24 --log-group /teamagent/dev

    # 結果を JSON で保存
    python scripts/pii_log_scan.py --hours 168 --output reports/pii_scan_$(date +%Y%m%d).json

Exit code:
    0: 漏洩疑い 0 件
    1: 1 件以上の疑いを検出
    2: 設定/接続エラー
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3

# -----------------------------------------------------------
# PII / シークレットの正規表現パターン
# -----------------------------------------------------------
# 注：顧客名は固有名詞リストに依存するので、社外秘の名前は env 経由で渡す
PATTERNS: dict[str, re.Pattern[str]] = {
    "slack_bot_token": re.compile(r"xoxb-[0-9A-Za-z\-]{20,}"),
    "slack_app_token": re.compile(r"xapp-[0-9A-Za-z\-]{20,}"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "email_address": re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    "jp_phone": re.compile(r"0\d{1,4}-\d{1,4}-\d{4}"),
    # 提案 PDF 全文の混入：1 メッセージ 2000 文字超は要注意（content フィールドや message 内）
    "very_long_text": re.compile(r"\b\w[\s\S]{2000,}"),
}


@dataclass
class HitSummary:
    pattern: str
    count: int = 0
    samples: list[str] = field(default_factory=list)  # マスク済みサンプル最大 3 件


def _mask(s: str, keep: int = 4) -> str:
    """中身を伏せた表示用文字列にする。"""
    if len(s) <= keep * 2:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep * 2) + s[-keep:]


def scan_log_group(
    log_group: str,
    region: str,
    hours: int,
    *,
    customer_names: list[str] | None = None,
) -> dict[str, HitSummary]:
    """指定ロググループを Logs Insights で走査して PII パターンを集計する。"""
    client = boto3.client("logs", region_name=region)

    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())

    # message フィールド全体を取得して python 側で正規表現マッチ
    # （Logs Insights の filter は正規表現が弱いので生メッセージを取って判定）
    query = "fields @timestamp, @message | sort @timestamp desc | limit 10000"

    resp = client.start_query(
        logGroupName=log_group,
        startTime=start_ts,
        endTime=end_ts,
        queryString=query,
    )
    qid = resp["queryId"]

    # ポーリング
    for _ in range(60):
        time.sleep(2)
        status = client.get_query_results(queryId=qid)
        if status["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break

    if status["status"] != "Complete":
        raise RuntimeError(f"Logs Insights query did not complete: {status['status']}")

    results: list[list[dict[str, str]]] = status.get("results", [])

    summaries: dict[str, HitSummary] = {name: HitSummary(pattern=name) for name in PATTERNS}
    # 顧客名は動的に追加
    if customer_names:
        for name in customer_names:
            summaries[f"customer:{name}"] = HitSummary(pattern=f"customer:{name}")

    for row in results:
        message = next((f["value"] for f in row if f["field"] == "@message"), "")
        if not message:
            continue
        for name, pat in PATTERNS.items():
            m = pat.search(message)
            if m:
                summaries[name].count += 1
                if len(summaries[name].samples) < 3:
                    summaries[name].samples.append(_mask(m.group(0)))
        if customer_names:
            for cname in customer_names:
                if cname in message:
                    key = f"customer:{cname}"
                    summaries[key].count += 1
                    if len(summaries[key].samples) < 3:
                        # 前後 30 文字だけ抽出してマスク
                        idx = message.find(cname)
                        window = message[max(0, idx - 30) : idx + len(cname) + 30]
                        summaries[key].samples.append(_mask(window, keep=8))

    return summaries


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--log-group", default="/teamagent/dev")
    p.add_argument("--region", default="ap-northeast-1")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument(
        "--customers",
        default="",
        help="カンマ区切りの顧客名（grep 対象）。CI で社内シークレット経由が望ましい",
    )
    p.add_argument("--output", default=None, help="JSON 出力先（省略時は stdout）")
    args = p.parse_args()

    customer_names = [c.strip() for c in args.customers.split(",") if c.strip()]
    try:
        summaries = scan_log_group(
            args.log_group, args.region, args.hours, customer_names=customer_names
        )
    except Exception as e:
        print(f"[ERROR] scan failed: {e}", file=sys.stderr)
        return 2

    payload: dict[str, Any] = {
        "scanned_at": datetime.now(UTC).isoformat(),
        "log_group": args.log_group,
        "hours": args.hours,
        "hits": {
            name: {"count": s.count, "samples": s.samples}
            for name, s in summaries.items()
            if s.count > 0
        },
    }
    out_json = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out_json)
    print(out_json)

    total = sum(s.count for s in summaries.values())
    return 1 if total > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
