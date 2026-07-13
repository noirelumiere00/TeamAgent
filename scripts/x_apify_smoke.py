"""X系 Apify actor の実疎通スモーク（ローカル・手動実行・~$0.01未満）。

使い方（APIFY_API_TOKEN を手元 env に設定して実行。CIでは動かさない）:
    APIFY_API_TOKEN=... uv run python scripts/x_apify_smoke.py "セブンイレブン 新商品"

確認すること:
  1. scraper_one/x-posts-search が全文+いいね+URL を返す（BRONZE以上プランの確認を兼ねる）
  2. xtracto 実在検証が投稿を再取得できる
  3. apidojo 期間指定（昨日1日分・5件）が動く ＝ ④ワーカーの日割り取得の前提確認
     （start==end の同日指定で結果が返るかは④の設計前提。0件なら要調査と表示する）
"""

from __future__ import annotations

import datetime as dt
import sys

from teamagent.adapters.apify_client import ApifyClient


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "セブンイレブン 新商品"
    client = ApifyClient.from_env()

    print(f"[1/3] search_posts: {query!r} (5件)")
    posts, cost = client.search_posts(query, count=5, request_id="smoke")
    for p in posts[:3]:
        print(f"  @{p.author_handle} ❤️{p.like_count} {p.text[:40]!r} {p.url}")
    print(f"  -> {len(posts)}件 / ${cost:.4f}")
    if not posts:
        print("  ⚠️ 0件: クエリ変更 or Apifyプラン(BRONZE以上)を確認")
        return 1

    print("[2/3] verify_posts (xtracto・上位2件)")
    verified, vcost = client.verify_posts([p.url for p in posts[:2]], request_id="smoke")
    for url, v in verified.items():
        print(f"  {'✅' if v else '⚠️ 要再確認'} {url}")
    print(f"  -> ${vcost:.4f}")

    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    print(f"[3/3] search_posts_period (apidojo・{yesterday} 同日指定・5件)")
    period, pcost = client.search_posts_period(
        [query], start=yesterday, end=yesterday, max_items=5, request_id="smoke"
    )
    print(f"  -> {len(period)}件 / ${pcost:.4f}")
    if not period:
        print("  ⚠️ 同日(start==end)指定が0件: ④ワーカーの日割り境界を要調査")
        print("     （end を翌日にする補正が必要かもしれない）")
    print("smoke done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
