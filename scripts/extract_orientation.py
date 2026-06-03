"""案件シート → OrientationBrief 抽出の動作確認 CLI（Phase1 手動起動の中核）。

    set -a; source .env.production; set +a
    source scripts/load_secrets.sh
    GOOGLE_FORCE_OAUTH=1 python scripts/extract_orientation.py <sheet_id> [管理番号]

管理番号を省略すると、納品（Drive）動画が入っているクリエイティブを一覧表示する。
管理番号を渡すと、その 1 件の OrientationBrief（Gemini に渡る正解条件）と
納品動画 URL を表示する。
"""

from __future__ import annotations

import argparse
import sys

from teamagent.skills.video_approval.sheet_orientation import OrientationExtractor


def main() -> int:
    ap = argparse.ArgumentParser(description="案件シートからオリエンを抽出")
    ap.add_argument("sheet_id", help="スプレッドシート ID")
    ap.add_argument("management_no", nargs="?", default=None, help="管理番号 (例 E01-01)")
    ap.add_argument("--client", default=None, help="商材名の上書き (例 伊藤園)")
    args = ap.parse_args()

    ext = OrientationExtractor(client_name=args.client)

    if args.management_no is None:
        refs = ext.list_creatives(args.sheet_id)
        ready = [r for r in refs if r.has_drive_video]
        print(f"# クリエイティブ {len(refs)} 件 (うち納品動画あり {len(ready)} 件)\n")
        for r in refs:
            mark = "🎬" if r.has_drive_video else "  "
            print(f" {mark} {r.management_no:<10} {r.creative_name[:40]}")
        if ready:
            print("\n納品済み（審査可能）の管理番号:")
            for r in ready:
                print(f"  {r.management_no}  → {r.video_url[:80]}")
        return 0

    res = ext.extract(args.sheet_id, args.management_no)
    if res is None:
        print(f"管理番号 {args.management_no} が見つかりませんでした", file=sys.stderr)
        return 1
    print(f"# 管理番号: {res.management_no}")
    print(f"# 納品動画 URL: {res.video_url or '(未入稿)'}")
    print(f"# Drive 動画か: {res.has_drive_video}\n")
    print("## OrientationBrief（審査の正解条件）")
    print(res.orientation.to_prompt_block())
    return 0


if __name__ == "__main__":
    sys.exit(main())
