#!/usr/bin/env python3
"""VSEO データ準備 CLI。

5KW を受け取り、tiktok_search で各KWを検索して VSEO スキルが食う JSON 群を
出力ディレクトリに生成する。人手の「ラッコ検索→上位30本を個別取得→Excel化」を
完全自動化する。

使い方:
    python scripts/vseo_prepare.py --out ./vseo_out \\
        --kw "新宿 ランチ" --kw "新宿 グルメ" --kw "新宿 ディナー" \\
        --kw "新宿 カフェ" --kw "新宿 居酒屋"

    # KW をカンマ区切りでまとめても可
    python scripts/vseo_prepare.py --out ./vseo_out \\
        --kws "新宿 ランチ,新宿 グルメ,新宿 ディナー,新宿 カフェ,新宿 居酒屋"

出力 (--out 配下):
    top10_with_urls.json / multi_kw_videos.json / kw_stats.json / _meta.json
    covers/<kw>/rankNN.jpeg

生成後、VSEO スキル (~/.claude/skills/tiktok-vseo-proposal/) の build_proposal.js を
この出力ディレクトリに対して実行すれば PPTX が生成できる。
ラッコ検索量 (kw50_categorized.json) は別途 VSEO スキル側で取得する。

前提: TIKTOK_NODE_BIN (node 絶対パス) と Chrome。tools/tiktok_scraper で npm install 済み。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from teamagent.skills.vseo.dataprep import utc_now_ts
from teamagent.skills.vseo.prepare import prepare_vseo_data


def main() -> None:
    ap = argparse.ArgumentParser(description="VSEO 提案書用データを TikTok 検索から自動生成")
    ap.add_argument("--out", type=Path, required=True, help="出力ディレクトリ")
    ap.add_argument("--kw", action="append", default=[], help="検索KW (複数指定可)")
    ap.add_argument("--kws", default="", help="検索KW をカンマ区切りで一括指定")
    ap.add_argument("--max", type=int, default=30, help="各KWの最大取得本数 (既定30)")
    ap.add_argument("--no-thumbnails", action="store_true", help="サムネ画像DLをスキップ")
    args = ap.parse_args()

    keywords: list[str] = list(args.kw)
    if args.kws:
        keywords += [k.strip() for k in args.kws.split(",") if k.strip()]
    # 重複除去・順序維持
    seen: set[str] = set()
    deduped: list[str] = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
    keywords = deduped

    if not keywords:
        print("エラー: --kw または --kws で検索KWを指定してください", file=sys.stderr)
        sys.exit(1)
    if len(keywords) > 10:
        print(f"エラー: KWが多すぎます ({len(keywords)}個)。最大10個まで", file=sys.stderr)
        sys.exit(1)

    print(f"[VSEO] {len(keywords)} KW を検索: {keywords}", file=sys.stderr)
    result = prepare_vseo_data(
        keywords,
        args.out,
        max_videos=args.max,
        now_ts=utc_now_ts(),
        download_thumbnails=not args.no_thumbnails,
    )

    print("\n=== VSEO データ準備 完了 ===")
    print(f"出力先: {result.project_dir}")
    for kw, n in result.counts.items():
        print(f"  {kw}: {n}本")
    print(f"マルチKW入賞: {result.multi_kw_count}本")
    print(f"サムネDL: {result.covers_saved}枚")
    if result.failed_keywords:
        print(f"⚠️ 検索失敗KW: {result.failed_keywords}")
    print("\n次のステップ: VSEO スキルの build_proposal.js をこの出力に対して実行")


if __name__ == "__main__":
    main()
