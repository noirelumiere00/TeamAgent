"""VideoAlgorithm Skill ローカル実走デモ — TikTok 検索 KW → report/slides HTML を生成。

本番に触れず、ローカルで実物の HTML（と任意で S3 署名URL）を作って手触り確認するためのCLI。
- report:  分析レポートHTML（縦長ダッシュボード）
- slides:  編集可スライドHTML（contenteditable・16:9・営業がブラウザで直接編集）
PPTX は今回スコープ外なので生成しない（outputs=["report","slides"]）。

前提（env）:
  - Gemini:     GEMINI_API_KEY（ローカルはAPIキー可） or Vertex 一式
  - 会社プロキシ: SSL_CERT_FILE=~/.hermes/ca_bundle.pem
  - tiktok検索:  node + Chromium（tools/tiktok_scraper/ は npm install 済）
  - S3署名URL:   aws 資格 + 下記を export（未設定なら report_url/slides_url は None・ローカルHTMLは出る）
      export VSEO_REPORT_BUCKET=teamagent-dev-raw-files
      export AWS_REGION=ap-northeast-1

使い方:
  python scripts/demo_video_algorithm_local.py 集中
  python scripts/demo_video_algorithm_local.py "作業用BGM" --max-videos 3
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from teamagent.skills.base import SkillContext  # noqa: E402
from teamagent.skills.video_algorithm.schema import VideoAlgorithmInput  # noqa: E402
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="VideoAlgorithm ローカル実走デモ")
    parser.add_argument("query", nargs="?", default="集中", help="TikTok 検索キーワード")
    parser.add_argument(
        "--max-videos", type=int, default=None, help="深掘り分析本数（1〜10・既定は env）"
    )
    parser.add_argument(
        "--board-size", type=int, default=None, help="取得（ボード）本数（5〜30・既定は env=30）"
    )
    args = parser.parse_args()

    bucket = os.environ.get("VSEO_REPORT_BUCKET")
    print(f"🔎 VideoAlgorithm ローカル実走: '{args.query}'")
    print(f"   S3 bucket: {bucket or '(未設定 → 署名URLなし・ローカルHTMLのみ)'}")
    print("   ⏳ TikTok 検索 → 動画取得 → Gemini 分析 → HTML 生成（1〜3分）...\n")

    skill = VideoAlgorithmSkill()
    kwargs: dict[str, object] = {"query": args.query, "outputs": ["report", "slides"]}
    if args.max_videos is not None:
        kwargs["max_videos"] = args.max_videos
    if args.board_size is not None:
        kwargs["board_size"] = args.board_size
    inp = VideoAlgorithmInput(**kwargs)  # type: ignore[arg-type]
    ctx = SkillContext(request_id=f"local-vseo-{uuid.uuid4().hex[:8]}")

    out = skill.run(inp, ctx)

    analyzed = sum(1 for v in out.videos if v.analysis)
    print("✓ 完了")
    print(f"   分析本数        : {analyzed} / {len(out.videos)} 本")
    print(f"   レポートHTML(local): {out.report_html_path}")
    print(f"   レポートURL(S3 7日) : {out.report_url or '(VSEO_REPORT_BUCKET 未設定で None)'}")
    print(f"   スライドURL(S3 7日) : {out.slides_url or '(VSEO_REPORT_BUCKET 未設定で None)'}")
    print(f"   概算コスト        : ${out.total_cost_usd:.4f}")
    if out.report_html_path:
        print(f"\n💡 ブラウザで開く: open '{out.report_html_path}'")
    print("   slides は contenteditable ＝ 文字をクリックしてその場編集できます。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
