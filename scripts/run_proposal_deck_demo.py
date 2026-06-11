"""proposal_deck Skill のライブ実走デモ（Bedrock / RDS 不要）。

検索する Agent 部分（search 等）は RDS を要するが、proposal_deck 単体は Bedrock + FMT
テンプレだけで動く。研究素材（過去事例 / Slack / Mail / 社会潮流）を手で渡して、実コンテンツの
提案書 .pptx を 1 枚生成する。

必要な env:
    AWS_REGION              例) us-east-1
    BEDROCK_MODEL_ID        例) us.anthropic.claude-sonnet-4-6
    TEAMAGENT_FMT_TEMPLATE  例) $PWD/data/templates/template_v2.pptx
    （AWS 認証は ~/.aws / 環境変数 / インスタンスロール）

使い方:
    .venv/bin/python scripts/run_proposal_deck_demo.py                       # 内蔵サンプルで実走
    .venv/bin/python scripts/run_proposal_deck_demo.py \\
        --product "商材名" --goal "..." --persona "..." \\
        --research-file path/to/research.md --out-dir "$HOME/Downloads"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_deck.schema import ProposalDeckInput
from teamagent.skills.proposal_deck.skill import ProposalDeckSkill

# 本来は Agent が search / proposal_draft / clientkarte / mail_constraints で集める素材の擬似版。
# （実 Agent 実走では research_material にこの種の収集結果が入る）
_DEFAULT_RESEARCH = """\
# 過去事例（Drive RAG 相当・社内提案アーカイブ）
- [chunk_id: 812] 飲料D2C「朝活ドリンク」提案: 「#朝活ルーティン」界隈で UGC を量産し指名検索が約2.3倍。
  勝ち筋＝“効果効能を直接言わず、続けられる自己管理”の文脈で共感フックを作ったこと。
  出典: https://drive.example.com/case-812
- [chunk_id: 540] 健康食品メーカーのTikTok施策: ナノインフルエンサー×時短レシピ動画で保存率12%。
  出典: https://drive.example.com/case-540

# Slack 営業FB（#proj-ショート動画_営業フィードバック情報）
- 「健康ジャンルは“効果効能”直球より“習慣・気分”の文脈の方が刺さる」（2026-04, 担当: 佐藤）
- 「腸活・ととのう系ワードが伸長。競合は機能訴求に寄りがち＝情緒文脈で差別化余地あり」（担当: 田中）

# Mail（クライアントブリーフ要約）
- 与件: 20-30代女性の新規認知と指名検索の最大化。予算は中規模。NG: 過度な効果効能表現。

# 社会潮流・調査データ（出典つき）
- 「腸活」関心は20-30代女性で増加傾向。出典: https://research.example.com/gut-health-2026
- 朝の自己管理ルーティン市場が拡大。出典: https://research.example.com/morning-routine-2026

# TikTok（界隈/ハッシュタグの目安）※実件数は要 VSEO 実測
- #ととのう #朝活 #腸活 が健康感度層で活発（数値は未検証）
"""


def main() -> int:
    p = argparse.ArgumentParser(description="proposal_deck ライブ実走デモ（Bedrock）")
    p.add_argument("--product", default="ベジトリー オーガニック青汁")
    p.add_argument("--goal", default="20〜30代女性への認知獲得とSNS上の指名検索の最大化")
    p.add_argument("--persona", default="20〜30代女性・健康/美容感度高め・TikTok中重度ユーザー")
    p.add_argument("--deadline", default="2026-07-31")
    p.add_argument("--url", default="https://example.com/vegetree")
    p.add_argument(
        "--research-file", default=None, help="研究素材ファイル(md/txt)。未指定なら内蔵サンプル"
    )
    p.add_argument("--out-dir", default=str(Path.home() / "Downloads"))
    p.add_argument("--max-repair", type=int, default=4)
    args = p.parse_args()

    research = (
        Path(args.research_file).read_text(encoding="utf-8")
        if args.research_file
        else _DEFAULT_RESEARCH
    )

    skill = ProposalDeckSkill()
    print(f"▶ proposal_deck 実走（Bedrock / Sonnet 4.6）: {args.product}")
    out = skill.run(
        ProposalDeckInput(
            product_name=args.product,
            goal=args.goal,
            target_persona=args.persona,
            deadline=args.deadline,
            urls=[args.url] if args.url else [],
            research_material=research,
            out_dir=args.out_dir,
            max_repair=args.max_repair,
        ),
        ctx=SkillContext(),
    )
    print("\n✅ 生成完了")
    print(f"  pptx        : {out.pptx_path}")
    print(
        f"  coverage    : {out.coverage_ratio:.2f}  "
        f"(filled={out.filled_count} / skipped={out.skipped_count})"
    )
    print(f"  skipped_ids : {out.skipped_ids}")
    print(f"  cost_usd    : {out.total_cost_usd:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
